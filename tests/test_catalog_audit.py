import unittest

from admin import catalog_audit
from server.catalog_checks import audit, measurement_issues


def product(product_id, sizes, product_type="Dildo", widest_label="Knot"):
    return {
        "id": product_id,
        "n": f"Product {product_id}",
        "vn": "Vendor",
        "pt": product_type,
        "sz": {"wl": widest_label, "s": sizes},
    }


def size(short, length=None, circ=None, wcirc=None, code=None, name=None):
    entry = {"ShortLabel": short, "sl": name or short, "len": length, "circ": circ}
    if wcirc is not None:
        entry["wcirc"] = wcirc
    if code is not None:
        entry["sc"] = code
    return entry


def findings(*products):
    return [
        (finding.check, finding.product_id, finding.sizes)
        for finding in audit(list(products))
    ]


class CatalogAuditTest(unittest.TestCase):
    def test_consistent_product_has_no_findings(self):
        self.assertEqual(
            findings(
                product(
                    1,
                    [
                        size("S", 5.5, 3.9, 5.4, 1),
                        size("M", 7.6, 5.3, 7.4, 2),
                        size("L", 9.5, 6.75, 9.5, 3),
                    ],
                )
            ),
            [],
        )

    def test_single_wrong_circumference_is_reported_three_ways(self):
        # Fantasy Toybox's Austin: L circumference should be 6.75, not 9.8.
        austin = product(
            138,
            [
                size("XS", 5.5, 3.9, 5.4, 1),
                size("S", 6.3, 4.5, 6.3, 2),
                size("M", 7.8, 5.3, 7.4, 3),
                size("L", 9.5, 9.8, 9.5, 4),
                size("XL", 11.3, 7.9, 11.3, 5),
            ],
        )
        self.assertEqual(
            findings(austin),
            [
                ("girth_order", 138, ["L", "XL"]),
                ("widest_mismatch", 138, ["L"]),
                ("proportion_outlier", 138, ["L"]),
            ],
        )
        # Only checks that pinpoint one wrong value mark sizes to leave out.
        self.assertEqual(
            [finding.wrong_value for finding in audit([austin])], [False, True, True]
        )

    def test_product_wide_narrow_widest_point_is_not_a_mismatch(self):
        # A "widest" point labelled Head can be narrower than the shaft throughout.
        valentino = product(
            90,
            [size("S", 5, 5.2, 4.5, 1), size("M", 6, 6.2, 5.4, 2)],
            widest_label="Head",
        )
        self.assertEqual(findings(valentino), [])

    def test_labels_and_copied_sizes(self):
        # Fantasy Toybox's Roc: the smallest size's full name says Medium.
        roc = product(
            1306,
            [
                size("XS", 4.1, 5, code=2, name="Medium"),
                size("S", 5, 6.1, code=3, name="Small"),
                size("M", 5.7, 7.1, code=4, name="Medium"),
            ],
        )
        copied = product(3, [size("S", 6.1, 5, code=1), size("L", 6.1, 5, code=3)])
        length_only = product(4, [size("S", 6.1, code=1), size("L", 6.1, code=3)])
        results = audit([roc, copied, length_only])
        self.assertEqual(
            [(finding.check, finding.product_id, finding.sizes) for finding in results],
            [
                ("duplicate_labels", 1306, ["XS", "M"]),
                ("identical_sizes", 3, ["S", "L"]),
            ],
        )
        self.assertIn("“Medium” is used by XS and M", results[0].detail)

    def test_lengths_out_of_size_order_skip_length_based_checks(self):
        self.assertEqual(
            findings(
                product(
                    4,
                    [
                        size("S", 19, 5, code=1),
                        size("M", 11, 6, code=2),
                        size("L", 12, 4, code=3),
                    ],
                )
            ),
            [("length_order", 4, ["S", "M"])],
        )

    def test_implausible_circumferences_explain_likely_cause(self):
        flint = product(
            126,
            [
                size("S", 5.3, 21.99, code=1),
                size("M", 6.5, 28.27, code=2),
                size("L", 8.5, 34.56, code=3),
            ],
        )
        centimetres = product(
            171, [size("S", 5.8, 24.76, code=1), size("M", 7.3, 29.47, code=2)]
        )
        egg = product(
            259,
            [size("S", 2, 8, code=1), size("M", 3, 12, code=2)],
            product_type="OviAndEgg",
        )
        results = audit([flint, centimetres, egg])
        self.assertEqual(
            [(finding.check, finding.product_id) for finding in results],
            [("implausible_girth", 126), ("implausible_girth", 171)],
        )
        self.assertIn("π × (7, 9, 11)", results[0].detail)
        self.assertIn("centimetres", results[1].detail)

    def test_invalid_values_and_unnamed_widest_point(self):
        self.assertEqual(
            findings(
                product(5, [size("S", "6in", 5, code=1), size("3XS", 8, 6)]),
                product(6, [size("S", 5, 5, 6, 1)], widest_label=""),
            ),
            [
                ("missing_size_code", 5, ["3XS"]),
                ("invalid_values", 5, ["S"]),
                ("missing_widest_label", 6, []),
            ],
        )

    def test_contributor_views_only_get_measurement_issues(self):
        catalog = [
            product(5, [size("S", "6in", 5, code=1), size("3XS", 8, 6)]),
            product(6, [size("S", 5, 5, 6, 1)], widest_label=""),
        ]
        self.assertEqual(
            measurement_issues(catalog),
            {
                5: [
                    {
                        "detail": "S length is '6in'",
                        "sizes": ["S"],
                        "wrong_value": False,
                    }
                ]
            },
        )

    def test_markdown_groups_findings_with_toybox_links(self):
        catalog = [product(3, [size("S", 6.1, 5, code=1), size("L", 6.1, 5, code=3)])]
        report = catalog_audit.markdown(audit(catalog), catalog, "v7")
        self.assertIn("catalog v7: 1 products. 1 findings in 1 products.", report)
        self.assertIn("## Identical sizes (1)", report)
        self.assertNotIn("## Duplicate size names", report)
        self.assertIn(
            "| 3 | [Vendor · Product 3](https://fantasytoybox.net/products/dildo/",
            report,
        )


if __name__ == "__main__":
    unittest.main()
