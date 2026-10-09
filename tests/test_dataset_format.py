import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from admin import release_dataset
from server.models import GuestMetadata, GuestSize, ReviewState
from server.routes.public import _archive
from server.workspace import Workspace


class DatasetFormatTest(unittest.TestCase):
    def test_publication_and_download_convert_all_measurements(self):
        for unit, multiplier in (("in", 1), ("cm", 2.54), ("mm", 25.4)):
            with self.subTest(unit=unit):
                size = GuestSize(
                    label="Medium",
                    short_label="M",
                    unit=unit,
                    length=6 * multiplier,
                    circumference=4 * multiplier,
                    widest_circumference=5 * multiplier,
                    price=25,
                )
                metadata = GuestMetadata(
                    vendor="Maker", product_type="Type", name="Product", sizes=[size]
                )
                record = Workspace.independent_document("example", metadata)
                self.assertEqual(record["schema_version"], 2)
                with zipfile.ZipFile(BytesIO(_archive(metadata, b"svg"))) as archive:
                    download = json.loads(archive.read("metadata.json"))
                for document in (record, download):
                    exported = document["sizes"][0]
                    self.assertEqual(exported["unit"], "in")
                    for field, expected in (
                        ("length", 6),
                        ("circumference", 4),
                        ("widest_circumference", 5),
                        ("price", 25),
                    ):
                        self.assertAlmostEqual(exported[field], expected)
                    self.assertEqual(exported["label"], "Medium")
                    # Re-exporting inches must not convert the values again.
                    self.assertEqual(
                        GuestSize.model_validate(exported).in_inches(),
                        record["sizes"][0],
                    )
                self.assertEqual(size.unit, unit)
                self.assertEqual(size.length, 6 * multiplier)

    def test_missing_measurements_remain_missing(self):
        size = GuestSize(short_label="M", unit="cm")
        exported = size.in_inches()
        self.assertEqual(exported["unit"], "in")
        for field in ("length", "circumference", "widest_circumference"):
            self.assertIsNone(exported[field])

    def test_catalog_without_variants_uses_version_two(self):
        record = Workspace.record_document(
            {"id": 1234}, ReviewState(rating="good"), "catalog"
        )
        self.assertEqual(record["schema_version"], 2)
        self.assertNotIn("variants", record)

    def test_release_rejects_old_versions_and_metric_measurements(self):
        metadata = GuestMetadata(
            vendor="Maker",
            product_type="Type",
            name="Product",
            sizes=[GuestSize(short_label="M", unit="cm", length=15)],
        )
        record = Workspace.independent_document("example", metadata)
        record["quality"] = "unusable"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "dataset/maker/type/product/metadata.json"
            path.parent.mkdir(parents=True)
            (root / "catalog_source.json").write_text(
                json.dumps(
                    {"version": 1, "url_template": "https://example.com/{version}.json"}
                )
            )
            with (
                patch.object(release_dataset, "ROOT", root),
                patch.object(release_dataset, "git", return_value="test"),
            ):
                path.write_text(json.dumps(record))
                self.assertEqual(
                    release_dataset.build_manifest("v1", [path])["schema_version"], 2
                )
                record["schema_version"] = 1
                path.write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, "schema version"):
                    release_dataset.build_manifest("v1", [path])
                record["schema_version"] = 2
                record["sizes"][0]["unit"] = "cm"
                path.write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, "must use inches"):
                    release_dataset.build_manifest("v1", [path])
