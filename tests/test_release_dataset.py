import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from admin import release_dataset


class ReleaseDatasetTest(unittest.TestCase):
    def test_builds_and_verifies_data_only_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, notes = release_dataset.build("v0.0.0", Path(directory))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
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
            self.assertTrue(
                all(
                    name == "manifest.json" or name.startswith("dataset/")
                    for name in names
                )
            )
            release_dataset.verify_release_assets(
                "v0.0.0", Path(directory), manifest["git_commit"], digest
            )
            archive.write_bytes(archive.read_bytes() + b"corrupt")
            with self.assertRaises(RuntimeError):
                release_dataset.verify_release_assets(
                    "v0.0.0", Path(directory), manifest["git_commit"], digest
                )

    def test_rejects_unsafe_release_name(self):
        with self.assertRaises(ValueError):
            release_dataset.build("../latest", Path("unused"))

    def test_auto_increments_integer_and_legacy_versions(self):
        releases = [
            {"tagName": "v0.1.0"},
            {"tagName": "v0.6.0"},
            {"tagName": "special-snapshot"},
        ]
        with patch(
            "admin.release_dataset.subprocess.check_output",
            return_value=json.dumps(releases),
        ):
            self.assertEqual(release_dataset.next_release_name(), "v7")
        releases.append({"tagName": "v9"})
        with patch(
            "admin.release_dataset.subprocess.check_output",
            return_value=json.dumps(releases),
        ):
            self.assertEqual(release_dataset.next_release_name(), "v10")

    def test_reads_hosted_dataset_source_from_env(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text(
                "# Hosted deployment\n"
                "SILICONE_SHADOWS_SERVER=shadows.example.com\n"
                'SILICONE_SHADOWS_USER="publisher"\n'
            )
            self.assertEqual(
                release_dataset.hosted_dataset_source(env),
                "publisher@shadows.example.com:/var/lib/silicone-shadows/dataset/",
            )
            env.write_text("SILICONE_SHADOWS_SERVER=bad host\n")
            with self.assertRaises(ValueError):
                release_dataset.hosted_dataset_source(env)

    def test_snapshot_difference_counts_records_and_files(self):
        released = release_dataset.snapshot(
            [
                ("a/metadata.json", b'{"quality":"good"}'),
                ("a/outline.svg", b"old"),
                ("removed.txt", b"removed"),
            ]
        )
        current = release_dataset.snapshot(
            [
                ("a/metadata.json", b'{"quality":"good"}'),
                ("a/outline.svg", b"new"),
                ("b/metadata.json", b'{"quality":"unusable"}'),
            ]
        )
        with patch("builtins.print") as output:
            release_dataset.report_difference("Current", released, current)
        output.assert_called_once_with(
            "Current: +1 records; 1 files added, 1 changed, 1 removed"
        )

    def test_sync_hosted_can_extend_check_mode(self):
        with patch(
            "sys.argv", ["admin/release_dataset.py", "--check", "--sync-hosted"]
        ):
            with patch("admin.release_dataset.check_state") as check_state:
                release_dataset.main()
        check_state.assert_called_once_with(True)

    def test_catalog_only_hosted_sync_commits_pin_and_builds_matching_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
            git("init", "-q")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.test")
            config = {"provider": "fantasytoybox", "version": 7,
                      "url_template": "https://example.test/v{version}.json"}
            (root / "catalog_source.json").write_text(json.dumps(config))
            record = root / "dataset" / "vendor" / "type" / "name" / "metadata.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"schema_version": 1, "catalog_id": 1,
                                          "quality": "unusable", "source": "catalog"}))
            git("add", ".")
            git("commit", "-qm", "Initial")
            run = subprocess.run
            def local_run(command, **kwargs):
                if command[0] == "rsync":
                    return subprocess.CompletedProcess(command, 0)
                return run(command, **kwargs)
            with (
                patch.object(release_dataset, "ROOT", root),
                patch.object(release_dataset, "read_hosted_catalog", return_value={**config, "version": 8}),
                patch.object(release_dataset, "hosted_dataset_source", return_value="unused"),
                patch.object(release_dataset.subprocess, "run", side_effect=local_run),
            ):
                self.assertTrue(release_dataset.sync_hosted_dataset("v1"))
                self.assertEqual(git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
                                 "catalog_source.json")
                manifest = release_dataset.build_manifest("v1", release_dataset.dataset_files())
                self.assertEqual(manifest["catalog"]["version"], 8)
                self.assertEqual(manifest["catalog"]["url"], "https://example.test/v8.json")
                self.assertFalse(release_dataset.sync_hosted_dataset("v1"))
            self.assertEqual(git("status", "--porcelain"), "")

    def test_hosted_catalog_descriptor_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "download"
            target.mkdir()
            config = {"provider": "fantasytoybox", "version": 7,
                      "url_template": "https://example.test/v{version}.json"}
            (root / "catalog_source.json").write_text(json.dumps(config))
            with (
                patch.object(release_dataset, "ROOT", root),
                patch.object(release_dataset, "hosted_dataset_source", return_value="host:/state/dataset/"),
                patch.object(release_dataset.subprocess, "run"),
            ):
                path = target / "catalog_source.json"
                path.write_text(json.dumps({**config, "version": 8}))
                self.assertEqual(release_dataset.read_hosted_catalog(target)["version"], 8)
                path.write_text(json.dumps({**config, "url_template": "https://other.test/{version}"}))
                with self.assertRaises(ValueError):
                    release_dataset.read_hosted_catalog(target)

    def test_rejects_non_canonical_outline(self):
        with tempfile.TemporaryDirectory() as directory:
            outline = Path(directory) / "outline.svg"
            outline.write_text(
                '<svg width="500" height="1000" viewBox="0 0 .5 1" '
                'xmlns="http://www.w3.org/2000/svg">'
                '<path id="outline" d="M0 0v1z"/>'
                '<line id="main-length" x1=".25" y1="1" x2=".25" y2="0" display="none"/>'
                "</svg>"
            )
            release_dataset.validate_outline(outline)
            outline.write_text(outline.read_text().replace('y2="0"', 'y2=".2"'))
            with self.assertRaises(ValueError):
                release_dataset.validate_outline(outline)


if __name__ == "__main__":
    unittest.main()
