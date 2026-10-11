"""Validate and commit a complete review, shared by HTTP transports."""

import json
import shutil
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response
from PIL import Image

from . import revisions, variants as variant_store
from .artifacts import atomic_bytes, atomic_image, atomic_json, validate_length
from .models import ReviewState
from .workspace import MAX_IMAGE_BYTES


def save_review(
    workspace,
    item_id,
    user,
    state_json,
    download_only=False,
    edits=None,
    variant="",
    refresh_comparison=None,
):
    store = workspace.hosted_store
    refresh_comparison = refresh_comparison or workspace.dataset_changed
    revision = revisions.require_current(workspace, item_id)
    paths = variant_store.paths(workspace, item_id, variant)
    raw = (
        json.loads(paths["metadata"].read_text()) if paths["metadata"].exists() else {}
    )
    preserved = (
        (raw.get("preserved") or raw.get("svg_canvas"))
        and paths["svg"].is_file()
        and not paths["source"].is_file()
    )
    try:
        submitted = ReviewState.model_validate_json(state_json)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if preserved or (submitted.rating == "unusable" and not paths["source"].is_file()):
        width = height = 0
    else:
        paths, width, height = workspace.prepare(item_id, variant=variant)
    try:
        state = ReviewState.model_validate_json(state_json)
        if revision:
            state.re_review = True
        if preserved and raw.get("svg_canvas"):
            validate_length(
                state.main_length,
                raw["svg_canvas"]["width"],
                raw["svg_canvas"]["height"],
            )
        elif not preserved:
            validate_length(state.main_length, width, height)
    except (ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if state.status == "done" and state.rating is None:
        raise HTTPException(status_code=400, detail="a rating is required")
    if (
        state.status == "done"
        and state.rating != "unusable"
        and state.main_length is None
        and not preserved
    ):
        raise HTTPException(
            status_code=400, detail="usable items require a main-length line"
        )

    if edits is not None:
        data = edits
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="paint layer is too large")
        try:
            with Image.open(BytesIO(data)) as image:
                paint = image.convert("RGBA")
                paint.load()
        except OSError as error:
            raise HTTPException(status_code=400, detail="invalid paint PNG") from error
        if paint.size != (width, height):
            raise HTTPException(
                status_code=400, detail="paint layer dimensions do not match"
            )
        atomic_image(paths["edits"], paint)

    snapshots = {}
    if download_only:
        for key in [""] + list(
            variant_store.collection(workspace, item_id)["variants"]
        ):
            metadata_path = variant_store.paths(workspace, item_id, key)["metadata"]
            snapshots[metadata_path] = (
                metadata_path.read_bytes() if metadata_path.exists() else None
            )
    source_kind = raw.get(
        "outline_source",
        "alternative" if paths["alternative"].exists() else "catalog",
    )
    document = raw | {
        "version": 1,
        "id": item_id,
        "outline_source": source_kind,
        **state.model_dump(mode="json"),
    }
    try:
        atomic_json(paths["metadata"], document)
        entries = None
        if state.status == "done":
            try:
                general_state, entries = variant_store.finish_collection(
                    workspace, item_id
                )
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            state = general_state
            if not download_only and not revision:
                state.re_review = False
            root_paths = workspace.paths(item_id)
            root_raw = (
                json.loads(root_paths["metadata"].read_text())
                if root_paths["metadata"].exists()
                else {}
            )
            atomic_json(
                root_paths["metadata"], root_raw | state.model_dump(mode="json")
            )
            source_kind = root_raw.get(
                "outline_source",
                "alternative" if root_paths["alternative"].exists() else "catalog",
            )
            paths = root_paths
        if download_only:
            if state.rating == "unusable" and not any(
                e["quality"] != "unusable" for e in (entries or {}).values()
            ):
                raise HTTPException(
                    status_code=400,
                    detail="an unusable item has no silhouette to download",
                )
            archive = BytesIO()
            products = workspace.catalog_by_stem.get(item_id, [])
            with zipfile.ZipFile(
                archive, "w", compression=zipfile.ZIP_DEFLATED
            ) as bundle:
                documents = [
                    (
                        "metadata.json"
                        if len(products) == 1
                        else f"metadata-{product['id']}.json",
                        workspace.catalog_download_document(
                            product, state, source_kind
                        ),
                    )
                    for product in products
                ]
                if revision and revision["independent"]:
                    metadata = revisions.independent_metadata(
                        workspace, item_id
                    ).model_dump(mode="json")
                    metadata["sizes"] = [
                        size.in_inches()
                        for size in revisions.independent_metadata(
                            workspace, item_id
                        ).sizes
                    ]
                    documents = [("metadata.json", metadata)]
                for name, metadata in documents:
                    if entries:
                        metadata.update(schema_version=2, variants=entries)
                    bundle.writestr(
                        name,
                        json.dumps(metadata, indent=2) + "\n",
                    )
                if state.rating != "unusable":
                    bundle.writestr("outline.svg", paths["svg"].read_bytes())
                for key, entry in (entries or {}).items():
                    if entry["quality"] != "unusable":
                        bundle.writestr(
                            entry["file"],
                            variant_store.paths(workspace, item_id, key)[
                                "svg"
                            ].read_bytes(),
                        )
            return Response(
                archive.getvalue(),
                media_type="application/zip",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": f'attachment; filename="{item_id}.zip"',
                },
            )
    finally:
        for metadata_path, previous in snapshots.items():
            if previous is None:
                metadata_path.unlink(missing_ok=True)
            else:
                atomic_bytes(metadata_path, previous)
    if revision and revision["independent"] and state.status == "done":
        revisions.save_independent(workspace, item_id, state, entries or {}, user)
        if not store:
            refresh_comparison()
        return workspace.item_summary(
            item_id,
            workspace.queue_items().get(item_id, Path(item_id)),
            user,
            store.claims() if store else {},
        )
    if store and state.status == "done":
        if store.submission(item_id) and not (revision and revision["pending"]):
            raise HTTPException(
                status_code=409, detail="item is already pending review"
            )
        pending = revisions.stage_submission(workspace, item_id)
        atomic_json(
            pending["metadata"],
            {
                "version": 1,
                "item_id": item_id,
                "source": source_kind,
                "state": state.model_dump(mode="json"),
                "records": [
                    (
                        revisions.catalog_document(
                            workspace, revision, product, state, source_kind
                        )
                        | (
                            {"schema_version": 2, "variants": entries}
                            if entries
                            else {}
                        )
                    )
                    for product in workspace.catalog_by_stem.get(item_id, [])
                ],
            },
        )
        if state.rating != "unusable":
            atomic_bytes(pending["svg"], paths["svg"].read_bytes())
        for key, entry in (entries or {}).items():
            if entry["quality"] != "unusable":
                target = pending["directory"] / "variants" / key
                atomic_bytes(
                    target / "outline.svg",
                    variant_store.paths(workspace, item_id, key)["svg"].read_bytes(),
                )
                variant_paths = variant_store.paths(workspace, item_id, key)
                alternative = variant_paths["alternative"]
                if not alternative.exists():
                    alternative = variant_paths["directory"] / "revision-photo.png"
                if alternative.exists():
                    atomic_bytes(target / "alternative.png", alternative.read_bytes())
        alternative = (
            paths["alternative"]
            if paths["alternative"].exists()
            else paths["directory"] / "revision-photo.png"
        )
        if alternative.exists():
            atomic_bytes(pending["alternative"], alternative.read_bytes())
        try:
            revisions.commit_submission(
                workspace,
                item_id,
                pending["directory"],
                user,
                source_kind,
                state.model_dump_json(),
                replace=bool(revision and revision["pending"]),
            )
        except Exception:
            shutil.rmtree(pending["directory"], ignore_errors=True)
            raise
        workspace.select_prefetch(user.id, [])
        workspace.discard_work(item_id)
    if state.status == "done" and not store:
        workspace.publish(
            item_id,
            state,
            paths["svg"] if state.rating != "unusable" else None,
            variants=entries,
            variants_directory=paths["directory"],
        )
        revisions.finish(workspace, item_id)
        refresh_comparison()
    elif not store and not state.re_review and not variant:
        workspace.unpublish(item_id)
    return workspace.item_summary(
        item_id,
        workspace.queue_items()[item_id],
        user,
        store.claims() if store else {},
    )
