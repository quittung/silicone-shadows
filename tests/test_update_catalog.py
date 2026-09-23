import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admin import update_catalog
from server import catalog


class UpdateCatalogTest(unittest.TestCase):
    def test_finds_latest_catalog_and_reports_changes(self):
        catalogs = {
            7: [{"id": 1, "pic": "same"}],
            8: [{"id": 1, "pic": "same"}, {"id": 2, "pic": "new"}],
            9: [{"id": 1, "pic": "changed"}, {"id": 2, "pic": "new"}],
        }

        with patch.object(
            catalog,
            "fetch",
            side_effect=lambda version, _template, optional=False: catalogs.get(
                version
            ),
        ):
            version, current, latest = update_catalog.latest_catalog(7, "unused")

        self.assertEqual(version, 9)
        self.assertEqual(current, catalogs[7])
        with patch("builtins.print") as output:
            update_catalog.report(catalogs[7], latest, 7, version)
        self.assertEqual(
            output.call_args_list[-1].args[0],
            "Added 1; removed 0; changed 1; image paths changed 1",
        )

    def test_update_commits_only_catalog_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "catalog_source.json"
            config = {"version": 7, "url_template": "unused"}
            config_path.write_text(json.dumps(config))
            with (
                patch.object(update_catalog, "ROOT", root),
                patch.object(update_catalog, "CONFIG", config_path),
                patch.object(update_catalog.subprocess, "run") as run,
            ):
                run.return_value.returncode = 0
                update_catalog.update_and_commit(config, 9)

            self.assertEqual(json.loads(config_path.read_text())["version"], 9)
            self.assertIn("--only", run.call_args_list[-1].args[0])
            self.assertIn("catalog_source.json", run.call_args_list[-1].args[0])


if __name__ == "__main__":
    unittest.main()
