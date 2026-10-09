import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from server import create_app
from server.hosted import HostedStore
from server.variants import paths, validate_variants
from tests.test_review import png_bytes


class VariantsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = self.root / "images"
        self.images.mkdir()
        Image.new("RGB", (32, 24), "white").save(self.images / "sample.jpg")
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
                        "sz": {
                            "s": [
                                {"sl": label, "ShortLabel": short, "len": length}
                                for label, short, length in [
                                    ("Small", "S", 4),
                                    ("Medium", "M", 6),
                                    ("Large", "L", 8),
                                ]
                            ]
                        },
                    }
                ]
            )
        )
        self.work = self.root / "work"
        self.dataset = self.root / "dataset"
        target = self.work / "sample"
        target.mkdir(parents=True)
        source = Image.new("RGB", (32, 24), "white")
        source.save(target / "source.png")
        mask = Image.new("RGBA", (32, 24), (0, 0, 0, 0))
        for x in range(8, 24):
            for y in range(3, 21):
                mask.putpixel((x, y), (100, 100, 100, 255))
        mask.save(target / "rembg.png")
        self.mask = png_bytes(mask)
        self.state = {
            "status": "pending",
            "rating": "good",
            "main_length": {"start": [16, 20], "end": [16, 3]},
        }
        self.app = create_app(
            self.images, self.work, self.catalog, dataset_dir=self.dataset
        )
        self.workspace = self.app.state.workspace
        self.patcher = patch.object(
            type(self.workspace), "remove_background", return_value=self.mask
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = TestClient(self.app)

    def save(self, variant="", done=False, client=None, state=None):
        return (client or self.client).post(
            f"/api/items/sample/save?variant={variant}",
            data={
                "state_json": json.dumps(
                    (state or self.state) | {"status": "done" if done else "pending"}
                )
            },
        )

    def add(self, key="medium-large", sizes=None, client=None, **kwargs):
        return (client or self.client).post(
            "/api/items/sample/variants",
            json={
                "action": "add",
                "id": key,
                "sizes": sizes or ["Medium", "Large"],
                **kwargs,
            },
        )

    def ready_variant(self, key="medium-large", client=None):
        client = client or self.client
        self.assertEqual(self.add(key, client=client).status_code, 200)
        blank = client.post(f"/api/items/sample/prepare?variant={key}")
        self.assertEqual(blank.status_code, 200)
        self.assertTrue(blank.json()["blank"])
        self.assertEqual(
            client.post(
                f"/api/items/sample/alternative?variant={key}",
                files={"image": ("variant.png", self.mask, "image/png")},
            ).status_code,
            200,
        )
        self.assertEqual(self.save(key, client=client).status_code, 200)

    def test_size_labels_use_catalog_shorthand(self):
        response = self.client.get("/api/items/sample/variants")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["size_labels"], {"Small": "S", "Medium": "M", "Large": "L"}
        )

    def test_grouped_variant_and_general_resolve_to_different_files(self):
        self.assertEqual(self.save().status_code, 200)
        self.ready_variant()
        self.assertEqual(self.save("medium-large", done=True).status_code, 200)
        metadata = json.loads(
            (self.dataset / "vendor/type/sample/metadata.json").read_text()
        )
        self.assertEqual(metadata["schema_version"], 2)
        self.assertEqual(
            metadata["variants"]["medium-large"]["sizes"], ["Medium", "Large"]
        )
        product = self.client.get("/api/comparison/products").json()["products"][0]
        small, medium, large = product["sizes"]
        self.assertNotEqual(small["svg_url"], medium["svg_url"])
        self.assertEqual(medium["svg_url"], large["svg_url"])
        self.assertEqual(self.client.get(medium["svg_url"]).status_code, 200)
        # Read-only editor lists all outlines, with individual previews.
        entries = self.client.get("/api/items/sample/variants").json()["outlines"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(self.client.get(entries[1]["preview_url"]).status_code, 200)

    def test_move_general_to_specific_and_only_expose_assigned_sizes(self):
        self.assertEqual(self.save().status_code, 200)
        response = self.add(move_from="")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["general"])
        self.assertEqual(self.save("medium-large", done=True).status_code, 200)
        directory = self.dataset / "vendor/type/sample"
        self.assertFalse((directory / "outline.svg").exists())
        self.assertEqual(
            json.loads((directory / "metadata.json").read_text())["quality"], "unusable"
        )
        product = self.client.get("/api/comparison/products").json()["products"][0]
        self.assertEqual([s["label"] for s in product["sizes"]], ["M", "L"])

    def test_duplicate_unknown_and_last_outline_are_rejected(self):
        self.assertEqual(self.add().status_code, 200)
        self.assertEqual(self.add("other", ["Medium"]).status_code, 400)
        self.assertEqual(self.add("unknown", ["XXL"]).status_code, 400)
        self.assertEqual(self.add("../unsafe", ["Small"]).status_code, 400)
        self.assertEqual(
            self.client.post(
                "/api/items/sample/variants", json={"action": "remove", "id": ""}
            ).status_code,
            200,
        )
        target = paths(self.workspace, "sample", "medium-large")["directory"]
        self.assertEqual(
            self.client.post(
                "/api/items/sample/variants",
                json={"action": "remove", "id": "medium-large"},
            ).status_code,
            400,
        )
        self.assertTrue(target.is_dir())

    def test_incomplete_sibling_blocks_publication_and_preserved_variant_survives_rereview(
        self,
    ):
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.add().status_code, 200)
        self.assertEqual(self.save(done=True).status_code, 400)
        self.assertFalse((self.dataset / "vendor/type/sample/metadata.json").exists())
        self.assertEqual(
            self.client.post(
                "/api/items/sample/prepare?variant=medium-large"
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/api/items/sample/alternative?variant=medium-large",
                files={"image": ("variant.png", self.mask, "image/png")},
            ).status_code,
            200,
        )
        self.assertEqual(self.save("medium-large", done=True).status_code, 200)
        old = (
            self.dataset / "vendor/type/sample/variants/medium-large.svg"
        ).read_bytes()
        # A fresh checkout lacks the contributor's source photos and editable masks.
        fresh = create_app(
            self.images, self.root / "fresh", self.catalog, dataset_dir=self.dataset
        )
        client = TestClient(fresh)
        self.assertEqual(client.post("/api/items/sample/rereview").status_code, 200)
        prepared = client.post("/api/items/sample/prepare?variant=medium-large")
        self.assertEqual(prepared.status_code, 200)
        self.assertTrue(prepared.json()["preview_only"])
        client.post("/api/items/sample/prepare")
        self.assertEqual(self.save(done=True, client=client).status_code, 200)
        self.assertEqual(
            (
                self.dataset / "vendor/type/sample/variants/medium-large.svg"
            ).read_bytes(),
            old,
        )

    def hosted_clients(self):
        store = HostedStore(self.root / "state.sqlite3")
        app = create_app(
            self.images,
            self.work,
            self.catalog,
            dataset_dir=self.dataset,
            hosted_store=store,
            pending_dir=self.root / "pending",
            secure_cookies=False,
        )
        contributor = TestClient(app)
        token = store.create_invite("contributor")
        contributor.post(f"/invite/{token}")
        reviewer = TestClient(app)
        token = store.create_invite("reviewer", True)
        reviewer.post(f"/invite/{token}")
        return contributor, reviewer, store

    def test_hosted_accept_and_reject_apply_to_whole_product(self):
        contributor, reviewer, store = self.hosted_clients()
        self.assertEqual(contributor.post("/api/items/sample/prepare").status_code, 200)
        self.assertEqual(self.save(client=contributor).status_code, 200)
        self.ready_variant(client=contributor)
        response = self.save("medium-large", done=True, client=contributor)
        self.assertEqual(response.status_code, 200, response.text)
        submission = reviewer.get("/api/moderation/submissions").json()["submissions"][
            0
        ]
        self.assertEqual(len(submission["variants"]), 1)
        self.assertEqual(
            reviewer.get(submission["variants"][0]["outline_url"]).status_code, 200
        )
        response = reviewer.post("/api/moderation/submissions/sample/approve")
        self.assertEqual(response.status_code, 204, response.text)
        self.assertTrue(
            (self.dataset / "vendor/type/sample/variants/medium-large.svg").exists()
        )
        self.assertIsNone(store.submission("sample"))
        # A later rejected collection does not replace the approved product.
        self.assertEqual(
            contributor.post("/api/items/sample/rereview").status_code, 200
        )
        self.assertEqual(contributor.post("/api/items/sample/prepare").status_code, 200)
        self.assertEqual(self.save(done=True, client=contributor).status_code, 200)
        self.assertEqual(
            reviewer.post("/api/moderation/submissions/sample/reject").status_code, 204
        )
        self.assertTrue(
            (self.dataset / "vendor/type/sample/variants/medium-large.svg").exists()
        )
        self.assertFalse((self.root / "pending/sample").exists())

    def test_moderation_overrides_each_outline_quality(self):
        contributor, reviewer, store = self.hosted_clients()
        contributor.post("/api/items/sample/prepare")
        self.save(client=contributor)
        self.ready_variant(client=contributor)
        self.save("medium-large", done=True, client=contributor)
        response = reviewer.post(
            "/api/moderation/submissions/sample/approve", json={"unknown": "good"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(store.submission("sample"))
        response = reviewer.post(
            "/api/moderation/submissions/sample/approve",
            json={"": "bad_perspective", "medium-large": "unusable"},
        )
        self.assertEqual(response.status_code, 204, response.text)
        record = json.loads(
            (self.dataset / "vendor/type/sample/metadata.json").read_text()
        )
        self.assertEqual(record["quality"], "bad_perspective")
        self.assertEqual(record["variants"]["medium-large"]["quality"], "unusable")
        self.assertTrue((self.dataset / "vendor/type/sample/outline.svg").exists())
        self.assertFalse(
            (self.dataset / "vendor/type/sample/variants/medium-large.svg").exists()
        )
        reviewer.post("/api/comparison/reload")
        sizes = reviewer.get("/api/comparison/products").json()["products"][0]["sizes"]
        self.assertEqual([size["label"] for size in sizes], ["S"])

    def test_moderation_without_fallback_omits_root_source(self):
        contributor, reviewer, _ = self.hosted_clients()
        contributor.post("/api/items/sample/prepare")
        self.save(client=contributor)
        self.add(move_from="", client=contributor)
        self.save("medium-large", done=True, client=contributor)
        submission = reviewer.get("/api/moderation/submissions").json()["submissions"][
            0
        ]
        self.assertIsNone(submission["outline_url"])
        self.assertIsNone(submission["source_url"])
        self.assertEqual(submission["variants"][0]["id"], "medium-large")
        response = reviewer.post(
            "/api/moderation/submissions/sample/approve", json={"": "good"}
        )
        self.assertEqual(response.status_code, 400)
        response = reviewer.post(
            "/api/moderation/submissions/sample/approve",
            json={"medium-large": "bad_perspective"},
        )
        self.assertEqual(response.status_code, 204)
        record = json.loads(
            (self.dataset / "vendor/type/sample/metadata.json").read_text()
        )
        self.assertEqual(
            record["variants"]["medium-large"]["quality"], "bad_perspective"
        )

    def test_variant_validation_rejects_duplicate_assignments(self):
        with self.assertRaises(ValueError):
            validate_variants(
                {"m": {"sizes": ["Medium"]}, "ml": {"sizes": ["Medium", "Large"]}}
            )

    def test_download_contains_variants_and_keeps_review_editable(self):
        import zipfile
        from io import BytesIO

        self.assertEqual(self.save().status_code, 200)
        self.ready_variant()
        prior = (self.work / "sample/metadata.json").read_bytes()
        response = self.client.post(
            "/api/items/sample/save?variant=medium-large",
            data={
                "state_json": json.dumps(self.state | {"status": "done"}),
                "download_only": "true",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(BytesIO(response.content)) as bundle:
            self.assertIn("outline.svg", bundle.namelist())
            self.assertIn("variants/medium-large.svg", bundle.namelist())
            self.assertEqual(
                json.loads(bundle.read("metadata.json"))["variants"]["medium-large"][
                    "sizes"
                ],
                ["Medium", "Large"],
            )
        self.assertEqual((self.work / "sample/metadata.json").read_bytes(), prior)
        self.assertFalse((self.dataset / "vendor/type/sample/metadata.json").exists())

    def test_failed_download_restores_draft_metadata(self):
        self.save()
        self.add()
        prior = (self.work / "sample/metadata.json").read_bytes()
        response = self.client.post(
            "/api/items/sample/save",
            data={
                "state_json": json.dumps(self.state | {"status": "done"}),
                "download_only": "true",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual((self.work / "sample/metadata.json").read_bytes(), prior)

    def test_promote_specific_outline_to_general_and_remove_assignment(self):
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.add(move_from="").status_code, 200)
        response = self.client.post(
            "/api/items/sample/variants",
            json={"action": "general", "move_from": "medium-large"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["general"])
        self.assertEqual([entry["id"] for entry in response.json()["outlines"]], [""])
        self.assertEqual(self.save(done=True).status_code, 200)
        self.assertEqual(
            len(
                self.client.get("/api/comparison/products").json()["products"][0][
                    "sizes"
                ]
            ),
            3,
        )

    def test_release_validation_accepts_mixed_versions_and_rejects_orphan_variants(
        self,
    ):
        import shutil
        from admin import release_dataset

        self.assertEqual(self.save().status_code, 200)
        self.ready_variant()
        self.assertEqual(self.save("medium-large", done=True).status_code, 200)
        directory = self.dataset / "vendor/type/sample"
        legacy = self.dataset / "vendor/type/legacy"
        legacy.mkdir()
        (legacy / "metadata.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog_id": 2,
                    "quality": "good",
                    "source": "catalog",
                }
            )
        )
        shutil.copyfile(directory / "outline.svg", legacy / "outline.svg")
        (self.root / "catalog_source.json").write_text(
            json.dumps(
                {
                    "provider": "fantasytoybox",
                    "version": 1,
                    "url_template": "https://example.com/products_v{version}.json",
                }
            )
        )
        files = sorted(self.dataset.rglob("*.json")) + sorted(
            self.dataset.rglob("*.svg")
        )
        with (
            patch.object(release_dataset, "ROOT", self.root),
            patch.object(release_dataset, "git", return_value="test"),
        ):
            manifest = release_dataset.build_manifest("v1", files)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["records"]["total"], 2)
            extra = directory / "variants/orphan.svg"
            shutil.copyfile(directory / "outline.svg", extra)
            with self.assertRaises(ValueError):
                release_dataset.build_manifest("v1", files + [extra])

    def test_new_fallback_outline_starts_blank(self):
        self.save()
        self.add(move_from="")
        response = self.client.post(
            "/api/items/sample/variants", json={"action": "general"}
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/api/items/sample/prepare")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["blank"])
        self.assertFalse((self.work / "sample/source.png").exists())
        self.assertTrue(
            paths(self.workspace, "sample", "medium-large")["source"].exists()
        )

    def test_new_variant_stays_blank_and_size_assignment_is_editable(self):
        self.assertEqual(self.save().status_code, 200)
        general = (self.work / "sample/source.png").read_bytes()
        self.assertEqual(self.add("medium", ["Medium"]).status_code, 200)
        response = self.client.post("/api/items/sample/prepare?variant=medium")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["blank"])
        target = paths(self.workspace, "sample", "medium")
        self.assertFalse(target["source"].exists())
        self.assertFalse(target["rembg"].exists())
        response = self.client.post(
            "/api/items/sample/variants",
            json={"action": "assign", "id": "medium", "sizes": ["Large"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outlines"][1]["sizes"], ["Large"])
        self.assertEqual((self.work / "sample/source.png").read_bytes(), general)
        self.assertEqual(self.save("medium", done=True).status_code, 400)
