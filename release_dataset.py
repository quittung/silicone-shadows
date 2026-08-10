#!/usr/bin/env python3
"""Build and optionally publish a versioned dataset snapshot."""

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parent
VERSION_PATTERN = re.compile(r"v\d+\.\d+\.\d+")
QUALITIES = ("good", "bad_perspective", "unusable")


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


def build_manifest(version: str, files: list[Path]) -> dict:
    metadata_files = [path for path in files if path.name == "metadata.json"]
    qualities: Counter[str] = Counter()
    schema_versions: set[int] = set()
    catalog_ids: set[int] = set()

    for path in metadata_files:
        record = json.loads(path.read_text(encoding="utf-8"))
        quality = record.get("quality")
        if quality not in QUALITIES:
            raise ValueError(f"{path.relative_to(ROOT)} has invalid quality {quality!r}")
        catalog_id = record.get("catalog_id")
        if not isinstance(catalog_id, int) or catalog_id in catalog_ids:
            raise ValueError(f"{path.relative_to(ROOT)} has an invalid or duplicate catalog_id")
        catalog_ids.add(catalog_id)
        qualities[quality] += 1
        schema_versions.add(record.get("schema_version"))

        outline = path.with_name("outline.svg")
        if quality == "unusable" and outline in files:
            raise ValueError(f"{outline.relative_to(ROOT)} must be omitted for an unusable record")
        if quality != "unusable" and outline not in files:
            raise ValueError(f"{outline.relative_to(ROOT)} is missing")

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
