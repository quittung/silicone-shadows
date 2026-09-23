import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.artifacts import atomic_json
from server.catalog import ensure_catalog
from server.catalog_updates import CatalogUpdates, selected_source
from server.hosted import HostedStore
from server.workspace import Workspace


class CatalogUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "images"
        self.input.mkdir()
        self.source = self.root / "repo" / "catalog_source.json"
        self.config = {"provider": "fantasytoybox", "version": 7,
                       "url_template": "https://example.test/products_v{version}.json"}
        atomic_json(self.source, self.config)
        self.product = {"id": 1, "n": "Old", "vn": "Vendor", "pt": "Type", "pic": "images/one.jpg"}
        self.products = self.root / "products_v7.json"
        self.products.write_text(json.dumps([self.product]))
        self.workspace = Workspace(self.input, self.root / "work", self.products,
                                   "https://example.test/", self.root / "dataset")
        self.updates = CatalogUpdates(self.workspace, self.source, self.products)

    def discover(self, products):
        with patch("server.catalog_updates.latest_catalog", return_value=(8, [self.product], products)):
            return self.updates.check(force=True)

    def test_daily_check_failure_and_restart_cache(self):
        with patch("server.catalog_updates.latest_catalog", return_value=(8, [], [self.product])) as fetch:
            self.updates.check()
            self.updates.check()
            restarted = CatalogUpdates(self.workspace, self.source, self.products)
            restarted.check()
            self.assertEqual(fetch.call_count, 1)
        with patch("server.catalog_updates.latest_catalog", side_effect=OSError("offline")):
            state = self.updates.check(force=True)
        self.assertEqual(state["latest_version"], 8)
        self.assertEqual(state["error"], "offline")
        self.assertEqual(self.workspace.catalog_version, 7)

    def test_update_keeps_review_by_id_and_persists_across_deployment(self):
        directory = self.workspace.record_paths[1]
        atomic_json(directory / "metadata.json", {"catalog_id": 1, "quality": "unusable"})
        renamed = {**self.product, "n": "New"}
        added = {**self.product, "id": 2, "pic": "images/two.jpg"}
        self.discover([renamed, added])
        self.updates.apply()
        self.assertEqual(self.workspace.catalog_version, 8)
        self.assertEqual(self.workspace.published_record(renamed)[1], directory)
        self.assertNotEqual(self.workspace.record_paths[2], directory)
        self.assertEqual(json.loads(self.source.read_text())["version"], 7)
        selected = selected_source(self.source, self.root)
        self.assertEqual(json.loads(selected.read_text())["version"], 8)
        with patch("server.catalog.download_catalog", side_effect=AssertionError("must use cache")):
            products = ensure_catalog(selected)
        restarted = Workspace(self.input, self.root / "work", products,
                              dataset_dir=self.root / "dataset")
        self.assertEqual(restarted.catalog_version, 8)
        self.assertEqual(restarted.published_record(renamed)[1], directory)
        self.assertNotEqual(restarted.record_paths[2], directory)
        atomic_json(self.source, {**self.config, "version": 9})
        self.assertEqual(selected_source(self.source, self.root), self.source)

    def test_changed_image_cannot_orphan_unfinished_work(self):
        paths = self.workspace.paths("one")
        atomic_json(paths["metadata"], {"status": "pending"})
        self.discover([{**self.product, "pic": "images/replacement.jpg"}])
        with self.assertRaisesRegex(ValueError, "Finish or discard"):
            self.updates.apply()
        self.assertEqual(self.workspace.catalog_version, 7)
        self.assertEqual(json.loads(self.updates.source.read_text())["version"], 7)
        self.assertTrue(paths["metadata"].exists())

    def test_completed_review_survives_image_change_and_clears_stale_prefetch(self):
        directory = self.workspace.record_paths[1]
        atomic_json(directory / "metadata.json", {"catalog_id": 1, "quality": "unusable"})
        paths = self.workspace.paths("one")
        atomic_json(paths["metadata"], {"status": "done", "rating": "unusable"})
        paths["source"].write_bytes(b"cached")
        self.workspace.catalog_sources["one"].write_bytes(b"cached")
        self.workspace.select_prefetch(0, ["one"])
        replacement = {**self.product, "pic": "images/replacement.jpg"}
        self.discover([replacement])
        self.updates.apply()
        self.assertIsNotNone(self.workspace.published_record(replacement))
        self.assertNotIn("one", self.workspace.queue_items())
        self.assertEqual(self.workspace._prefetch_ids[0], [])
        self.assertFalse(paths["source"].exists())
        selected = selected_source(self.source, self.root)
        restarted = CatalogUpdates(self.workspace, selected, self.updates.cache(8))
        with patch("server.catalog_updates.latest_catalog") as fetch:
            restarted.check()
            fetch.assert_not_called()

    def test_invalid_candidate_does_not_change_active_catalog(self):
        self.discover([{**self.product, "pic": "bad.txt"}])
        with self.assertRaises(ValueError):
            self.updates.apply()
        self.assertEqual(self.workspace.catalog_version, 7)
        self.assertEqual(json.loads(self.updates.source.read_text())["version"], 7)

    def test_controls_are_reviewer_only_and_apply_without_restart(self):
        store = HostedStore(self.root / "state.sqlite3")
        app = create_app(self.input, self.root / "work", self.products,
                         dataset_dir=self.root / "dataset", hosted_store=store,
                         pending_dir=self.root / "pending", catalog_source=self.source)
        client = TestClient(app)
        self.assertEqual(client.get("/api/catalog").status_code, 401)
        for reviewer in (False, True):
            token = store.create_invite(f"user-{reviewer}", reviewer)
            session, _ = store.redeem_invite(token)
            client.cookies.set("silicone_shadows_session", session)
            if not reviewer:
                for path in ("check", "update"):
                    self.assertEqual(client.post(f"/api/catalog/{path}").status_code, 403)
                continue
            with patch("server.catalog_updates.latest_catalog", return_value=(8, [], [self.product])):
                self.assertEqual(client.post("/api/catalog/check").json()["latest_version"], 8)
            response = client.post("/api/catalog/update")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["current_version"], 8)
            self.assertEqual(app.state.workspace.catalog_version, 8)


if __name__ == "__main__":
    unittest.main()
