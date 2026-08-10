#!/usr/bin/env python3
"""Build and optionally publish a versioned dataset snapshot."""

import argparse
import hashlib
import json
import math
import re
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parent
VERSION_PATTERN = re.compile(r"v\d+\.\d+\.\d+")
QUALITIES = ("good", "bad_perspective", "unusable")
SOURCES = ("catalog", "alternative")
METADATA_FIELDS = {
    "schema_version", "catalog_id", "vendor", "product_type", "name", "quality", "source"
}
DATASET_ROOT_FILES = {Path("dataset/LICENSE"), Path("dataset/NOTICE.md")}


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def validate_version(version: str) -> None:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("version must look like v1.2.3")


def tracked_dataset_files() -> list[Path]:
    files = [ROOT / line for line in git("ls-files", "--", "dataset").splitlines()]
    if not files:
        raise RuntimeError("dataset contains no tracked files")
    return sorted(files)


def validate_outline(path: Path) -> None:
    label = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    try:
        root = ET.parse(path).getroot()
        width, height = float(root.get("width")), float(root.get("height"))
        view_box = tuple(map(float, root.get("viewBox").split()))
    except (ET.ParseError, TypeError, ValueError) as error:
        raise ValueError(f"{label} is not a valid SVG") from error

    if (
        len(view_box) != 4
        or not all(math.isfinite(value) for value in (width, height, *view_box))
        or width <= 0
        or height <= 0
        or view_box[2] <= 0
        or view_box[3] <= 0
        or not math.isclose(view_box[0], 0, abs_tol=1e-12)
        or not math.isclose(view_box[1], 0, abs_tol=1e-12)
    ):
        raise ValueError(f"{label} has invalid dimensions")
    if not math.isclose(max(width, height), 1000, rel_tol=0, abs_tol=1e-8):
        raise ValueError(f"{label} does not have a 1000px longest side")
    if not math.isclose(width / height, view_box[2] / view_box[3], rel_tol=1e-9):
        raise ValueError(f"{label} has mismatched intrinsic dimensions")

    paths = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "path"]
    if len(paths) != 1 or paths[0].get("id") != "outline":
        raise ValueError(f"{label} must contain one outline path")
    line = next((element for element in root.iter() if element.get("id") == "main-length"), None)
    try:
        x1, y1, x2, y2 = (float(line.get(name)) for name in ("x1", "y1", "x2", "y2"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{label} has no valid main-length vector") from error
    if (
        line.get("display") != "none"
        or not all(math.isfinite(value) for value in (x1, y1, x2, y2))
        or not math.isclose(x1, x2, abs_tol=1e-8)
        or not math.isclose(y1 - y2, 1, abs_tol=1e-8)
    ):
        raise ValueError(f"{label} has a non-canonical main-length vector")


def build_manifest(version: str, files: list[Path]) -> dict:
    for path in files:
        relative = path.relative_to(ROOT)
        if relative not in DATASET_ROOT_FILES and (
            len(relative.parts) != 5
            or relative.parts[0] != "dataset"
            or relative.name not in {"metadata.json", "outline.svg"}
        ):
            raise ValueError(f"unexpected published dataset file: {relative}")

    file_set = set(files)
    metadata_files = [path for path in files if path.name == "metadata.json"]
    qualities: Counter[str] = Counter()
    schema_versions: set[int] = set()
    catalog_ids: set[int] = set()

    for path in metadata_files:
        record = json.loads(path.read_text(encoding="utf-8"))
        if set(record) != METADATA_FIELDS:
            raise ValueError(f"{path.relative_to(ROOT)} has unexpected metadata fields")
        if type(record["schema_version"]) is not int:
            raise ValueError(f"{path.relative_to(ROOT)} has an invalid schema version")
        if any(not isinstance(record[field], str) or not record[field] for field in ("vendor", "product_type", "name")):
            raise ValueError(f"{path.relative_to(ROOT)} has an invalid catalog identity")
        quality = record.get("quality")
        if quality not in QUALITIES:
            raise ValueError(f"{path.relative_to(ROOT)} has invalid quality {quality!r}")
        if record.get("source") not in SOURCES:
            raise ValueError(f"{path.relative_to(ROOT)} has invalid source {record.get('source')!r}")
        catalog_id = record.get("catalog_id")
        if type(catalog_id) is not int or catalog_id in catalog_ids:
            raise ValueError(f"{path.relative_to(ROOT)} has an invalid or duplicate catalog_id")
        catalog_ids.add(catalog_id)
        qualities[quality] += 1
        schema_versions.add(record.get("schema_version"))

        outline = path.with_name("outline.svg")
        if quality == "unusable" and outline in file_set:
            raise ValueError(f"{outline.relative_to(ROOT)} must be omitted for an unusable record")
        if quality != "unusable" and outline not in file_set:
            raise ValueError(f"{outline.relative_to(ROOT)} is missing")
        if quality != "unusable":
            validate_outline(outline)

    metadata_directories = {path.parent for path in metadata_files}
    orphaned_outlines = [
        path for path in files if path.name == "outline.svg" and path.parent not in metadata_directories
    ]
    if orphaned_outlines:
        raise ValueError(f"{orphaned_outlines[0].relative_to(ROOT)} has no metadata")

    if len(schema_versions) != 1 or not metadata_files:
        raise ValueError("dataset must contain records with one schema version")

    catalog = json.loads((ROOT / "catalog_source.json").read_text(encoding="utf-8"))
    catalog["url"] = catalog.pop("url_template").format(version=catalog["version"])
    return {
        "dataset_version": version,
        "schema_version": schema_versions.pop(),
        "license": "CC0-1.0",
        "rights_notice": "dataset/NOTICE.md",
        "catalog": catalog,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "git_commit": git("rev-parse", "HEAD"),
        "records": {
            "total": len(metadata_files),
            "quality": {quality: qualities[quality] for quality in QUALITIES},
        },
    }


def release_notes(manifest: dict) -> str:
    records = manifest["records"]
    quality = records["quality"]
    catalog = manifest["catalog"]
    return f"""Silhouette dataset snapshot for Fantasy Toybox catalog v{catalog['version']}.

- Records: {records['total']}
- Good: {quality['good']}
- Bad perspective: {quality['bad_perspective']}
- Unusable: {quality['unusable']}
- Metadata format version: {manifest['schema_version']}
- Dataset dedication: CC0-1.0, to the extent contributors hold applicable rights

The attached ZIP contains the published `dataset/` tree and its snapshot manifest.
Third-party rights and the project's correction/removal process are described in
`dataset/NOTICE.md` inside the archive.
Use the attached SHA-256 file to verify the download.
"""


def build(version: str, output_dir: Path) -> tuple[Path, Path, Path]:
    validate_version(version)
    files = tracked_dataset_files()
    manifest = build_manifest(version, files)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"silicone-shadows-dataset-{version}.zip"

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for path in files:
            bundle.write(path, path.relative_to(ROOT))

    checksum = output_dir / f"{archive.name}.sha256"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    notes = output_dir / f"silicone-shadows-dataset-{version}-release-notes.md"
    notes.write_text(release_notes(manifest), encoding="utf-8")
    return archive, checksum, notes


def verify_release_assets(version: str, directory: Path, expected_commit: str) -> None:
    archive = directory / f"silicone-shadows-dataset-{version}.zip"
    checksum = directory / f"{archive.name}.sha256"
    checksum_parts = checksum.read_text(encoding="ascii").split()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if checksum_parts != [digest, archive.name]:
        raise RuntimeError("downloaded release checksum does not match its archive")

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        manifest = json.loads(bundle.read("manifest.json"))
    if manifest.get("dataset_version") != version:
        raise RuntimeError("downloaded release has the wrong dataset version")
    if manifest.get("git_commit") != expected_commit:
        raise RuntimeError("downloaded release was built from the wrong commit")
    if not {"dataset/LICENSE", "dataset/NOTICE.md"} <= names:
        raise RuntimeError("downloaded release is missing its license or rights notice")


def verify_uploaded_release(version: str, expected_commit: str) -> None:
    with TemporaryDirectory() as directory:
        subprocess.run(
            [
                "gh", "release", "download", version,
                "--dir", directory,
                "--pattern", f"silicone-shadows-dataset-{version}.zip*",
            ],
            cwd=ROOT,
            check=True,
        )
        verify_release_assets(version, Path(directory), expected_commit)


def ensure_publishable() -> str:
    if git("status", "--porcelain"):
        raise RuntimeError("commit all changes before publishing")
    try:
        upstream = git("rev-parse", "@{upstream}")
    except subprocess.CalledProcessError as error:
        raise RuntimeError("the current branch has no upstream") from error
    head = git("rev-parse", "HEAD")
    if head != upstream:
        raise RuntimeError("push the current commit before publishing")
    subprocess.run(["gh", "auth", "status"], cwd=ROOT, check=True)
    return head


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="release tag, for example v0.1.0")
    parser.add_argument("--draft", action="store_true", help="create a private GitHub draft release")
    args = parser.parse_args()

    try:
        archive, checksum, notes = build(args.version, ROOT / "dist")
        print(f"Built {archive.relative_to(ROOT)}")
        print(f"Built {checksum.relative_to(ROOT)}")
        print(f"Built {notes.relative_to(ROOT)}")
        if args.draft:
            head = ensure_publishable()
            subprocess.run(
                [
                    "gh", "release", "create", args.version,
                    str(archive), str(checksum),
                    "--target", head,
                    "--title", f"Dataset {args.version}",
                    "--notes-file", str(notes),
                    "--draft",
                ],
                cwd=ROOT,
                check=True,
            )
            verify_uploaded_release(args.version, head)
            print("Verified uploaded archive and checksum")
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
