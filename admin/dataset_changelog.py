#!/usr/bin/env python3
"""Generate product changelogs for dataset release archives or standalone previews."""

import argparse
import json
import re
import subprocess
import sys
import zipfile
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.catalog import download_catalog  # noqa: E402

if __package__:
    from .release_dataset import (
        QUALITIES,
        ROOT,
        changed_records,
        snapshot,
        validate_release_name,
    )
else:
    from release_dataset import (
        QUALITIES,
        ROOT,
        changed_records,
        snapshot,
        validate_release_name,
    )


def changelog(
    version: str,
    previous_version: str | None,
    before: dict[str, str],
    after: dict[str, str],
    before_metadata: dict[str, dict],
    after_metadata: dict[str, dict],
) -> str:
    groups = changed_records(before, after)
    added, updated, removed = (len(group) for group in groups)
    removed_summary = f"{removed} removed" if removed else "none removed"
    lines = [
        f"# Dataset {version} changelog",
        f"Compared with {previous_version}: {added} product{'s' if added != 1 else ''} added, {updated} updated, and {removed_summary}."
        if previous_version
        else f"Initial release: {added} product{'s' if added != 1 else ''} added.",
    ]

    def label(value: str) -> str:
        # Catalog-backed records store their identity in the directory names.
        return value.replace("-", " ").title()

    def escape(value: str) -> str:
        return re.sub(r"([\\`*_{}\[\]<>#])", r"\\\1", " ".join(value.split()))

    def outlines(files: dict[str, str], product: Path) -> set[str]:
        prefix = product.as_posix() + "/"
        return {
            name for name in files if name.startswith(prefix) and name.endswith(".svg")
        }

    def quality_score(record: dict) -> Fraction:
        ratings = [record["quality"]] + [
            variant["quality"] for variant in record.get("variants", {}).values()
        ]
        return Fraction(
            sum(2 - QUALITIES.index(rating) for rating in ratings), len(ratings)
        )

    def product_metadata(record: dict) -> dict:
        return {
            key: value
            for key, value in record.items()
            if key not in {"quality", "schema_version", "source", "variants"}
        }

    def outline_metadata(record: dict, filename: str) -> dict:
        if filename == "outline.svg":
            return {"source": record.get("source")}
        variant = next(
            (
                entry
                for entry in record.get("variants", {}).values()
                if entry.get("file") == filename
            ),
            {},
        )
        return {key: value for key, value in variant.items() if key != "quality"}

    for status, products in zip(("Added", "Updated", "Removed"), groups):
        if not products:
            continue
        if lines[-1].startswith("- **"):
            lines.extend(["", "---"])
        lines.extend(["", f"## {status}"])
        if status == "Added":
            lines.append(
                "Unless noted otherwise, each product includes one outline rated good."
            )
        elif status == "Updated":
            lines.append("Unless noted otherwise, all outlines were updated.")
        if status in {"Added", "Updated"}:
            lines.append("")
        entries = []
        for product in products:
            key = (product / "metadata.json").as_posix()
            metadata = (before_metadata if status == "Removed" else after_metadata)[key]
            vendor, product_type, name = product.parts
            entries.append(
                (
                    metadata.get("product_type") or label(product_type),
                    metadata.get("vendor") or label(vendor),
                    metadata.get("name") or label(name),
                    product,
                    metadata,
                )
            )
        last_type = last_vendor = None
        for product_type, vendor, name, product, metadata in sorted(
            entries,
            key=lambda entry: (
                *[value.casefold() for value in entry[:3]],
                str(entry[3]),
            ),
        ):
            if product_type != last_type:
                if lines[-1].startswith("- **"):
                    lines.append("")
                lines.append(f"### {escape(product_type)}")
                last_type, last_vendor = product_type, None
            if vendor != last_vendor:
                if lines[-1].startswith("- **"):
                    lines.append("")
                lines.append(f"#### {escape(vendor)}")
                last_vendor = vendor
            details = []
            old_outlines, new_outlines = (
                outlines(before, product),
                outlines(after, product),
            )
            if status == "Added":
                count = len(new_outlines)
                qualities = {
                    variant["quality"]
                    for variant in metadata.get("variants", {}).values()
                    if (product / variant.get("file", "")).as_posix() in new_outlines
                }
                if (product / "outline.svg").as_posix() in new_outlines:
                    qualities.add(metadata["quality"])
                if not qualities:
                    qualities = {metadata["quality"]}
                ordered = [
                    quality.replace("_", " ")
                    for quality in reversed(QUALITIES)
                    if quality in qualities
                ]
                quality_range = (
                    ordered[0] if len(ordered) == 1 else f"{ordered[0]}–{ordered[-1]}"
                )
                if count == 0 and qualities == {"unusable"}:
                    details.append("no product image suitable for outlining found")
                else:
                    if count != 1:
                        details.append(f"{count} outlines")
                    if qualities != {"good"}:
                        details.append(f"quality: {quality_range}")
            elif status == "Updated":
                key = (product / "metadata.json").as_posix()
                old_metadata = before_metadata[key]
                updated_outlines = sum(
                    before[name] != after[name]
                    or outline_metadata(
                        old_metadata, Path(name).relative_to(product).as_posix()
                    )
                    != outline_metadata(
                        metadata, Path(name).relative_to(product).as_posix()
                    )
                    for name in old_outlines & new_outlines
                )
                for count, action in (
                    (len(new_outlines - old_outlines), "added"),
                    (len(old_outlines - new_outlines), "removed"),
                ):
                    if count:
                        details.append(
                            f"{count} outline{'s' if count != 1 else ''} {action}"
                        )
                if new_outlines and (
                    updated_outlines != len(new_outlines)
                    or old_outlines != new_outlines
                ):
                    details.append(f"{updated_outlines}/{len(new_outlines)} updated")
                quality_delta = quality_score(metadata) - quality_score(old_metadata)
                if quality_delta:
                    details.append(
                        "quality improved" if quality_delta > 0 else "quality declined"
                    )
                if before[key] != after[key] and product_metadata(
                    old_metadata
                ) != product_metadata(metadata):
                    details.append("metadata updated")
            suffix = f" — {'; '.join(details)}." if details else ""
            lines.append(f"- **{escape(name)}**{suffix}")
    if not any(groups):
        lines.extend(["", "No product changes."])
    return "\n".join(lines) + "\n"


def release_contents(
    version: str, directory: Path
) -> tuple[list[tuple[str, bytes]], dict]:
    validate_release_name(version)
    directory.mkdir()
    subprocess.run(
        [
            "gh",
            "release",
            "download",
            version,
            "--dir",
            str(directory),
            "--pattern",
            f"silicone-shadows-dataset-{version}.zip",
        ],
        cwd=ROOT,
        check=True,
    )
    archive = directory / f"silicone-shadows-dataset-{version}.zip"
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        if manifest["dataset_version"] != version:
            raise ValueError(f"archive does not contain dataset {version}")
        contents = [
            (name.removeprefix("dataset/"), bundle.read(name))
            for name in bundle.namelist()
            if name.startswith("dataset/") and not name.endswith("/")
        ]
    return contents, manifest["catalog"]


def record_metadata(
    contents: list[tuple[str, bytes]], catalog: list[dict]
) -> dict[str, dict]:
    identities = {product["id"]: product for product in catalog}
    records = {}
    for name, content in contents:
        if not name.endswith("/metadata.json"):
            continue
        record = json.loads(content)
        identity = identities.get(record.get("catalog_id"), {})
        records[name] = {
            **record,
            **{
                field: identity[key]
                for field, key in (
                    ("name", "n"),
                    ("vendor", "vn"),
                    ("product_type", "pt"),
                )
                if identity.get(key)
            },
        }
    return records


def generate_changelog(
    version: str,
    previous_version: str | None,
    before_contents: list[tuple[str, bytes]],
    after_contents: list[tuple[str, bytes]],
    before_catalog: dict,
    after_catalog: dict,
) -> str:
    catalogs = {
        url: json.loads(download_catalog(url))
        for url in {before_catalog["url"], after_catalog["url"]}
    }
    before, _ = snapshot(before_contents, ignore_schema_version=True)
    after, _ = snapshot(after_contents, ignore_schema_version=True)
    return changelog(
        version,
        previous_version,
        before,
        after,
        record_metadata(before_contents, catalogs[before_catalog["url"]]),
        record_metadata(after_contents, catalogs[after_catalog["url"]]),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "version", nargs="?", help="defaults to the latest published release"
    )
    parser.add_argument("--previous", help="override the preceding published release")
    parser.add_argument(
        "--output", type=Path, help="write Markdown to a standalone file"
    )
    args = parser.parse_args()
    try:
        releases = json.loads(
            subprocess.check_output(
                [
                    "gh",
                    "release",
                    "list",
                    "--limit",
                    "1000",
                    "--json",
                    "tagName,isDraft,isPrerelease,publishedAt",
                ],
                cwd=ROOT,
                text=True,
            )
        )
        published = sorted(
            (
                release
                for release in releases
                if not release["isDraft"] and not release["isPrerelease"]
            ),
            key=lambda release: release["publishedAt"],
            reverse=True,
        )
        tags = [release["tagName"] for release in published]
        version = args.version or tags[0]
        index = tags.index(version)
        previous = args.previous or (tags[index + 1] if index + 1 < len(tags) else None)
        with TemporaryDirectory() as directory:
            after_contents, after_catalog = release_contents(
                version, Path(directory) / "after"
            )
            before_contents, before_catalog = (
                release_contents(previous, Path(directory) / "before")
                if previous
                else ([], after_catalog)
            )
        text = generate_changelog(
            version,
            previous,
            before_contents,
            after_contents,
            before_catalog,
            after_catalog,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
            print(f"Wrote {args.output}")
        else:
            print(text, end="")
    except (
        OSError,
        ValueError,
        IndexError,
        KeyError,
        subprocess.CalledProcessError,
    ) as error:
        parser.error(f"could not preview release comparison: {error}")


if __name__ == "__main__":
    main()
