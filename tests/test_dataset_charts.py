import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from admin import dataset_charts
from server.catalog import has_measurements


class DatasetChartsTest(unittest.TestCase):
    def test_measurements_match_stats_definition(self):
        for value in (True, False, None, "5", 0, -1):
            self.assertFalse(has_measurements({"sz": {"s": [{"len": value}]}}))
        for field in ("len", "circ", "wcirc"):
            self.assertTrue(has_measurements({"sz": {"s": [None, {field: 5}]}}))

    def test_counts_only_current_measured_catalog_once_per_product(self):
        products = {1: {"pt": "Dildo"}, 2: {"pt": "Plug"}}
        contents = [
            (
                "a/metadata.json",
                json.dumps(
                    {
                        "catalog_id": 1,
                        "quality": "good",
                        "variants": {"large": {"quality": "bad_perspective"}},
                    }
                ).encode(),
            ),
            ("b/metadata.json", b'{"catalog_id":2,"quality":"unusable"}'),
            ("c/metadata.json", b'{"catalog_id":3,"quality":"good"}'),
            ("d/metadata.json", b'{"record_id":"community","quality":"good"}'),
        ]
        self.assertEqual(
            dataset_charts.count_release(contents, products),
            {"quality": {"good": 1, "unusable": 1}, "types": {"Dildo": 1, "Plug": 1}},
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            dataset_charts.count_release(contents + [contents[0]], products)

    def test_collect_excludes_drafts_and_prereleases_and_sorts_publication(self):
        releases = [
            {"tagName": tag, "isDraft": draft, "isPrerelease": pre, "publishedAt": date}
            for tag, draft, pre, date in [
                ("v3", True, False, ""),
                ("v4", False, True, "4"),
                ("v2", False, False, "2"),
                ("v1", False, False, "1"),
            ]
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "catalog_source.json").write_text(
                json.dumps(
                    {"version": 241, "url_template": "https://example.test/v{version}"}
                )
            )
            with (
                patch.object(dataset_charts, "ROOT", root),
                patch.object(
                    dataset_charts,
                    "download_catalog",
                    return_value=b'[{"id":1,"sz":{"s":[{"circ":3}]}},{"id":2}]',
                ),
                patch.object(
                    dataset_charts.subprocess,
                    "check_output",
                    return_value=json.dumps(releases),
                ),
                patch.object(dataset_charts, "release_contents", return_value=([], {})),
            ):
                data = dataset_charts.collect_history()
        self.assertEqual(data["measured_products"], 1)
        self.assertEqual([r["tagName"] for r in data["releases"]], ["v1", "v2"])

    def test_first_release_and_unknown_product_type_render(self):
        try:
            dataset_charts.plotting()
        except RuntimeError:
            self.skipTest("Optional chart dependencies not installed")
        data = {
            "catalog_version": 241,
            "measured_products": 5,
            "releases": [
                {
                    "tagName": "v0.1.0",
                    "quality": {"good": 1},
                    "types": {"New & special": 1},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            for field in ("types", "quality"):
                dataset_charts.render_chart(data, field, Path(directory))
            for path in Path(directory).glob("*.svg"):
                text = "".join(ET.parse(path).getroot().itertext())
                self.assertIn("v1", text)
                self.assertNotIn("v0.1.0", text)
                self.assertIn("Not processed · 4", text)
            data["measured_products"] = 0
            with self.assertRaisesRegex(ValueError, "nonempty"):
                dataset_charts.render_chart(data, "types", Path(directory))


if __name__ == "__main__":
    unittest.main()
