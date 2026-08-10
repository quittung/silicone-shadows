import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import release_dataset


class ReleaseDatasetTest(unittest.TestCase):
    def test_builds_data_only_archive_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, _checksum, notes = release_dataset.build("v0.0.0", Path(directory))
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                manifest = json.loads(bundle.read("manifest.json"))

            self.assertEqual(manifest["dataset_version"], "v0.0.0")
            self.assertEqual(manifest["license"], "CC0-1.0")
            self.assertEqual(manifest["rights_notice"], "dataset/NOTICE.md")
            self.assertIn(f"Records: {manifest['records']['total']}", notes.read_text())
            self.assertIn("Metadata format version: 1", notes.read_text())
            self.assertIn("Dataset dedication: CC0-1.0", notes.read_text())
            self.assertIn("dataset/LICENSE", names)
            self.assertIn("dataset/NOTICE.md", names)
            self.assertEqual(
                manifest["records"]["total"],
                sum(name.endswith("/metadata.json") for name in names),
            )
            self.assertTrue(all(name == "manifest.json" or name.startswith("dataset/") for name in names))
            release_dataset.verify_release_assets(
                "v0.0.0", Path(directory), manifest["git_commit"]
            )
            archive.write_bytes(archive.read_bytes() + b"corrupt")
            with self.assertRaises(RuntimeError):
                release_dataset.verify_release_assets(
                    "v0.0.0", Path(directory), manifest["git_commit"]
                )

    def test_rejects_non_semantic_version(self):
        with self.assertRaises(ValueError):
            release_dataset.build("latest", Path("unused"))


if __name__ == "__main__":
    unittest.main()
