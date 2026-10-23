#!/usr/bin/env python3
"""Report suspicious size measurements in the pinned Fantasy Toybox catalog."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.catalog import ensure_catalog, toybox_url  # noqa: E402
from server.catalog_checks import CHECKS, Finding, audit  # noqa: E402


CONFIG = ROOT / "catalog_source.json"


def markdown(findings: list[Finding], catalog: list[dict], version: str) -> str:
    products = {product["id"]: product for product in catalog}
    affected = {finding.product_id for finding in findings}
    lines = [
        "# Catalog measurement report",
        "",
        f"Fantasy Toybox catalog {version}: {len(catalog)} products. "
        f"{len(findings)} findings in {len(affected)} products. Measurements are "
        "inches, as published in the catalog. Each finding is a lead to verify "
        "against the vendor's size chart, not a confirmed error.",
    ]
    for check in CHECKS:
        rows = [finding for finding in findings if finding.check == check.key]
        if not rows:
            continue
        lines += [
            "",
            f"## {check.title} ({len(rows)})",
            "",
            check.explanation,
            "",
            "| ID | Product | Finding |",
            "| --- | --- | --- |",
        ]
        for finding in rows:
            link = toybox_url(products[finding.product_id])
            name = f"{finding.vendor} · {finding.name}".replace("|", "\\|")
            lines.append(
                f"| {finding.product_id} | [{name}]({link}) | {finding.detail} |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--products", type=Path, help="catalog JSON (default: pinned catalog)"
    )
    parser.add_argument(
        "--output", type=Path, help="write the report here instead of stdout"
    )
    parser.add_argument("--json", action="store_true", help="write findings as JSON")
    args = parser.parse_args()

    path = args.products or ensure_catalog(CONFIG)
    catalog = json.loads(path.read_text())
    findings = audit(catalog)
    version = path.stem.removeprefix("products_")
    report = (
        json.dumps([asdict(finding) for finding in findings], indent=2) + "\n"
        if args.json
        else markdown(findings, catalog, version)
    )
    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"Wrote {len(findings)} findings to {args.output}")
    else:
        sys.stdout.write(report)


if __name__ == "__main__":
    main()
