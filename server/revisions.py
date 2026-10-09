"""Preservation-safe working copies and lossless SVG vector revisions."""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from fastapi import HTTPException

from .artifacts import (
    atomic_bytes,
    atomic_json,
    read_state,
)
from .models import (
    GuestMetadata,
    IndependentSubmission,
    IndependentUpdate,
    ReviewState,
)


def document(workspace, item_id: str) -> dict | None:
    path = workspace.paths(item_id)["directory"] / "revision.json"
    return json.loads(path.read_text()) if path.is_file() else None


def fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(directory)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def independent_record(
    workspace, item_id: str, records: dict | None = None
) -> tuple[dict, Path] | None:
    if item_id in workspace.catalog_by_stem:
        return None
    records = records if records is not None else workspace.independent_records()
    if workspace.hosted_store:
        row = workspace.hosted_store.submission(item_id)
        if row and row["kind"] == "independent":
            state = IndependentSubmission.model_validate_json(row["state_json"])
            metadata = workspace.independent_document(
                f"community:{item_id}", state.metadata
            )
            raw = json.loads(workspace.pending_paths(item_id)["metadata"].read_text())
            metadata.update(raw.get("record", {}))
            return metadata, workspace.pending_paths(item_id)["directory"]
        if row and row["kind"] == "independent_update":
            state = IndependentUpdate.model_validate_json(row["state_json"])
            record = records.get(state.record_id)
            if record:
                metadata = workspace.independent_document(
                    state.record_id, state.metadata
                )
                raw = json.loads(
                    workspace.pending_paths(item_id)["metadata"].read_text()
                )
                metadata.update(raw.get("record", {}))
                metadata.setdefault("variants", record[0].get("variants", {}))
                return metadata, workspace.pending_paths(item_id)[
                    "directory"
                ] if raw.get("outline_revision") else record[1]
    return records.get(item_id)


def saved_records(workspace, item_id: str):
    independent = independent_record(workspace, item_id)
    if independent:
        return [independent]
    published = workspace.published_item(item_id)
    if published:
        return published
    revision = document(workspace, item_id)
    root = workspace.paths(item_id)["directory"]
    if revision and revision.get("work_only"):
        return [(revision["records"][0], root / ".revision-original")]
    state = read_state(root / "metadata.json")
    if state.status == "done" and (
        not workspace.dataset_dir or not workspace.catalog_by_stem.get(item_id)
    ):
        from .variants import document_variants

        raw = json.loads((root / "metadata.json").read_text())
        record = {
            "quality": state.rating,
            "source": raw.get(
                "outline_source",
                "alternative" if (root / "alternative.png").exists() else "catalog",
            ),
        }
        entries = document_variants(workspace, item_id)
        if entries:
            record["variants"] = entries
        return [(record, root)]
    return None


def pending_item(workspace, item_id: str):
    if not workspace.hosted_store:
        return None
    direct = workspace.hosted_store.submission(item_id)
    if direct:
        return direct
    for row in workspace.hosted_store.submissions():
        if row["kind"] == "independent_update":
            state = IndependentUpdate.model_validate_json(row["state_json"])
            if state.record_id == item_id:
                return row
    return None


def require_current(workspace, item_id: str) -> dict | None:
    revision = document(workspace, item_id)
    if not revision:
        return None
    if revision.get("work_only"):
        backup = workspace.paths(item_id)["directory"] / ".revision-original"
        current = (
            fingerprint(backup)
            == revision["fingerprints"][str(workspace.paths(item_id)["directory"])]
        )
    elif revision["pending"]:
        row = workspace.hosted_store.submission(item_id)
        directory = workspace.pending_paths(item_id)["directory"]
        current = bool(row) and fingerprint(directory) == revision["fingerprint"]
    else:
        records = saved_records(workspace, item_id)
        current = bool(records) and all(
            fingerprint(directory) == revision["fingerprints"].get(str(directory))
            for _, directory in records
        )
    if not current:
        raise HTTPException(
            status_code=409,
            detail="The saved product changed during editing. Cancel and reopen it before saving.",
        )
    return revision


def start(workspace, item_id: str, user) -> None:
    existing = document(workspace, item_id)
    try:
        _start(workspace, item_id, user)
    except Exception:
        if not existing:
            cancel(workspace, item_id, force=True)
        raise


def _start(workspace, item_id: str, user) -> None:
    from . import variants

    pending = pending_item(workspace, item_id)
    if pending and (not user or not user.reviewer):
        raise HTTPException(
            status_code=403,
            detail="reviewer access required to edit a pending submission",
        )
    if pending and pending["item_id"] != item_id:
        raise HTTPException(
            status_code=409,
            detail="An update is already pending. Edit it from moderation.",
        )
    if document(workspace, item_id):
        require_current(workspace, item_id)
        return
    if pending:
        directory = workspace.pending_paths(item_id)["directory"]
        records = (
            saved_records(workspace, item_id) if pending["kind"] != "catalog" else None
        )
        metadata = (
            records[0][0]
            if records
            else json.loads((directory / "metadata.json").read_text())["records"][0]
        )
        records = (
            [metadata]
            if records
            else json.loads((directory / "metadata.json").read_text())["records"]
        )
        revision = {
            "pending": True,
            "fingerprint": fingerprint(directory),
            "records": records,
        }
    else:
        published = saved_records(workspace, item_id)
        if not published:
            raise HTTPException(status_code=400, detail="item has no saved result")
        metadata, directory = published[0]
        revision = {
            "pending": False,
            "fingerprints": {str(path): fingerprint(path) for _, path in published},
            "records": [record for record, _ in published],
        }
    revision["work_only"] = (
        not pending and directory == workspace.paths(item_id)["directory"]
    )
    revision["independent"] = (
        metadata.get("catalog_id") is None and "record_id" in metadata
    )
    revision["record_id"] = metadata.get("record_id")
    root = workspace.paths(item_id)["directory"]
    root.mkdir(parents=True, exist_ok=True)
    # Local sessions can retain their original photo, mask and paint layers.
    backup = root / ".revision-original"
    backup.mkdir()
    for path in list(root.iterdir()):
        if path == backup:
            continue
        if path.is_dir():
            shutil.copytree(path, backup / path.name)
        else:
            shutil.copyfile(path, backup / path.name)
    info = {
        "general": (directory / "outline.svg").is_file()
        or not metadata.get("variants"),
        "variants": {
            key: {"sizes": entry["sizes"]}
            for key, entry in metadata.get("variants", {}).items()
        },
    }
    atomic_json(root / "variants.json", info)
    for key in ([""] if info["general"] else []) + list(info["variants"]):
        target = variants.paths(workspace, item_id, key)
        entry = metadata["variants"][key] if key else metadata
        saved_svg = directory / (
            f"variants/{key}/outline.svg"
            if (pending or revision["work_only"]) and key
            else entry.get("file", "outline.svg")
            if key
            else "outline.svg"
        )
        if not saved_svg.is_file() and revision["independent"]:
            original_record = workspace.independent_records().get(revision["record_id"])
            if original_record:
                saved_svg = original_record[1] / (
                    entry["file"] if key else "outline.svg"
                )
        old = read_state(target["metadata"])
        editable = (
            not pending
            and target["source"].is_file()
            and target["rembg"].is_file()
            and old.main_length is not None
            and old.status == "done"
            and target["svg"].is_file()
            and saved_svg.is_file()
            and target["svg"].read_bytes() == saved_svg.read_bytes()
        )
        state = old if editable else ReviewState()
        state.rating = entry["quality"]
        state.status = "pending"
        state.re_review = True
        raw = {"outline_source": entry.get("source", metadata.get("source", "catalog"))}
        target["directory"].mkdir(parents=True, exist_ok=True)
        if saved_svg.is_file():
            from .svg_revision import initialize_outline

            reference = directory / (
                f"variants/{key}/alternative.png"
                if pending and key
                else "alternative.png"
            )
            if (
                not reference.is_file()
                and target["svg"].is_file()
                and target["svg"].read_bytes() == saved_svg.read_bytes()
            ):
                saved_layers = backup / "variants" / key if key else backup
                reference = (
                    saved_layers / "alternative.png"
                    if (saved_layers / "alternative.png").is_file()
                    else saved_layers / "source.png"
                )
            raw.update(
                initialize_outline(target, saved_svg, state, editable, reference)
            )
        else:
            state.main_length = None
            raw["waiting_image"] = True
        atomic_json(target["metadata"], raw | state.model_dump(mode="json"))
    # A variant-only product still needs a root state to identify its working copy.
    if not info["general"]:
        atomic_json(
            workspace.paths(item_id)["metadata"],
            ReviewState(re_review=True).model_dump(mode="json"),
        )
    atomic_json(root / "revision.json", revision)


def cancel(workspace, item_id: str, force: bool = False) -> None:
    root = workspace.paths(item_id)["directory"]
    backup = root / ".revision-original"
    if not backup.is_dir() or (not force and not document(workspace, item_id)):
        return
    for path in list(root.iterdir()):
        if path == backup:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    for path in list(backup.iterdir()):
        path.replace(root / path.name)
    backup.rmdir()


def finish(workspace, item_id: str) -> None:
    root = workspace.paths(item_id)["directory"]
    metadata = workspace.paths(item_id)["metadata"]
    if metadata.is_file():
        raw = json.loads(metadata.read_text())
        atomic_json(metadata, raw | {"re_review": False})
    (root / "revision.json").unlink(missing_ok=True)
    shutil.rmtree(root / ".revision-original", ignore_errors=True)
    for directory in [root] + list((root / "variants").glob("*")):
        for path in directory.glob("revision*.*"):
            path.unlink()


def stage_submission(workspace, item_id: str) -> dict:
    destination = workspace.pending_paths(item_id)
    staged = Path(
        tempfile.mkdtemp(prefix=".revision-", dir=destination["directory"].parent)
    )
    return {
        key: staged if key == "directory" else staged / path.name
        for key, path in destination.items()
    }


def commit_submission(
    workspace,
    item_id: str,
    staged: Path,
    user,
    source: str,
    state_json: str,
    replace: bool,
    kind: str = "catalog",
) -> None:
    """Swap the complete bundle and roll back if its database update fails."""
    destination = workspace.pending_paths(item_id)["directory"]
    with (
        workspace.session_lock,
        tempfile.TemporaryDirectory(
            prefix=".previous-", dir=destination.parent
        ) as backup_directory,
    ):
        require_current(workspace, item_id)
        backup = Path(backup_directory) / "submission"
        if destination.exists():
            destination.replace(backup)
        try:
            staged.replace(destination)
            workspace.hosted_store.save_submission_revision(
                item_id, user, source, state_json, replace=replace, kind=kind
            )
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            if backup.exists():
                backup.replace(destination)
            raise


def catalog_document(
    workspace, revision: dict | None, product: dict, state: ReviewState, source: str
) -> dict:
    previous = (
        next(
            (
                record
                for record in revision["records"]
                if record.get("catalog_id") == product["id"]
            ),
            {},
        )
        if revision
        else {}
    )
    document = previous | workspace.record_document(product, state, source)
    document.pop("variants", None)
    return document


def independent_metadata(workspace, item_id: str) -> GuestMetadata:
    revision = document(workspace, item_id)
    record = (
        revision["records"][0]
        if revision
        else independent_record(workspace, item_id)[0]
    )
    return GuestMetadata.model_validate(record)


def save_independent(workspace, item_id: str, state: ReviewState, entries: dict, user):
    """Save a complete independent revision, retaining its stable record identity."""
    revision = require_current(workspace, item_id)
    if state.rating == "unusable" and not any(
        entry["quality"] != "unusable" for entry in entries.values()
    ):
        raise HTTPException(
            status_code=400,
            detail="Independent products need at least one usable outline.",
        )
    metadata = independent_metadata(workspace, item_id)
    if state.rating != "unusable":
        metadata.quality = state.rating
    record = workspace.independent_document(revision["record_id"], metadata)
    record["source"] = revision["records"][0].get("source", "alternative")
    if entries:
        record["variants"] = entries
    root = workspace.paths(item_id)
    store = workspace.hosted_store
    if store:
        staged = stage_submission(workspace, item_id)["directory"]
        update = workspace.independent_records().get(revision["record_id"])
        kind = "independent_update" if update else "independent"
        submission = (
            IndependentUpdate(record_id=revision["record_id"], metadata=metadata)
            if update
            else IndependentSubmission(metadata=metadata, main_length=state.main_length)
        )
        atomic_json(
            staged / "metadata.json",
            {
                "outline_revision": True,
                "record": record,
                **submission.model_dump(mode="json"),
            },
        )
        if state.rating != "unusable":
            atomic_bytes(staged / "outline.svg", root["svg"].read_bytes())
        for key, entry in entries.items():
            if entry["quality"] != "unusable":
                origin = root["directory"] / "variants" / key
                atomic_bytes(
                    staged / "variants" / key / "outline.svg",
                    (origin / "outline.svg").read_bytes(),
                )
                photo = (
                    origin / "alternative.png"
                    if (origin / "alternative.png").is_file()
                    else origin / "revision-photo.png"
                )
                if photo.is_file():
                    atomic_bytes(
                        staged / "variants" / key / "alternative.png",
                        photo.read_bytes(),
                    )
        photo = (
            root["alternative"]
            if root["alternative"].is_file()
            else root["directory"] / "revision-photo.png"
        )
        if photo.is_file():
            atomic_bytes(staged / "alternative.png", photo.read_bytes())
        try:
            commit_submission(
                workspace,
                item_id,
                staged,
                user,
                "alternative",
                submission.model_dump_json(),
                replace=revision["pending"],
                kind=kind,
            )
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        workspace.discard_work(item_id)
    else:
        publish_independent_revision(
            workspace,
            record,
            root["directory"],
            include_general=state.rating != "unusable",
        )
        finish(workspace, item_id)


def publish_independent_revision(
    workspace, record: dict, source: Path, include_general: bool = True
):
    from .catalog import slug
    from .variants import validate_variants

    entries = record.get("variants", {})
    validate_variants(entries, {s["label"] for s in record["sizes"]})
    previous = workspace.independent_records().get(record["record_id"])
    directory = (
        workspace.dataset_dir
        / slug(record["vendor"])
        / slug(record["product_type"])
        / slug(record["name"])
    )
    if directory.exists() and (not previous or directory != previous[1]):
        directory = directory.with_name(
            f"{directory.name}--{slug(record['record_id'])}"
        )
    staged = Path(
        tempfile.mkdtemp(prefix=".publication-", dir=workspace.dataset_dir.parent)
    )
    atomic_json(staged / "metadata.json", record)
    if include_general and (source / "outline.svg").is_file():
        atomic_bytes(staged / "outline.svg", (source / "outline.svg").read_bytes())
    for key, entry in entries.items():
        if entry["quality"] != "unusable":
            atomic_bytes(
                staged / entry["file"],
                (source / "variants" / key / "outline.svg").read_bytes(),
            )
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".previous-", dir=workspace.dataset_dir.parent
    ) as backup_directory:
        backup = Path(backup_directory) / "record"
        if directory.exists():
            directory.replace(backup)
        try:
            staged.replace(directory)
        except Exception:
            if backup.exists():
                backup.replace(directory)
            raise
    if previous and previous[1] != directory:
        shutil.rmtree(previous[1])
    workspace.dataset_changed()
