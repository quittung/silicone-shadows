"""Revision behavior: explicit commit, cancellation and source-free editing."""

import json
import shutil
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from server import create_app
from server.artifacts import svg_main_length
from server.hosted import HostedStore
from server.models import GuestMetadata
from server.revisions import fingerprint
from tests.test_review import png_bytes
from tests import test_variants as variant_tests


class RevisionTest(unittest.TestCase):
    setUp = variant_tests.VariantsTest.setUp
    save = variant_tests.VariantsTest.save
    add = variant_tests.VariantsTest.add
    ready_variant = variant_tests.VariantsTest.ready_variant

    def published(self):
        self.assertEqual(self.save(done=True).status_code, 200)
        return self.dataset / "vendor/type/sample"

    def reopen(self, client=None, item="sample"):
        client = client or self.client
        response = client.post(f"/api/items/{item}/rereview")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["revision"])
        return client.post(f"/api/items/{item}/prepare").json()

    def fresh_client(self):
        app = create_app(
            self.images, self.root / "fresh", self.catalog, dataset_dir=self.dataset
        )
        self.addCleanup(app.state.comparison.close)
        return TestClient(app)

    def init_mask(self, client, details, item="sample", variant=""):
        # The browser rasterizes the returned SVG. A silhouette fixture exercises
        # the same server initialization and subsequent editable-mask path.
        mask = Image.new("RGBA", (details["width"], details["height"]))
        for x in range(60, details["width"] - 60):
            for y in range(60, details["height"] - 60):
                mask.putpixel((x, y), (0, 0, 0, 255))
        response = client.post(
            f"/api/items/{item}/revision-mask?variant={variant}",
            files={"mask": ("mask.png", png_bytes(mask), "image/png")},
        )
        self.assertEqual(response.status_code, 204, response.text)
        return client.post(f"/api/items/{item}/prepare?variant={variant}").json()

    def test_local_cancel_restores_every_original_artifact(self):
        record = self.published()
        before = fingerprint(self.work / "sample")
        saved = fingerprint(record)
        details = self.reopen()
        self.assertEqual(details["state"]["main_length"], self.state["main_length"])
        self.assertEqual(details["state"]["rating"], "good")
        self.assertEqual(self.add().status_code, 200)
        self.assertEqual(fingerprint(record), saved)
        self.assertEqual(
            self.client.post("/api/items/sample/cancel-revision").status_code, 204
        )
        self.assertEqual(fingerprint(self.work / "sample"), before)
        self.assertEqual(fingerprint(record), saved)

    def test_vector_only_revision_keeps_paths_and_normalizes_vector(self):
        record = self.published()
        original = (record / "outline.svg").read_bytes()
        details = self.reopen()
        changed = details["state"] | {
            "main_length": {"start": [14, 19], "end": [18, 4]}
        }
        self.assertEqual(self.save(state=changed).status_code, 200)
        self.assertEqual((record / "outline.svg").read_bytes(), original)
        self.assertEqual(self.save(done=True, state=changed).status_code, 200)
        revised = record / "outline.svg"

        def paths(root):
            return [node.get("d") for node in root.iter() if node.tag.endswith("path")]

        self.assertEqual(
            paths(ET.fromstring(original)), paths(ET.parse(revised).getroot())
        )
        line = svg_main_length(revised)
        self.assertAlmostEqual(line.start[0], line.end[0])
        self.assertAlmostEqual(line.start[1] - line.end[1], 1)

    def test_source_free_vector_and_shape_editing(self):
        record = self.published()
        original = (record / "outline.svg").read_bytes()
        client = self.fresh_client()
        details = self.reopen(client)
        self.assertTrue(details["outline_mask"])
        details = self.init_mask(client, details)
        self.assertTrue(details["saved_outline"])
        changed = details["state"] | {
            "main_length": {
                "start": [details["width"] / 2, details["height"] - 60],
                "end": [details["width"] / 2 + 20, 60],
            }
        }
        response = client.post(
            "/api/items/sample/save",
            data={"state_json": json.dumps(changed | {"status": "done"})},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual((record / "outline.svg").read_bytes(), original)

        def paths(root):
            return [n.get("d") for n in root.iter() if n.tag.endswith("path")]

        self.assertEqual(
            paths(ET.fromstring(original)),
            paths(ET.parse(record / "outline.svg").getroot()),
        )
        # Brush edits use normal tracing; no reference image or rembg is needed.
        details = self.reopen(client)
        paint = Image.new("RGBA", (details["width"], details["height"]))
        for x in range(60, 110):
            for y in range(60, 110):
                paint.putpixel((x, y), (0, 0, 0, 255))
        response = client.post(
            "/api/items/sample/save",
            data={"state_json": json.dumps(details["state"] | {"status": "done"})},
            files={"edits": ("paint.png", png_bytes(paint), "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(
            paths(ET.parse(record / "outline.svg").getroot()),
            paths(ET.fromstring(original)),
        )

    def test_untouched_source_free_sibling_survives_photo_replacement(self):
        self.save()
        self.ready_variant()
        self.save("medium-large", done=True)
        record = self.dataset / "vendor/type/sample"
        old = (record / "variants/medium-large.svg").read_bytes()
        client = self.fresh_client()
        self.reopen(client)
        response = client.post(
            "/api/items/sample/alternative",
            files={"image": ("photo.png", self.mask, "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.save(done=True, client=client).status_code, 200)
        self.assertEqual((record / "variants/medium-large.svg").read_bytes(), old)

    def test_changed_saved_result_blocks_revision_commit(self):
        record = self.published()
        self.reopen()
        metadata = record / "metadata.json"
        saved = json.loads(metadata.read_text()) | {"quality": "bad_perspective"}
        metadata.write_text(json.dumps(saved))
        response = self.save(done=True)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(json.loads(metadata.read_text()), saved)

    def hosted_client(self, name, reviewer=False):
        if not hasattr(self, "store"):
            self.store = HostedStore(self.root / "state.sqlite3")
            self.hosted_app = create_app(
                self.images,
                self.work,
                self.catalog,
                dataset_dir=self.dataset,
                hosted_store=self.store,
                pending_dir=self.root / "pending",
                secure_cookies=False,
            )
            self.addCleanup(self.hosted_app.state.comparison.close)
        token = self.store.create_invite(name, reviewer)
        client = TestClient(self.hosted_app)
        client.post(f"/invite/{token}")
        return client

    def test_pending_revision_retains_contributor_and_is_not_approved(self):
        alice = self.hosted_client("Alice")
        alice.post("/api/items/sample/prepare")
        self.assertEqual(self.save(done=True, client=alice).status_code, 200)
        pending = self.root / "pending/sample"
        before = fingerprint(pending)
        self.assertEqual(alice.post("/api/items/sample/rereview").status_code, 403)
        moderator = self.hosted_client("Moderator", True)
        details = self.reopen(moderator)
        details = self.init_mask(moderator, details)
        self.assertEqual(
            moderator.post("/api/moderation/submissions/sample/approve").status_code,
            409,
        )
        self.assertEqual(
            moderator.post("/api/moderation/submissions/sample/reject").status_code, 409
        )
        changed = details["state"] | {
            "main_length": {"start": [500, 1000], "end": [550, 100]}
        }
        response = self.save(done=True, state=changed, client=moderator)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(fingerprint(pending), before)
        self.assertEqual(self.store.submission("sample")["user_name"], "Alice")
        self.assertFalse((self.dataset / "vendor/type/sample/metadata.json").exists())
        self.assertEqual(
            moderator.post("/api/moderation/submissions/sample/approve").status_code,
            204,
        )

    def test_approved_revision_stays_published_until_approval(self):
        record = self.published()
        before = fingerprint(record)
        alice = self.hosted_client("Alice")
        details = self.reopen(alice)
        changed = details["state"] | {
            "main_length": {"start": [14, 20], "end": [17, 4]}
        }
        response = self.save(done=True, state=changed, client=alice)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(fingerprint(record), before)
        moderator = self.hosted_client("Moderator", True)
        self.assertEqual(
            moderator.post("/api/moderation/submissions/sample/approve").status_code,
            204,
        )
        self.assertNotEqual(fingerprint(record), before)

    def independent(self):
        catalog = self.published()
        directory = self.dataset / "vendor/type/independent"
        directory.mkdir(parents=True)
        metadata = GuestMetadata(
            vendor="Vendor",
            product_type="Type",
            name="Independent",
            sizes=[
                {"label": "Small", "short_label": "S", "length": 4, "unit": "in"},
                {"label": "Medium", "short_label": "M", "length": 6, "unit": "in"},
            ],
        )
        record = self.workspace.independent_document("community:independent", metadata)
        (directory / "metadata.json").write_text(json.dumps(record))
        shutil.copyfile(catalog / "outline.svg", directory / "outline.svg")
        return directory, metadata

    def test_independent_metadata_variants_and_outline_revision(self):
        directory, metadata = self.independent()
        before = fingerprint(directory)
        details = self.reopen(item="community:independent")
        self.assertTrue(details["outline_mask"])
        response = self.client.post(
            "/api/items/community:independent/revision-metadata",
            json=metadata.model_dump(mode="json") | {"notes": "Revised metadata"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(
            "/api/items/community:independent/variants",
            json={"action": "add", "id": "small", "sizes": ["Small"], "move_from": ""},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(fingerprint(directory), before)
        response = self.client.post(
            "/api/items/community:independent/save?variant=small",
            data={"state_json": json.dumps(details["state"] | {"status": "done"})},
        )
        self.assertEqual(response.status_code, 200, response.text)
        record = json.loads((directory / "metadata.json").read_text())
        self.assertEqual(record["notes"], "Revised metadata")
        self.assertEqual(record["variants"]["small"]["sizes"], ["Small"])
        self.assertFalse((directory / "outline.svg").exists())
        self.assertTrue((directory / "variants/small.svg").exists())

    def test_independent_hosted_revision_and_approval(self):
        directory, metadata = self.independent()
        before = fingerprint(directory)
        alice = self.hosted_client("Alice")
        details = self.reopen(alice, item="community:independent")
        response = alice.post(
            "/api/items/community:independent/revision-metadata",
            json=metadata.model_dump(mode="json")
            | {"name": "Renamed", "notes": "Keep me"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = alice.post(
            "/api/items/community:independent/save",
            data={"state_json": json.dumps(details["state"] | {"status": "done"})},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(fingerprint(directory), before)
        moderator = self.hosted_client("Moderator", True)
        response = moderator.post(
            "/api/moderation/submissions/community:independent/approve"
        )
        self.assertEqual(response.status_code, 204, response.text)
        renamed = self.dataset / "vendor/type/renamed"
        self.assertEqual(
            json.loads((renamed / "metadata.json").read_text())["notes"], "Keep me"
        )
        self.assertEqual(
            (renamed / "outline.svg").read_bytes(),
            (directory / "outline.svg").read_bytes()
            if directory.exists()
            else (self.dataset / "vendor/type/sample/outline.svg").read_bytes(),
        )

    def test_completed_work_without_dataset_uses_revision_and_cancel(self):
        self.published()
        app = create_app(self.images, self.work, self.catalog)
        self.addCleanup(app.state.comparison.close)
        client = TestClient(app)
        listed = client.get("/api/items").json()["items"][0]
        self.assertTrue(listed["read_only"])
        self.assertTrue(listed["can_edit"])
        before = fingerprint(self.work / "sample")
        details = self.reopen(client)
        changed = details["state"] | {"rating": "bad_perspective"}
        self.assertEqual(self.save(state=changed, client=client).status_code, 200)
        self.assertEqual(
            client.post("/api/items/sample/cancel-revision").status_code, 204
        )
        self.assertEqual(fingerprint(self.work / "sample"), before)
        details = self.reopen(client)
        self.assertEqual(
            self.save(state=details["state"], done=True, client=client).status_code, 200
        )

    def test_failed_submission_update_restores_previous_bundle(self):
        alice = self.hosted_client("Alice")
        alice.post("/api/items/sample/prepare")
        self.save(done=True, client=alice)
        moderator = self.hosted_client("Moderator", True)
        details = self.reopen(moderator)
        before = fingerprint(self.root / "pending/sample")
        previous = dict(self.store.submission("sample"))
        with patch.object(
            self.store,
            "save_submission_revision",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                self.save(
                    done=True,
                    state=details["state"] | {"rating": "bad_perspective"},
                    client=moderator,
                )
        self.assertEqual(fingerprint(self.root / "pending/sample"), before)
        self.assertEqual(dict(self.store.submission("sample")), previous)
        self.assertTrue(moderator.get("/api/items").json()["items"][0]["revision"])
        self.assertEqual(
            self.save(done=True, state=details["state"], client=moderator).status_code,
            200,
        )

    def test_pending_independent_revision_keeps_metadata_and_reference_photo(self):
        from server.models import IndependentSubmission

        catalog = self.published()
        moderator = self.hosted_client("Moderator", True)
        user = self.store.user_for_session(
            moderator.cookies.get("silicone_shadows_session")
        )
        metadata = GuestMetadata(
            vendor="Vendor",
            product_type="Type",
            name="Independent",
            notes="Original notes",
            sizes=[{"label": "Small", "short_label": "S", "length": 4, "unit": "in"}],
        )
        submission = IndependentSubmission(
            metadata=metadata, main_length=self.state["main_length"]
        )
        pending = self.root / "pending/independent"
        pending.mkdir(parents=True)
        (pending / "metadata.json").write_text(
            json.dumps(
                {
                    "metadata": metadata.model_dump(mode="json"),
                    "main_length": self.state["main_length"],
                }
            )
        )
        shutil.copyfile(catalog / "outline.svg", pending / "outline.svg")
        (pending / "alternative.png").write_bytes(self.mask)
        self.store.put_submission(
            "independent",
            user,
            "alternative",
            submission.model_dump_json(),
            kind="independent",
        )
        details = self.reopen(moderator, item="independent")
        details = self.init_mask(moderator, details, item="independent")
        self.assertIsNotNone(details["reference_url"])
        self.assertEqual(moderator.get(details["reference_url"]).content, self.mask)
        response = moderator.post(
            "/api/items/independent/save",
            data={"state_json": json.dumps(details["state"] | {"status": "done"})},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((pending / "alternative.png").read_bytes(), self.mask)
        self.assertEqual(
            moderator.post(
                "/api/moderation/submissions/independent/approve"
            ).status_code,
            204,
        )
        published = self.dataset / "vendor/type/independent"
        self.assertEqual(
            json.loads((published / "metadata.json").read_text())["notes"],
            "Original notes",
        )

    def test_other_sessions_see_saved_variants_until_explicit_save(self):
        self.published()
        alice = self.hosted_client("Alice")
        bob = self.hosted_client("Bob")
        self.reopen(alice)
        self.assertEqual(self.add(client=alice).status_code, 200)
        working = alice.get("/api/items/sample/variants").json()["outlines"]
        original = bob.get("/api/items/sample/variants").json()["outlines"]
        self.assertEqual(len(working), 2)
        self.assertEqual(len(original), 1)
        self.assertTrue(bob.get("/api/items").json()["items"][0]["published"])

    def test_repeated_vector_revisions_do_not_accumulate_svg_wrappers(self):
        record = self.published()
        for tip in ([18, 4], [17, 6]):
            details = self.reopen()
            changed = details["state"] | {
                "main_length": {"start": [14, 19], "end": tip}
            }
            self.assertEqual(self.save(done=True, state=changed).status_code, 200)
        svg = ET.parse(record / "outline.svg").getroot()
        self.assertEqual(
            sum(node.get("id") == "revision-orientation" for node in svg.iter()), 1
        )
        line = svg_main_length(record / "outline.svg")
        self.assertAlmostEqual(line.start[0], line.end[0])
        self.assertAlmostEqual(line.start[1] - line.end[1], 1)

    def test_source_free_canvas_includes_vector_outside_outline_bounds(self):
        original_state = self.state | {
            "main_length": {"start": [16, 23.5], "end": [16, 0.5]}
        }
        self.assertEqual(self.save(done=True, state=original_state).status_code, 200)
        record = self.dataset / "vendor/type/sample"
        original = (record / "outline.svg").read_bytes()
        client = self.fresh_client()
        details = self.reopen(client)
        for x, y in details["state"]["main_length"].values():
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x, details["width"])
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(y, details["height"])
        self.assertEqual(
            self.save(done=True, state=details["state"], client=client).status_code, 200
        )
        self.assertEqual((record / "outline.svg").read_bytes(), original)
