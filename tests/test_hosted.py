import json
import os
import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from server import create_app
from server.hosted import ClaimError, HostedStore, PublicQueue, QueueError
from server.models import ReviewState
from server.workspace import Workspace


def png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class HostedAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input_dir = self.root / "images"
        self.work_dir = self.root / "work"
        self.dataset_dir = self.root / "dataset"
        self.pending_dir = self.root / "pending"
        self.input_dir.mkdir()
        self.work_dir.mkdir()
        source = Image.new("RGB", (32, 24), "white")
        source.save(self.input_dir / "sample.jpg")
        item_dir = self.work_dir / "sample"
        item_dir.mkdir()
        source.save(item_dir / "source.png")
        alpha = np.zeros((24, 32, 4), dtype=np.uint8)
        alpha[4:20, 6:26, :3] = 100
        alpha[4:20, 6:26, 3] = 255
        Image.fromarray(alpha).save(item_dir / "rembg.png")
        self.catalog = self.root / "products.json"
        self.catalog.write_text(
            json.dumps(
                [
                    {
                        "id": 1,
                        "n": "Sample",
                        "vn": "Vendor",
                        "pt": "Type",
                        "pic": "images/sample.jpg",
                        "sp": "Dragon",
                        "tags": ["knot"],
                        "feat": {"sc": True},
                        "sz": {
                            "wl": "Knot",
                            "s": [
                                {
                                    "sl": "One size",
                                    "ShortLabel": "OS",
                                    "len": 6,
                                }
                            ],
                        },
                    }
                ]
            )
        )
        self.store = HostedStore(self.root / "state.sqlite3")
        self.app = create_app(
            self.input_dir,
            self.work_dir,
            self.catalog,
            dataset_dir=self.dataset_dir,
            hosted_store=self.store,
            pending_dir=self.pending_dir,
            secure_cookies=False,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def login(self, name: str, reviewer: bool = False) -> TestClient:
        token = self.store.create_invite(name, reviewer)
        client = TestClient(self.app)
        landing = client.get(f"/invite/{token}")
        self.assertIn("Accept invitation", landing.text)
        self.assertIn("CC0 1.0 Universal", landing.text)
        self.assertIn("Only submit material you have the right", landing.text)
        response = client.post(f"/invite/{token}", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        self.assertEqual(client.post(f"/invite/{token}").status_code, 400)
        return client

    def public_proof(self, client: TestClient) -> str:
        fake_rembg = types.ModuleType("rembg")
        fake_rembg.new_session = lambda: object()
        fake_rembg.remove = lambda data, session: data
        ticket = client.post("/api/public/queue").json()["ticket"]
        client.post("/api/public/queue/status", json={"ticket": ticket})
        image = png_bytes(Image.new("RGBA", (12, 10), (100, 50, 20, 255)))
        with patch.dict(sys.modules, {"rembg": fake_rembg}):
            response = client.post(
                "/api/public/rembg",
                data={"ticket": ticket},
                files={"image": ("private.png", image, "image/png")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.headers["x-archive-token"]

    def test_invites_claims_submission_and_approval(self) -> None:
        anonymous = TestClient(self.app)
        landing = anonymous.get("/")
        self.assertIn("Start an independent product", landing.text)
        self.assertEqual(landing.headers["cache-control"], "no-store")
        self.assertEqual(anonymous.get("/static/public.css").status_code, 200)
        self.assertEqual(anonymous.get("/static/public.js").status_code, 200)
        self.assertEqual(anonymous.get("/static/metadata.css").status_code, 200)
        self.assertEqual(anonymous.get("/static/metadata.js").status_code, 200)
        options = anonymous.get("/api/public/metadata-options").json()
        self.assertEqual(options["product_types"], ["Type"])
        self.assertEqual(options["features"], ["sc"])
        self.assertEqual(options["tags"], ["knot"])
        self.assertEqual(
            options["size_labels"],
            [{"label": "One size", "short_label": "OS"}],
        )
        self.assertEqual(
            anonymous.get("/editor", follow_redirects=False).status_code, 303
        )
        self.assertEqual(anonymous.get("/api/items").status_code, 401)

        alice = self.login("Alice")
        bob = self.login("Bob")
        moderator = self.login("Moderator", reviewer=True)
        self.assertEqual(alice.get("/api/session").json()["user"]["name"], "Alice")
        self.assertIn("canvas", alice.get("/editor").text)
        self.assertEqual(alice.get("/review").status_code, 404)
        self.assertEqual(alice.get("/moderate").status_code, 403)
        self.assertEqual(moderator.get("/moderate").status_code, 200)
        self.assertEqual(alice.get("/stats").status_code, 200)
        self.assertEqual(alice.get("/compare").status_code, 200)

        item = alice.get("/api/items").json()["items"][0]
        self.assertEqual(item["workflow_status"], "never_worked")
        selected = alice.post("/api/prefetch", json={"item_ids": ["sample"]})
        self.assertEqual(selected.json(), {"selected": 1})
        prepared = alice.post("/api/items/sample/prepare")
        self.assertEqual(prepared.status_code, 200, prepared.text)
        self.assertIsNotNone(prepared.json()["claim_expires_at"])
        self.assertEqual(alice.get("/api/items/sample/file/source").status_code, 200)
        self.assertEqual(bob.get("/api/items/sample/file/source").status_code, 409)
        conflict = bob.post("/api/items/sample/prepare")
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertIn("Alice", conflict.json()["detail"])
        self.assertEqual(bob.post("/api/items/sample/claim").status_code, 409)
        self.assertEqual(self.store.claims()["sample"]["name"], "Alice")
        self.assertEqual(
            alice.post("/api/items/sample/claim").status_code,
            200,
        )

        state = {
            "status": "done",
            "rating": "good",
            "alpha_threshold": 128,
            "main_length": {"start": [8, 18], "end": [22, 5]},
        }
        download_metadata = {
            "submission_version": 1,
            "catalog_id": 1,
            "vendor": "Vendor",
            "product_type": "Type",
            "name": "Sample",
            "species": "Dragon",
            "quality": "good",
            "source": "catalog",
            "tags": ["knot"],
            "features": ["sc"],
            "sizes": [
                {
                    "label": "One size",
                    "short_label": "OS",
                    "length": 6,
                    "widest_label": "Knot",
                    "unit": "in",
                }
            ],
        }
        downloaded = alice.post(
            "/api/items/sample/save",
            data={
                "state_json": json.dumps(state),
                "download_only": "true",
                "metadata_json": json.dumps(download_metadata),
            },
        )
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        with zipfile.ZipFile(BytesIO(downloaded.content)) as archive:
            self.assertEqual(set(archive.namelist()), {"metadata.json", "outline.svg"})
            archived_metadata = json.loads(archive.read("metadata.json"))
        self.assertEqual(archived_metadata["tags"], ["knot"])
        self.assertEqual(archived_metadata["sizes"][0]["widest_label"], "Knot")
        self.assertIn("sample", self.store.claims())
        submitted = alice.post(
            "/api/items/sample/save",
            data={"state_json": json.dumps(state)},
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertTrue(submitted.json()["pending_review"])
        self.assertTrue((self.work_dir / "sample/source.png").is_file())
        self.assertTrue((self.work_dir / "sample/rembg.png").is_file())
        self.assertFalse((self.work_dir / "sample/metadata.json").exists())
        self.assertTrue((self.pending_dir / "sample/metadata.json").exists())
        self.assertTrue((self.pending_dir / "sample/outline.svg").exists())
        self.assertEqual(
            alice.get("/api/submissions/sample/outline.svg").status_code, 403
        )
        self.assertEqual(
            moderator.get("/api/submissions/sample/outline.svg").status_code, 200
        )
        self.assertFalse(
            (self.dataset_dir / "vendor/type/sample/metadata.json").exists()
        )
        self.assertEqual(bob.post("/api/items/sample/prepare").status_code, 409)
        summary = alice.get("/api/stats").json()["summary"]
        self.assertEqual(
            (
                summary["never_worked"],
                summary["pending_review"],
                summary["in_catalog"],
            ),
            (0, 1, 0),
        )

        pending = moderator.get("/api/moderation/submissions")
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertEqual(pending.json()["submissions"][0]["contributor"], "Alice")
        outline_url = pending.json()["submissions"][0]["outline_url"]
        preview = moderator.get(outline_url)
        self.assertEqual(preview.status_code, 200, preview.text)
        preview_root = ET.fromstring(preview.content)
        preview_line = next(
            element
            for element in preview_root.iter()
            if element.get("id") == "main-length"
        )
        self.assertEqual(preview_line.get("display"), "inline")
        self.assertTrue(
            any(
                element.get("id") == "main-length-tip"
                for element in preview_root.iter()
            )
        )
        stored_line = next(
            element
            for element in ET.parse(self.pending_dir / "sample/outline.svg")
            .getroot()
            .iter()
            if element.get("id") == "main-length"
        )
        self.assertEqual(stored_line.get("display"), "none")
        approved = moderator.post("/api/moderation/submissions/sample/approve")
        self.assertEqual(approved.status_code, 204, approved.text)
        record = json.loads(
            (self.dataset_dir / "vendor/type/sample/metadata.json").read_text()
        )
        self.assertEqual(
            set(record), {"schema_version", "catalog_id", "quality", "source"}
        )
        self.assertEqual(record["quality"], "good")
        self.assertTrue((self.dataset_dir / "vendor/type/sample/outline.svg").exists())
        self.assertFalse((self.pending_dir / "sample").exists())
        item = alice.get("/api/items").json()["items"][0]
        self.assertEqual(item["workflow_status"], "in_catalog")
        self.assertIn("show_length=true", item["svg_url"])
        catalog_preview = ET.fromstring(alice.get(item["svg_url"]).content)
        catalog_line = next(
            element
            for element in catalog_preview.iter()
            if element.get("id") == "main-length"
        )
        self.assertEqual(catalog_line.get("display"), "inline")
        self.assertEqual(catalog_line.get("stroke"), "#0440db")
        published_line = next(
            element
            for element in ET.parse(self.dataset_dir / "vendor/type/sample/outline.svg")
            .getroot()
            .iter()
            if element.get("id") == "main-length"
        )
        self.assertEqual(published_line.get("display"), "none")
        summary = alice.get("/api/stats").json()["summary"]
        self.assertEqual(
            (
                summary["never_worked"],
                summary["pending_review"],
                summary["in_catalog"],
            ),
            (0, 0, 1),
        )

        cached_rembg = (self.work_dir / "sample/rembg.png").read_bytes()
        with patch.object(
            self.app.state.workspace,
            "remove_background",
            side_effect=AssertionError("re-review should reuse the cached mask"),
        ):
            rereview = alice.post("/api/items/sample/rereview")
            self.assertEqual(rereview.status_code, 200, rereview.text)
            reopened = alice.post("/api/items/sample/prepare")
            self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertEqual(
            (self.work_dir / "sample/rembg.png").read_bytes(), cached_rembg
        )
        released = alice.post("/api/items/sample/release")
        self.assertEqual(released.status_code, 204, released.text)
        self.assertTrue((self.work_dir / "sample/source.png").is_file())
        self.assertTrue((self.work_dir / "sample/rembg.png").is_file())
        self.assertFalse((self.work_dir / "sample/metadata.json").exists())

    def test_abandoned_alternative_upload_is_deleted(self) -> None:
        alice = self.login("Alice")
        self.assertEqual(alice.post("/api/items/sample/prepare").status_code, 200)
        alternative = png_bytes(Image.new("RGB", (20, 16), "black"))
        masked = png_bytes(Image.new("RGBA", (20, 16), (0, 0, 0, 255)))
        with patch.object(
            self.app.state.workspace, "remove_background", return_value=masked
        ):
            uploaded = alice.post(
                "/api/items/sample/alternative",
                files={"image": ("alternative.png", alternative, "image/png")},
            )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertTrue((self.work_dir / "sample/alternative.png").is_file())
        self.assertEqual(alice.post("/api/items/sample/release").status_code, 204)
        self.assertFalse((self.work_dir / "sample").exists())

    def test_hosted_prefetch_is_fair_and_lru_protects_claims(self) -> None:
        root = self.root / "multi"
        input_dir = root / "images"
        work_dir = root / "work"
        dataset_dir = root / "dataset"
        pending_dir = root / "pending"
        input_dir.mkdir(parents=True)
        catalog_path = root / "products.json"
        catalog_path.write_text(
            json.dumps(
                [
                    {
                        "id": index,
                        "n": f"Item {index}",
                        "vn": "Vendor",
                        "pt": "Type",
                        "pic": f"images/item-{index}.png",
                    }
                    for index in range(6)
                ]
            )
        )
        for index in range(6):
            path = input_dir / f"item-{index}.png"
            Image.new("RGB", (8, 8), "white").save(path)
            os.utime(path, ns=(index + 1, index + 1))

        store = HostedStore(root / "state.sqlite3")
        workspace = Workspace(
            input_dir,
            work_dir,
            catalog_path,
            "https://images.example/",
            dataset_dir,
            store,
            pending_dir,
        )
        _, alice = store.redeem_invite(store.create_invite("Alice"))
        _, bob = store.redeem_invite(store.create_invite("Bob"))
        workspace.select_prefetch(alice.id, ["item-0", "item-1", "item-2"])
        workspace.select_prefetch(bob.id, ["item-3", "item-4", "item-5"])
        store.acquire_claim("item-1", bob)
        store.put_submission(
            "item-4",
            alice,
            "catalog",
            ReviewState(status="done", rating="unusable").model_dump_json(),
        )
        self.assertEqual(
            workspace._prefetch_window(),
            ["item-0", "item-3", "item-2", "item-5"],
        )

        workspace.select_prefetch(alice.id, [])
        workspace.select_prefetch(bob.id, [])
        with patch("server.workspace.PREFETCH_CACHE_LIMIT", 2):
            workspace._prune_catalog_cache()
        remaining = {
            item_id
            for item_id, path in workspace.catalog_sources.items()
            if path.exists()
        }
        self.assertEqual(remaining, {"item-1", "item-5"})

    def test_signed_in_independent_product_can_be_moderated(self) -> None:
        guest = TestClient(self.app)
        alice = self.login("Alice")
        moderator = self.login("Moderator", reviewer=True)
        mask = np.zeros((24, 32, 4), dtype=np.uint8)
        mask[4:20, 6:26] = 255
        image = png_bytes(Image.new("RGB", (32, 24), "white"))
        metadata = {
            "submission_version": 1,
            "vendor": "Example Maker",
            "product_type": "Type",
            "name": "Independent Product",
            "product_url": None,
            "species": "Dragon",
            "quality": "good",
            "source": "contributor_photo",
            "tags": ["knot"],
            "features": ["sc"],
            "sizes": [],
            "notes": None,
        }
        request = {
            "data": {"metadata_json": json.dumps(metadata)},
            "files": {
                "mask": ("mask.png", png_bytes(Image.fromarray(mask)), "image/png"),
                "source": ("source.png", image, "image/png"),
            },
        }
        self.assertEqual(guest.post("/api/public/submit", **request).status_code, 401)
        submitted = alice.post("/api/public/submit", **request)
        self.assertEqual(submitted.status_code, 200, submitted.text)
        item_id = submitted.json()["item_id"]
        self.assertEqual(self.store.submission(item_id)["kind"], "independent")
        self.assertTrue((self.pending_dir / item_id / "alternative.png").is_file())

        listing = moderator.get("/api/moderation/submissions").json()["submissions"]
        independent = next(item for item in listing if item["item_id"] == item_id)
        self.assertEqual(independent["kind"], "independent")
        self.assertEqual(independent["products"][0]["name"], "Independent Product")
        approved = moderator.post(f"/api/moderation/submissions/{item_id}/approve")
        self.assertEqual(approved.status_code, 204, approved.text)
        record_path = (
            self.dataset_dir / "example-maker/type/independent-product/metadata.json"
        )
        record = json.loads(record_path.read_text())
        self.assertIsNone(record["catalog_id"])
        self.assertEqual(record["record_id"], f"community:{item_id}")
        self.assertEqual(record["species"], "Dragon")
        self.assertEqual(record["features"], ["sc"])
        self.assertTrue(record_path.with_name("outline.svg").is_file())

        record_id = record["record_id"]
        listed = next(
            item
            for item in alice.get("/api/items").json()["items"]
            if item["id"] == record_id
        )
        self.assertTrue(listed["independent"])
        original_outline = record_path.with_name("outline.svg").read_bytes()
        updated_metadata = {
            **metadata,
            "catalog_id": None,
            "name": "Renamed Product",
        }
        update = alice.post(
            f"/api/community/{record_id}/metadata", json=updated_metadata
        )
        self.assertEqual(update.status_code, 200, update.text)
        update_id = update.json()["item_id"]
        pending_update = next(
            item
            for item in moderator.get("/api/moderation/submissions").json()[
                "submissions"
            ]
            if item["item_id"] == update_id
        )
        self.assertEqual(pending_update["kind"], "independent_update")
        self.assertIsNone(pending_update["source_url"])
        approved = moderator.post(f"/api/moderation/submissions/{update_id}/approve")
        self.assertEqual(approved.status_code, 204, approved.text)
        renamed_path = (
            self.dataset_dir / "example-maker/type/renamed-product/metadata.json"
        )
        self.assertEqual(json.loads(renamed_path.read_text())["record_id"], record_id)
        self.assertEqual(
            renamed_path.with_name("outline.svg").read_bytes(), original_outline
        )
        self.assertFalse(record_path.exists())

    def test_public_queue_processes_without_persistent_job_state(self) -> None:
        fake_rembg = types.ModuleType("rembg")
        fake_rembg.new_session = lambda: object()
        fake_rembg.remove = lambda data, session: data
        client = TestClient(self.app)
        queued = client.post("/api/public/queue")
        self.assertEqual(queued.status_code, 200, queued.text)
        ticket = queued.json()["ticket"]
        ready = client.post("/api/public/queue/status", json={"ticket": ticket})
        self.assertEqual(ready.json()["status"], "ready")
        image = png_bytes(Image.new("RGBA", (12, 10), (100, 50, 20, 255)))
        with patch.dict(sys.modules, {"rembg": fake_rembg}):
            response = client.post(
                "/api/public/rembg",
                data={"ticket": ticket},
                files={"image": ("private.png", image, "image/png")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, image)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(response.headers["x-archive-token"])
        expired = client.post("/api/public/queue/status", json={"ticket": ticket})
        self.assertEqual(expired.status_code, 404)
        self.assertEqual(list(self.root.rglob("private.png")), [])

    def test_public_archive_contains_guest_metadata_and_aligned_svg(self) -> None:
        mask = np.zeros((24, 32, 4), dtype=np.uint8)
        mask[4:20, 6:26] = 255
        metadata = {
            "submission_version": 1,
            "vendor": "Example Maker",
            "product_type": "Type",
            "name": "Example Product",
            "product_url": "https://example.com/product",
            "species": "Dragon",
            "quality": "good",
            "source": "contributor_photo",
            "tags": ["knot"],
            "features": ["sc"],
            "sizes": [
                {
                    "label": "One size",
                    "short_label": "OS",
                    "price": 50,
                    "length": 15,
                    "circumference": 10,
                    "widest_circumference": 12,
                    "widest_label": "Knot",
                    "unit": "cm",
                }
            ],
            "notes": "Guest submission",
        }
        client = TestClient(self.app)
        first_proof = self.public_proof(client)
        missing_line = client.post(
            "/api/public/archive",
            data={"proof": first_proof, "metadata_json": json.dumps(metadata)},
            files={"mask": ("mask.png", png_bytes(Image.fromarray(mask)), "image/png")},
        )
        self.assertEqual(missing_line.status_code, 400, missing_line.text)
        self.assertIn("usable-length line", missing_line.json()["detail"])

        proof = self.public_proof(client)
        response = client.post(
            "/api/public/archive",
            data={
                "proof": proof,
                "metadata_json": json.dumps(metadata),
                "main_length_json": json.dumps({"start": [8, 18], "end": [22, 5]}),
            },
            files={"mask": ("mask.png", png_bytes(Image.fromarray(mask)), "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            self.assertEqual(set(archive.namelist()), {"metadata.json", "outline.svg"})
            archived_metadata = json.loads(archive.read("metadata.json"))
            svg = ET.fromstring(archive.read("outline.svg"))
        self.assertEqual(archived_metadata["vendor"], "Example Maker")
        self.assertEqual(archived_metadata["sizes"][0]["unit"], "cm")
        self.assertEqual(archived_metadata["features"], ["sc"])
        self.assertIsNotNone(
            next(
                (element for element in svg.iter() if element.get("id") == "outline"),
                None,
            )
        )
        self.assertIsNotNone(
            next(
                (
                    element
                    for element in svg.iter()
                    if element.get("id") == "main-length"
                ),
                None,
            )
        )
        reused = client.post(
            "/api/public/archive",
            data={"proof": proof, "metadata_json": json.dumps(metadata)},
            files={"mask": ("mask.png", png_bytes(Image.fromarray(mask)), "image/png")},
        )
        self.assertEqual(reused.status_code, 403)

    def test_security_boundaries_reject_host_bypass_and_unsafe_items(self) -> None:
        anonymous = TestClient(self.app)
        bypass = anonymous.get(
            "/api/items", headers={"host": "example.com/?public=/api/public/"}
        )
        self.assertNotEqual(bypass.status_code, 200)
        permissive_host_app = create_app(
            self.input_dir,
            self.work_dir,
            self.catalog,
            dataset_dir=self.dataset_dir,
            hosted_store=self.store,
            pending_dir=self.pending_dir,
            secure_cookies=False,
            trusted_hosts=["*"],
        )
        raw_path_check = TestClient(permissive_host_app).get(
            "/api/items", headers={"host": "example.com/?public=/api/public/"}
        )
        self.assertEqual(raw_path_check.status_code, 401)

        alice = self.login("Alice")
        unknown = alice.post("/api/items/not-a-real-item/prepare")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(self.store.claims(), {})
        encoded_traversal = alice.post("/api/items/%2e%2e/prepare")
        self.assertNotEqual(encoded_traversal.status_code, 200)
        with self.assertRaises(HTTPException):
            self.app.state.workspace.paths("..")
        self.app.state.workspace.discard_work("..")
        self.assertTrue(self.store.path.is_file())
        self.assertTrue((self.work_dir / "sample/source.png").is_file())

        self.assertEqual(alice.post("/api/items/sample/prepare").status_code, 200)
        with patch("server.routes.reviews.MAX_IMAGE_PIXELS", 100):
            oversized = alice.post(
                "/api/items/sample/alternative",
                files={
                    "image": (
                        "large.png",
                        png_bytes(Image.new("RGB", (32, 24), "white")),
                        "image/png",
                    )
                },
            )
        self.assertEqual(oversized.status_code, 413)

        landing = anonymous.get("/")
        self.assertEqual(landing.headers["referrer-policy"], "no-referrer")
        self.assertEqual(landing.headers["x-frame-options"], "DENY")

    def test_sessions_can_be_revoked_by_name(self) -> None:
        alice = self.login("Alice")
        self.assertIsNotNone(alice.get("/api/session").json()["user"])
        self.assertEqual(self.store.revoke_sessions("Alice"), 1)
        self.assertIsNone(alice.get("/api/session").json()["user"])

    def test_claim_has_idle_and_absolute_expiration(self) -> None:
        with patch("server.hosted.time.time", return_value=1000):
            token = self.store.create_invite("Alice")
            _, user = self.store.redeem_invite(token)
            self.store.acquire_claim("sample", user)
        with patch("server.hosted.time.time", return_value=1179):
            self.store.heartbeat("sample", user)
        with patch("server.hosted.time.time", return_value=1360):
            with self.assertRaises(ClaimError):
                self.store.heartbeat("sample", user)

        with patch("server.hosted.time.time", return_value=2000):
            self.store.acquire_claim("sample", user)
        for now in (2170, 2340, 2510, 2680, 2850):
            with patch("server.hosted.time.time", return_value=now):
                self.store.heartbeat("sample", user)
        with patch("server.hosted.time.time", return_value=2900):
            with self.assertRaises(ClaimError):
                self.store.heartbeat("sample", user)

        with patch("server.hosted.time.time", return_value=3000):
            self.store.acquire_claim("sample", user)
        with patch("server.hosted.time.time", return_value=3181):
            self.assertEqual(self.store.claims(), {})
        with patch("server.hosted.time.time", return_value=3182):
            discarded, _ = self.store.acquire_claim("sample", user)
            self.assertEqual(discarded, ["sample"])

    def test_public_queue_rate_limit_is_in_memory(self) -> None:
        queue = PublicQueue()
        for _ in range(5):
            ticket = queue.create("192.0.2.10")["ticket"]
            queue.finish(ticket)
        with self.assertRaises(QueueError) as raised:
            queue.create("192.0.2.10")
        self.assertEqual(raised.exception.status_code, 429)
        self.assertNotEqual(
            queue.address_key("192.0.2.10"),
            queue.address_key("192.0.2.11"),
        )

    def test_processing_ticket_does_not_expire_at_heartbeat_timeout(self) -> None:
        queue = PublicQueue()
        with patch("server.hosted.time.time", return_value=1000):
            first = queue.create("192.0.2.10")["ticket"]
            queue.status(first, "192.0.2.10")
            queue.begin(first, "192.0.2.10")
        with patch("server.hosted.time.time", return_value=1031):
            second = queue.create("192.0.2.11")["ticket"]
            self.assertEqual(queue.status(second, "192.0.2.11")["status"], "queued")


if __name__ == "__main__":
    unittest.main()
