"""Browser drafts have no durable effects until a complete, conflict-checked Save."""

import json
import xml.etree.ElementTree as ET
from unittest.mock import patch
import unittest

from server.revisions import fingerprint
from tests import test_revisions as revision_tests


class EditorDocumentTest(unittest.TestCase):
    setUp = revision_tests.RevisionTest.setUp
    save = revision_tests.RevisionTest.save
    add = revision_tests.RevisionTest.add
    ready_variant = revision_tests.RevisionTest.ready_variant
    published = revision_tests.RevisionTest.published
    independent = revision_tests.RevisionTest.independent
    hosted_client = revision_tests.RevisionTest.hosted_client

    def open(self, client=None, item="sample"):
        response = (client or self.client).post(f"/api/editor/items/{item}/open")
        self.assertEqual(response.status_code, 200, response.text)
        descriptor = response.json()
        return {
            "baseline": descriptor["baseline"],
            "metadata": descriptor.get("metadata"),
            "outlines": descriptor["outlines"],
        }

    def commit(self, document, files=None, client=None, item="sample", download=False):
        return (client or self.client).post(
            f"/api/editor/items/{item}/commit"
            + ("?download_only=true" if download else ""),
            data={"document": json.dumps(document)},
            files=files or {},
        )

    def photo(self, document, key=""):
        outline = next(o for o in document["outlines"] if o["id"] == key)
        outline.update(mode="photo", source="alternative", state=self.state)
        return {
            f"source:{key}": ("source.png", self.mask, "image/png"),
            f"mask:{key}": ("mask.png", self.mask, "image/png"),
        }

    def test_open_svg_is_read_only_and_untouched_save_preserves_bytes(self):
        record = self.published()
        saved, work = fingerprint(record), fingerprint(self.work / "sample")
        original = (record / "outline.svg").read_bytes()
        document = self.open()
        self.assertEqual(document["outlines"][0]["mode"], "svg")
        self.assertIn("mask_svg", document["outlines"][0])
        self.assertEqual(fingerprint(record), saved)
        self.assertEqual(fingerprint(self.work / "sample"), work)
        response = self.commit(document)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((record / "outline.svg").read_bytes(), original)

    def test_processing_is_transient(self):
        before = fingerprint(self.work)
        response = self.client.post(
            "/api/editor/process",
            files={"image": ("image.png", self.mask, "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, self.mask)
        self.assertEqual(fingerprint(self.work), before)

    def test_new_photo_save_and_export(self):
        document = self.open()
        files = self.photo(document)
        before = fingerprint(self.work)
        response = self.commit(document, files, download=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertEqual(fingerprint(self.work), before)
        self.assertFalse((self.dataset / "vendor/type/sample").exists())
        response = self.commit(document, files)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((self.dataset / "vendor/type/sample/outline.svg").is_file())

    def test_invalid_replacement_and_baseline_conflict_preserve_original(self):
        record = self.published()
        document = self.open()
        before = fingerprint(record)
        files = self.photo(document)
        files["mask:"] = ("bad.png", b"invalid", "image/png")
        self.assertEqual(self.commit(document, files).status_code, 400)
        self.assertEqual(fingerprint(record), before)
        path = record / "metadata.json"
        path.write_text(path.read_text() + "\n")
        self.assertEqual(self.commit(document, self.photo(document)).status_code, 409)

    def test_complete_product_with_untouched_sibling_and_failed_incomplete_sibling(
        self,
    ):
        self.save()
        self.ready_variant()
        self.save("medium-large", done=True)
        record = self.dataset / "vendor/type/sample"
        original = (record / "variants/medium-large.svg").read_bytes()
        before = fingerprint(record)
        document = self.open()
        files = self.photo(document)
        sibling = document["outlines"][1]
        rating = sibling["state"]["rating"]
        sibling["state"]["rating"] = None
        self.assertEqual(self.commit(document, files).status_code, 400)
        self.assertEqual(fingerprint(record), before)
        sibling["state"]["rating"] = rating
        response = self.commit(document, files)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((record / "variants/medium-large.svg").read_bytes(), original)

    def test_length_only_preserves_vector_paths(self):
        record = self.published()
        original = (record / "outline.svg").read_bytes()
        document = self.open()
        outline = document["outlines"][0]
        outline["state"]["main_length"]["start"][0] += 20
        response = self.commit(document)
        self.assertEqual(response.status_code, 200, response.text)

        def paths(raw):
            return [
                n.get("d") for n in ET.fromstring(raw).iter() if n.tag.endswith("path")
            ]

        self.assertEqual(paths(original), paths((record / "outline.svg").read_bytes()))

    def test_hosted_save_waits_for_moderation_and_requires_claim(self):
        record = self.published()
        before = fingerprint(record)
        alice = self.hosted_client("Alice")
        document = self.open(alice)
        response = self.commit(document, self.photo(document), client=alice)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(fingerprint(record), before)
        self.assertIsNotNone(self.store.submission("sample"))
        self.assertEqual(self.commit(document, client=alice).status_code, 409)
        self.assertEqual(alice.post("/api/editor/items/sample/open").status_code, 403)
        moderator = self.hosted_client("Moderator", True)
        pending = self.open(moderator)
        contributor = self.store.submission("sample")["user_id"]
        response = self.commit(pending, client=moderator)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.submission("sample")["user_id"], contributor)
        self.assertEqual(fingerprint(record), before)

    def test_independent_metadata_commit(self):
        record, metadata = self.independent()
        self.workspace.dataset_changed()
        document = self.open(item="community:independent")
        document["metadata"]["notes"] = "Local edit"
        before = fingerprint(record)
        self.assertEqual(fingerprint(record), before)
        response = self.commit(document, item="community:independent")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            json.loads((record / "metadata.json").read_text())["notes"], "Local edit"
        )

    def test_variants_only_and_unusable_without_source(self):
        document = self.open()
        outline = document["outlines"][0]
        outline.update(id="medium", sizes=["Medium"], original_id=None)
        files = self.photo(document, "medium")
        response = self.commit(document, files)
        self.assertEqual(response.status_code, 200, response.text)
        record = self.dataset / "vendor/type/sample"
        self.assertTrue((record / "variants/medium.svg").is_file())
        self.assertFalse((record / "outline.svg").exists())

    def test_prepared_cache_does_not_prune_on_each_open(self):
        with patch.object(self.workspace, "_prune_catalog_cache") as prune:
            self.open()
            self.open()
        prune.assert_not_called()

    def test_brush_edit_is_traced_and_incomplete_upload_rejected(self):
        from PIL import Image, ImageDraw
        from tests.test_review import png_bytes

        record = self.published()
        original = (record / "outline.svg").read_bytes()
        document = self.open()
        outline = document["outlines"][0]
        outline["shape_changed"] = True
        mask = Image.new("RGBA", (outline["width"], outline["height"]))
        ImageDraw.Draw(mask).rectangle(
            (60, 60, outline["width"] - 60, outline["height"] - 60), fill="black"
        )
        paint = Image.new("RGBA", mask.size)
        ImageDraw.Draw(paint).rectangle((65, 65, 90, 90), fill="black")
        files = {
            "mask:": ("mask.png", png_bytes(mask), "image/png"),
            "edits:": ("edits.png", png_bytes(paint), "image/png"),
        }
        response = self.commit(document, files)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual((record / "outline.svg").read_bytes(), original)

    def test_work_only_saved_product_can_be_revised(self):
        self.workspace.dataset_dir = None
        self.save(done=True)
        document = self.open()
        response = self.commit(document)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((self.work / "sample/outline.svg").is_file())

    def test_general_can_be_assigned_to_variant_and_back(self):
        record = self.published()
        original = (record / "outline.svg").read_bytes()
        document = self.open()
        outline = document["outlines"][0]
        outline.update(id="small", sizes=["Small"])
        response = self.commit(document)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((record / "variants/small.svg").read_bytes(), original)
        document = self.open()
        document["outlines"][0].update(id="", sizes=[])
        response = self.commit(document)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((record / "outline.svg").read_bytes(), original)

    def test_unusable_blank_commits_without_fetching_source(self):
        document = self.open()
        document["outlines"][0].update(mode="blank", state={"rating": "unusable"})
        with patch.object(
            self.workspace,
            "prepare",
            side_effect=AssertionError("Unexpected processing"),
        ):
            response = self.commit(document)
        self.assertEqual(response.status_code, 200, response.text)

    def test_processing_does_not_block_session_requests(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        started, release = threading.Event(), threading.Event()

        def inference(_data):
            started.set()
            if not release.wait(10):
                raise RuntimeError("Processing was never released")
            return self.mask

        with patch.object(self.workspace, "remove_background", side_effect=inference):
            with ThreadPoolExecutor(max_workers=2) as pool:
                processing = pool.submit(
                    self.client.post,
                    "/api/editor/process",
                    files={"image": ("test.png", self.mask, "image/png")},
                )
                try:
                    self.assertTrue(started.wait(3))
                    session = pool.submit(self.client.get, "/api/session")
                    self.assertEqual(session.result(timeout=3).status_code, 200)
                    self.assertFalse(processing.done())
                finally:
                    release.set()
                self.assertEqual(processing.result(timeout=3).status_code, 200)

    def test_metadata_index_is_reused_until_dataset_changes(self):
        self.workspace.invalidate_record_cache()
        with patch.object(
            self.workspace,
            "_read_independent_records",
            wraps=self.workspace._read_independent_records,
        ) as read:
            self.workspace.independent_records()
            self.workspace.independent_records()
            self.assertEqual(read.call_count, 1)
            self.workspace.dataset_changed()
            self.workspace.independent_records()
            self.assertEqual(read.call_count, 2)

    def test_unexpected_parts_and_unauthenticated_requests_rejected(self):
        self.published()
        document = self.open()
        response = self.commit(
            document, {"edits:": ("edits.png", self.mask, "image/png")}
        )
        self.assertEqual(response.status_code, 400, response.text)
        alice = self.hosted_client("Alice")
        alice.cookies.clear()
        self.assertEqual(alice.post("/api/editor/items/sample/open").status_code, 401)
        self.assertEqual(alice.post("/api/editor/process").status_code, 401)
        self.assertEqual(
            alice.post(
                "/api/editor/items/sample/commit",
                headers={"Content-Length": str(131 * 1024 * 1024)},
            ).status_code,
            413,
        )

    def test_expired_claim_can_resume_without_discarding_browser_baseline(self):
        import time
        from server.hosted import CLAIM_MAX_SECONDS

        record = self.published()
        alice = self.hosted_client("Alice")
        document = self.open(alice)
        before = fingerprint(record)
        cached = fingerprint(self.work / "sample")
        with self.store.connect() as db:
            db.execute(
                "UPDATE claims SET claimed_at = ? WHERE item_id = ?",
                (time.time() - CLAIM_MAX_SECONDS - 1, "sample"),
            )
        response = alice.post("/api/editor/items/sample/claim")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("Resume editing", response.json()["detail"])
        self.assertEqual(fingerprint(self.work / "sample"), cached)
        reopened = self.open(alice)
        self.assertEqual(reopened["baseline"], document["baseline"])
        self.assertEqual(fingerprint(record), before)
        self.assertEqual(alice.post("/api/editor/items/sample/claim").status_code, 200)
