#!/usr/bin/env python3
"""Check for a newer Fantasy Toybox catalog and optionally commit its version."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.catalog import download_catalog  # noqa: E402


CONFIG = ROOT / "catalog_source.json"


def fetch(
    version: int, url_template: str, *, optional: bool = False
) -> list[dict] | None:
    try:
        data = download_catalog(url_template.format(version=version))
    except HTTPError as error:
        if optional and error.code == 404:
            return None
        raise
    except ValueError as error:
        if optional and str(error) in {
            "downloaded catalog is not JSON",
            "downloaded catalog is not a product list",
        }:
            return None
        raise

    catalog = json.loads(data)
    if any(not isinstance(product, dict) or "id" not in product for product in catalog):
        raise ValueError(f"catalog v{version} contains a product without an ID")
    if len({product["id"] for product in catalog}) != len(catalog):
        raise ValueError(f"catalog v{version} contains duplicate product IDs")
    return catalog


def latest_catalog(
    current: int, url_template: str
) -> tuple[int, list[dict], list[dict]]:
    current_catalog = fetch(current, url_template)
    assert current_catalog is not None
    latest_version, latest = current, current_catalog
    while True:
        version = latest_version + 1
        candidate = fetch(version, url_template, optional=True)
        if candidate is None:
            return latest_version, current_catalog, latest
        latest_version, latest = version, candidate


def report(
    current: list[dict],
    latest: list[dict],
    current_version: int,
    latest_version: int,
) -> None:
    before = {product["id"]: product for product in current}
    after = {product["id"]: product for product in latest}
    common = before.keys() & after.keys()
    changed = {
        product_id for product_id in common if before[product_id] != after[product_id]
    }
    changed_images = {
        product_id
        for product_id in common
        if before[product_id].get("pic") != after[product_id].get("pic")
    }
    print(f"Pinned v{current_version}: {len(current)} products")
    print(f"Latest v{latest_version}: {len(latest)} products")
    print(
        f"Added {len(after.keys() - before.keys())}; "
        f"removed {len(before.keys() - after.keys())}; "
        f"changed {len(changed)}; image paths changed {len(changed_images)}"
    )


def update_and_commit(config: dict, latest_version: int) -> None:
    path = CONFIG.relative_to(ROOT)
    path_text = path.as_posix()
    for command in (
        ["git", "diff", "--quiet", "--", path_text],
        ["git", "diff", "--cached", "--quiet", "--", path_text],
    ):
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode == 1:
            raise RuntimeError(f"refusing to overwrite an existing change to {path}")
        result.check_returncode()

    config["version"] = latest_version
    CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "commit",
            "--only",
            path_text,
            "-m",
            f"Update Fantasy Toybox catalog to v{latest_version}",
        ],
        cwd=ROOT,
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="update catalog_source.json and commit that file only",
    )
    args = parser.parse_args()

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    current_version = int(config["version"])
    latest_version, current, latest = latest_catalog(
        current_version, config["url_template"]
    )
    report(current, latest, current_version, latest_version)

    if not args.update:
        return
    if latest_version == current_version:
        print("Catalog is already current; nothing to commit.")
        return
    update_and_commit(config, latest_version)


if __name__ == "__main__":
    main()
