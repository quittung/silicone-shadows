#!/usr/bin/env python3
"""Build and optionally publish a versioned dataset snapshot."""

import argparse
import hashlib
import io
import json
import math
import re
import subprocess
import tarfile
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
RELEASE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
INTEGER_VERSION_PATTERN = re.compile(r"v(\d+)")
LEGACY_VERSION_PATTERN = re.compile(r"v0\.(\d+)\.0")
QUALITIES = ("good", "bad_perspective", "unusable")
SOURCES = ("catalog", "alternative")
CATALOG_METADATA_FIELDS = {"schema_version", "catalog_id", "quality", "source"}
INDEPENDENT_METADATA_FIELDS = CATALOG_METADATA_FIELDS | {
    "record_id",
    "vendor",
    "product_type",
    "name",
    "product_url",
    "species",
    "tags",
    "features",
    "sizes",
    "notes",
}
DATASET_ROOT_FILES = {Path("dataset/LICENSE"), Path("dataset/NOTICE.md")}
HOSTED_DATASET_PATH = "/var/lib/silicone-shadows/dataset"
ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SSH_USER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SSH_SERVER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def validate_release_name(version: str) -> None:
    if not RELEASE_NAME_PATTERN.fullmatch(version):
        raise ValueError(
            "release name must use letters, numbers, dots, dashes, or underscores"
        )


def next_release_name() -> str:
    releases = json.loads(
        subprocess.check_output(
            ["gh", "release", "list", "--limit", "1000", "--json", "tagName"],
            cwd=ROOT,
            text=True,
        )
    )
    numbers = []
    for release in releases:
        tag = release.get("tagName", "")
        match = (
            INTEGER_VERSION_PATTERN.fullmatch(tag)
            or LEGACY_VERSION_PATTERN.fullmatch(tag)
        )
        if match:
            numbers.append(int(match.group(1)))
    return f"v{max(numbers, default=0) + 1}"


def tracked_dataset_files() -> list[Path]:
    files = [ROOT / line for line in git("ls-files", "--", "dataset").splitlines()]
    if not files:
        raise RuntimeError("dataset contains no tracked files")
    return sorted(files)


def dataset_files() -> list[Path]:
    return sorted(path for path in (ROOT / "dataset").rglob("*") if path.is_file())


def snapshot(contents: list[tuple[str, bytes]]) -> tuple[dict[str, str], Counter[str]]:
    files = {}
    qualities: Counter[str] = Counter()
    for name, content in contents:
        files[name] = hashlib.sha256(content).hexdigest()
        if name.endswith("/metadata.json"):
            quality = json.loads(content).get("quality")
            if quality not in QUALITIES:
                raise ValueError(f"{name} has invalid quality {quality!r}")
            qualities[quality] += 1
    return files, qualities


def directory_snapshot(root: Path) -> tuple[dict[str, str], Counter[str]]:
    return snapshot(
        [
            (path.relative_to(root).as_posix(), path.read_bytes())
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]
    )


def latest_release_snapshot() -> tuple[str, tuple[dict[str, str], Counter[str]]]:
    release = json.loads(
        subprocess.check_output(
            ["gh", "release", "view", "--json", "tagName,targetCommitish"],
            cwd=ROOT,
            text=True,
        )
    )
    tag = release["tagName"]
    refs = (tag, release["targetCommitish"])
    commit = None
    for ref in refs:
        try:
            commit = git("rev-parse", "--verify", f"{ref}^{{commit}}")
            break
        except subprocess.CalledProcessError:
            pass
    if commit is None:
        raise RuntimeError(f"release {tag} is not available in the local git history")

    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", commit, "dataset"], cwd=ROOT
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        contents = []
        for member in bundle.getmembers():
            if not member.isfile() or not member.name.startswith("dataset/"):
                continue
            source = bundle.extractfile(member)
            if source is not None:
                contents.append((member.name.removeprefix("dataset/"), source.read()))
    return tag, snapshot(contents)


def report_snapshot(
    label: str, current: tuple[dict[str, str], Counter[str]]
) -> None:
    files, qualities = current
    quality = ", ".join(f"{name}={qualities[name]}" for name in QUALITIES)
    print(f"{label}: {sum(qualities.values())} records ({quality}), {len(files)} files")


def report_difference(
    label: str,
    before: tuple[dict[str, str], Counter[str]],
    after: tuple[dict[str, str], Counter[str]],
) -> None:
    before_files, before_qualities = before
    after_files, after_qualities = after
    added = after_files.keys() - before_files.keys()
    removed = before_files.keys() - after_files.keys()
    changed = {
        name
        for name in before_files.keys() & after_files.keys()
        if before_files[name] != after_files[name]
    }
    record_delta = sum(after_qualities.values()) - sum(before_qualities.values())
    if not added and not changed and not removed:
        print(f"{label}: no changes")
        return
    print(
        f"{label}: {record_delta:+d} records; "
        f"{len(added)} files added, {len(changed)} changed, {len(removed)} removed"
    )


def check_state(include_hosted: bool) -> None:
    tag, released = latest_release_snapshot()
    local = directory_snapshot(ROOT / "dataset")
    report_snapshot(f"Latest release {tag}", released)
    report_snapshot("Local", local)
    report_difference(f"Local vs {tag}", released, local)

    if include_hosted:
        with TemporaryDirectory() as directory:
            hosted_root = Path(directory) / "dataset"
            subprocess.run(
                [
                    "rsync",
                    "--recursive",
                    "--quiet",
                    hosted_dataset_source(),
                    f"{hosted_root}/",
                ],
                cwd=ROOT,
                check=True,
            )
            hosted = directory_snapshot(hosted_root)
        report_snapshot("Hosted", hosted)
        report_difference(f"Hosted vs {tag}", released, hosted)
        report_difference("Hosted vs local", local, hosted)
    print("Check complete; no project or release state changed")


def hosted_dataset_source(env_path: Path = ROOT / ".env") -> str:
    if not env_path.is_file():
        raise RuntimeError(f"missing {env_path.name} with hosted server settings")
    values = {}
    for number, raw_line in enumerate(env_path.read_text().splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not ENV_NAME_PATTERN.fullmatch(key):
            raise ValueError(f"{env_path.name}:{number} is not a valid setting")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    user = values.get("SILICONE_SHADOWS_USER", "")
    server = values.get("SILICONE_SHADOWS_SERVER", "")
    if not SSH_USER_PATTERN.fullmatch(user):
        raise ValueError("SILICONE_SHADOWS_USER is missing or invalid")
    if not SSH_SERVER_PATTERN.fullmatch(server):
        raise ValueError("SILICONE_SHADOWS_SERVER is missing or invalid")
    return f"{user}@{server}:{HOSTED_DATASET_PATH}/"


def sync_hosted_dataset(version: str) -> bool:
    if git("status", "--porcelain"):
        raise RuntimeError("commit or stash existing changes before syncing")
    before_count = sum(path.name == "metadata.json" for path in tracked_dataset_files())
    subprocess.run(
        [
            "rsync",
            "--recursive",
            "--checksum",
            "--delete",
            "--itemize-changes",
            hosted_dataset_source(),
            f"{ROOT / 'dataset'}/",
        ],
        cwd=ROOT,
        check=True,
    )
    files = dataset_files()
    build_manifest(version, files)
    changes = git("status", "--short", "--", "dataset")
    if not changes:
        print("Hosted dataset is already current", flush=True)
        return False
    print(changes, flush=True)
    subprocess.run(["git", "add", "-A", "--", "dataset"], cwd=ROOT, check=True)
    staged = git("diff", "--cached", "--name-only")
    if not staged or any(
        not path.startswith("dataset/") for path in staged.splitlines()
    ):
        raise RuntimeError("refusing to commit changes outside dataset/")
    after_count = sum(path.name == "metadata.json" for path in files)
    added = after_count - before_count
    message = (
        f"Add {added} reviewed silhouettes" if added > 0 else "Sync hosted dataset"
    )
    subprocess.run(
        ["git", "commit", "-m", message, "--", "dataset"], cwd=ROOT, check=True
    )
    return True


def validate_outline(path: Path, require_main_length: bool = True) -> None:
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

    paths = [
        element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "path"
    ]
    if len(paths) != 1 or paths[0].get("id") != "outline":
        raise ValueError(f"{label} must contain one outline path")
    line = next(
        (element for element in root.iter() if element.get("id") == "main-length"), None
    )
    if line is None and not require_main_length:
        return
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
    record_ids: set[str] = set()

    for path in metadata_files:
        record = json.loads(path.read_text(encoding="utf-8"))
        independent = record.get("catalog_id") is None
        expected_fields = (
            INDEPENDENT_METADATA_FIELDS if independent else CATALOG_METADATA_FIELDS
        )
        if set(record) != expected_fields:
            raise ValueError(f"{path.relative_to(ROOT)} has unexpected metadata fields")
        if type(record["schema_version"]) is not int:
            raise ValueError(f"{path.relative_to(ROOT)} has an invalid schema version")
        quality = record.get("quality")
        if quality not in QUALITIES:
            raise ValueError(
                f"{path.relative_to(ROOT)} has invalid quality {quality!r}"
            )
        if record.get("source") not in SOURCES:
            raise ValueError(
                f"{path.relative_to(ROOT)} has invalid source {record.get('source')!r}"
            )
        catalog_id = record.get("catalog_id")
        if independent:
            if any(
                not isinstance(record[field], str) or not record[field]
                for field in ("vendor", "product_type", "name")
            ):
                raise ValueError(
                    f"{path.relative_to(ROOT)} has an invalid independent identity"
                )
            record_id = record.get("record_id")
            if (
                not isinstance(record_id, str)
                or not record_id
                or record_id in record_ids
            ):
                raise ValueError(
                    f"{path.relative_to(ROOT)} has an invalid or duplicate record_id"
                )
            record_ids.add(record_id)
            if not isinstance(record.get("sizes"), list):
                raise ValueError(f"{path.relative_to(ROOT)} has invalid sizes")
        else:
            if type(catalog_id) is not int or catalog_id in catalog_ids:
                raise ValueError(
                    f"{path.relative_to(ROOT)} has an invalid or duplicate catalog_id"
                )
            catalog_ids.add(catalog_id)
        qualities[quality] += 1
        schema_versions.add(record.get("schema_version"))

        outline = path.with_name("outline.svg")
        if quality == "unusable" and outline in file_set:
            raise ValueError(
                f"{outline.relative_to(ROOT)} must be omitted for an unusable record"
            )
        if quality != "unusable" and outline not in file_set:
            raise ValueError(f"{outline.relative_to(ROOT)} is missing")
        if quality != "unusable":
            validate_outline(outline, require_main_length=not independent)

    metadata_directories = {path.parent for path in metadata_files}
    orphaned_outlines = [
        path
        for path in files
        if path.name == "outline.svg" and path.parent not in metadata_directories
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
        "created_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
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
    return f"""Silhouette dataset snapshot for Fantasy Toybox catalog v{catalog["version"]}.

- Records: {records["total"]}
- Good: {quality["good"]}
- Bad perspective: {quality["bad_perspective"]}
- Unusable: {quality["unusable"]}
- Metadata format version: {manifest["schema_version"]}
- Dataset dedication: CC0-1.0, to the extent contributors hold applicable rights

The attached ZIP contains the published `dataset/` tree and its snapshot manifest.
Third-party rights and the project's correction/removal process are described in
`dataset/NOTICE.md` inside the archive.
"""


def build(version: str, output_dir: Path) -> tuple[Path, Path]:
    validate_release_name(version)
    files = tracked_dataset_files()
    manifest = build_manifest(version, files)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"silicone-shadows-dataset-{version}.zip"

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for path in files:
            bundle.write(path, path.relative_to(ROOT))

    notes = output_dir / f"silicone-shadows-dataset-{version}-release-notes.md"
    notes.write_text(release_notes(manifest), encoding="utf-8")
    return archive, notes


def verify_release_assets(
    version: str, directory: Path, expected_commit: str, expected_digest: str
) -> None:
    archive = directory / f"silicone-shadows-dataset-{version}.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != expected_digest:
        raise RuntimeError("downloaded release does not match its GitHub digest")

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
    archive_name = f"silicone-shadows-dataset-{version}.zip"
    release = subprocess.run(
        ["gh", "release", "view", version, "--json", "assets"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assets = json.loads(release.stdout)["assets"]
    asset = next((item for item in assets if item["name"] == archive_name), None)
    if not asset or not asset.get("digest", "").startswith("sha256:"):
        raise RuntimeError("GitHub did not report a SHA-256 digest for the archive")
    expected_digest = asset["digest"].removeprefix("sha256:")
    with TemporaryDirectory() as directory:
        subprocess.run(
            [
                "gh",
                "release",
                "download",
                version,
                "--dir",
                directory,
                "--pattern",
                archive_name,
            ],
            cwd=ROOT,
            check=True,
        )
        verify_release_assets(version, Path(directory), expected_commit, expected_digest)


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
    parser.add_argument(
        "version",
        nargs="?",
        help="release name; defaults to the next vN from GitHub",
    )
    parser.add_argument(
        "--draft", action="store_true", help="create a private GitHub draft release"
    )
    parser.add_argument(
        "--sync-hosted",
        action="store_true",
        help="sync and commit hosted data, or include it in --check",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="push the hosted dataset commit before creating a draft",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the local dataset with the latest published release",
    )
    args = parser.parse_args()

    if args.push and not args.sync_hosted:
        parser.error("--push requires --sync-hosted")
    if args.sync_hosted and args.draft and not args.push:
        parser.error("--sync-hosted with --draft also requires --push")
    if args.check and (args.version or args.draft or args.push):
        parser.error("--check cannot be combined with release actions or a version")

    try:
        if args.check:
            check_state(args.sync_hosted)
            return
        version = args.version or next_release_name()
        validate_release_name(version)
        print(f"Using release name {version}")
        if args.sync_hosted:
            sync_hosted_dataset(version)
        if args.push:
            subprocess.run(["git", "push"], cwd=ROOT, check=True)
        archive, notes = build(version, ROOT / "dist")
        print(f"Built {archive.relative_to(ROOT)}")
        print(f"Built {notes.relative_to(ROOT)}")
        if args.draft:
            head = ensure_publishable()
            subprocess.run(
                [
                    "gh",
                    "release",
                    "create",
                    version,
                    str(archive),
                    "--target",
                    head,
                    "--title",
                    f"Dataset {version}",
                    "--notes-file",
                    str(notes),
                    "--draft",
                ],
                cwd=ROOT,
                check=True,
            )
            verify_uploaded_release(version, head)
            print("Verified uploaded archive and GitHub digest")
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
