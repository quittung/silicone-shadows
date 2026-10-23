import json
import unittest

from admin import dataset_changelog, release_dataset


class DatasetChangelogTest(unittest.TestCase):
    def render(self, before, after):
        old = release_dataset.snapshot(
            list(before.items()), ignore_schema_version=True
        )[0]
        new = release_dataset.snapshot(list(after.items()), ignore_schema_version=True)[
            0
        ]
        return dataset_changelog.changelog(
            "v2",
            "v1",
            old,
            new,
            dataset_changelog.record_metadata(list(before.items()), []),
            dataset_changelog.record_metadata(list(after.items()), []),
        )

    def metadata(self, **fields):
        return json.dumps({"schema_version": 2, "quality": "good", **fields}).encode()

    def test_compact_counts_grouping_and_removed_product(self):
        before = {
            "vendor/plug/changed/metadata.json": self.metadata(),
            "vendor/plug/changed/outline.svg": b"old",
            "vendor/plug/changed/variants/xl.svg": b"removed",
            "vendor/plug/removed/metadata.json": self.metadata(name="Retired"),
        }
        after = {
            "vendor/plug/changed/metadata.json": self.metadata(source="alternative"),
            "vendor/plug/changed/outline.svg": b"new",
            "vendor/plug/changed/variants/large.svg": b"added",
            "vendor/plug/added/metadata.json": self.metadata(
                variants={
                    "large": {
                        "quality": "bad_perspective",
                        "file": "variants/large.svg",
                    },
                }
            ),
            "vendor/plug/added/outline.svg": b"general",
            "vendor/plug/added/variants/large.svg": b"large",
        }
        text = self.render(before, after)
        self.assertIn("1 product added, 1 updated, and 1 removed", text)
        self.assertIn("### Plug\n#### Vendor\n- **Added**", text)
        self.assertTrue(text.startswith("# Dataset v2 changelog\nCompared with v1:"))
        self.assertIn("quality: bad perspective–good.\n\n---\n\n## Updated", text)
        self.assertIn("1/2 updated.\n\n---\n\n## Removed", text)
        self.assertEqual(text.count("\n---\n"), 2)
        self.assertIn("all outlines were updated.\n\n### Plug", text)
        self.assertIn("- **Added** — 2 outlines; quality: bad perspective–good.", text)
        self.assertIn(
            "- **Changed** — 1 outline added; 1 outline removed; 1/2 updated.",
            text,
        )
        self.assertIn("- **Retired**\n", text)
        self.assertEqual(sum(line.startswith("- **") for line in text.splitlines()), 3)
        self.assertNotIn("xl", text)
        self.assertNotIn("variants/", text)

    def test_schema_only_and_formatting_changes_are_excluded(self):
        before = {"vendor/plug/kept/metadata.json": self.metadata(schema_version=1)}
        after = {"vendor/plug/kept/metadata.json": self.metadata()}
        text = self.render(before, after)
        self.assertIn("0 products added, 0 updated, and none removed", text)
        self.assertIn("No product changes.", text)

    def test_initial_release_has_no_predecessor(self):
        text = dataset_changelog.changelog(
            "v1",
            None,
            {},
            {"vendor/plug/product/metadata.json": "digest"},
            {},
            {"vendor/plug/product/metadata.json": {"quality": "unusable"}},
        )
        self.assertIn("Initial release: 1 product added.", text)
        self.assertNotIn("Compared with", text)
        self.assertIn("no product image suitable for outlining found", text)

    def test_added_defaults_are_stated_once_and_exceptions_remain_explicit(self):
        after = {
            "vendor/plug/ordinary/metadata.json": self.metadata(),
            "vendor/plug/ordinary/outline.svg": b"outline",
            "vendor/plug/perspective/metadata.json": self.metadata(
                quality="bad_perspective"
            ),
            "vendor/plug/perspective/outline.svg": b"outline",
            "vendor/plug/unusable/metadata.json": self.metadata(quality="unusable"),
        }
        text = self.render({}, after)
        self.assertIn("3 products added, 0 updated, and none removed.", text)
        note = "Unless noted otherwise, each product includes one outline rated good."
        self.assertEqual(text.count(note), 1)
        self.assertIn(f"## Added\n{note}\n\n### Plug", text)
        self.assertNotIn("\n---\n", text)
        self.assertIn("- **Ordinary**\n", text)
        self.assertIn("- **Perspective** — quality: bad perspective.", text)
        self.assertIn(
            "- **Unusable** — no product image suitable for outlining found.", text
        )
        self.assertNotIn("0 outlines; quality: unusable", text)
        self.assertNotIn("1 outline; quality: good.", text)

    def test_quality_changes_are_distinct_from_other_metadata(self):
        key = "phreak/dildo/brutus/metadata.json"
        for old, new, wording in (
            ("bad_perspective", "good", "quality improved"),
            ("good", "unusable", "quality declined"),
        ):
            with self.subTest(wording=wording):
                text = self.render(
                    {key: self.metadata(quality=old)}, {key: self.metadata(quality=new)}
                )
                self.assertIn(f"- **Brutus** — {wording}.", text)
                self.assertNotIn("metadata updated", text)

    def test_variant_quality_changes_are_summarized_at_product_level(self):
        key = "vendor/plug/product/metadata.json"
        before = {key: self.metadata(variants={"large": {"quality": "unusable"}})}
        after = {key: self.metadata(variants={"large": {"quality": "good"}})}
        text = self.render(before, after)
        self.assertIn("- **Product** — quality improved.", text)
        self.assertNotIn("metadata updated", text)

    def test_outline_metadata_is_included_in_update_count(self):
        key = "vendor/plug/product/metadata.json"
        svg = "vendor/plug/product/outline.svg"
        text = self.render(
            {key: self.metadata(source="catalog"), svg: b"unchanged"},
            {key: self.metadata(source="alternative"), svg: b"unchanged"},
        )
        self.assertIn("- **Product**\n", text)
        self.assertNotIn("metadata updated", text)
        variant_svg = "vendor/plug/product/variants/large.svg"
        old = {
            "large": {
                "file": "variants/large.svg",
                "quality": "good",
                "source": "catalog",
                "sizes": ["M"],
            }
        }
        new = {"large": {**old["large"], "sizes": ["L"]}}
        text = self.render(
            {key: self.metadata(variants=old), variant_svg: b"unchanged"},
            {key: self.metadata(variants=new), variant_svg: b"unchanged"},
        )
        self.assertIn("- **Product**\n", text)
        self.assertNotIn("metadata updated", text)

    def test_product_metadata_changes_remain_visible(self):
        key = "vendor/plug/product/metadata.json"
        text = self.render(
            {key: self.metadata(notes="Old description")},
            {key: self.metadata(notes="Corrected description")},
        )
        self.assertIn("- **Product** — metadata updated.", text)

    def test_partial_outline_updates_show_fraction(self):
        key = "vendor/plug/product/metadata.json"
        before = {
            key: self.metadata(),
            "vendor/plug/product/outline.svg": b"old",
            "vendor/plug/product/variants/medium.svg": b"old",
            "vendor/plug/product/variants/large.svg": b"unchanged",
        }
        after = {
            **before,
            "vendor/plug/product/outline.svg": b"new",
            "vendor/plug/product/variants/medium.svg": b"new",
        }
        text = self.render(before, after)
        self.assertIn("Unless noted otherwise, all outlines were updated.", text)
        self.assertIn("- **Product** — 2/3 updated.", text)
        after["vendor/plug/product/variants/large.svg"] = b"new"
        self.assertIn("- **Product**\n", self.render(before, after))

    def test_added_quality_range_only_describes_included_outlines(self):
        key = "vendor/plug/product/metadata.json"
        after = {
            key: self.metadata(
                quality="unusable",
                variants={
                    "medium": {"quality": "good", "file": "variants/medium.svg"},
                    "large": {"quality": "good", "file": "variants/large.svg"},
                },
            ),
            "vendor/plug/product/variants/medium.svg": b"medium",
            "vendor/plug/product/variants/large.svg": b"large",
        }
        text = self.render({}, after)
        self.assertIn("- **Product** — 2 outlines.", text)
        self.assertNotIn("quality: good", text)
        self.assertNotIn("unusable", text)

    def test_catalog_identity_preserves_names_and_independent_records(self):
        contents = [
            ("vendor/plug/catalog/metadata.json", self.metadata(catalog_id=7)),
            (
                "vendor/plug/independent/metadata.json",
                self.metadata(name="Independent"),
            ),
        ]
        records = dataset_changelog.record_metadata(
            contents,
            [
                {"id": 7, "n": "Alien Egg II", "vn": "Drake’s Shop", "pt": "Ovi & Egg"},
            ],
        )
        self.assertEqual(records[contents[0][0]]["name"], "Alien Egg II")
        self.assertEqual(records[contents[0][0]]["vendor"], "Drake’s Shop")
        self.assertEqual(records[contents[0][0]]["product_type"], "Ovi & Egg")
        self.assertEqual(records[contents[1][0]]["name"], "Independent")


if __name__ == "__main__":
    unittest.main()
