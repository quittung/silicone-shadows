"""Outline collections; legacy root outline remains the general fallback."""

import json
import re
import shutil
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .artifacts import atomic_json, item_paths, read_state
from .models import ReviewState

ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")


class VariantChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    id: str = ""
    sizes: list[str] = Field(default_factory=list)
    move_from: str | None = None


def size_key(size: dict, independent: bool = False) -> str:
    # Full catalog labels survive sorting and avoid ambiguous abbreviated labels.
    return str(
        (size.get("label") if independent else size.get("sl"))
        or (size.get("short_label") if independent else size.get("ShortLabel"))
        or ""
    )


def validate_variants(variants: dict, known_sizes: set[str] | None = None) -> None:
    if not isinstance(variants, dict):
        raise ValueError("variants must be an object")
    assigned = set()
    for key, entry in variants.items():
        if not ID_PATTERN.fullmatch(key) or key == "general":
            raise ValueError("invalid variant ID")
        if not isinstance(entry, dict) or set(entry) - {
            "sizes",
            "quality",
            "source",
            "file",
        }:
            raise ValueError("invalid variant fields")
        if "quality" in entry and entry["quality"] not in {
            "good",
            "bad_perspective",
            "unusable",
        }:
            raise ValueError("invalid variant quality")
        if "source" in entry and entry["source"] not in {"catalog", "alternative"}:
            raise ValueError("invalid variant source")
        sizes = entry.get("sizes")
        if (
            not isinstance(sizes, list)
            or not sizes
            or any(not isinstance(s, str) or not s for s in sizes)
        ):
            raise ValueError("each variant must have at least one size")
        if len(set(sizes)) != len(sizes) or assigned.intersection(sizes):
            raise ValueError("a size can belong to only one variant")
        if known_sizes is not None and not set(sizes) <= known_sizes:
            raise ValueError("variant refers to an unknown size")
        assigned.update(sizes)
        if "file" in entry and entry["file"] != f"variants/{key}.svg":
            raise ValueError("invalid variant outline path")


def collection(workspace, item_id: str) -> dict:
    root = workspace.paths(item_id)["directory"]
    manifest = root / "variants.json"
    if manifest.is_file():
        return json.loads(manifest.read_text())
    return {"general": True, "variants": {}}


def paths(workspace, item_id: str, variant: str = "") -> dict:
    if not variant:
        return workspace.paths(item_id)
    if variant not in collection(workspace, item_id)["variants"]:
        raise HTTPException(status_code=404, detail="unknown variant")
    return item_paths(workspace.paths(item_id)["directory"] / "variants", variant)


def initialize(workspace, item_id: str) -> None:
    root = workspace.paths(item_id)["directory"]
    if (root / "variants.json").exists():
        return
    published = workspace.published_item(item_id)
    result = {"general": True, "variants": {}}
    if published:
        metadata, directory = published[0]
        result["general"] = (directory / "outline.svg").is_file() or not metadata.get(
            "variants"
        )
        result["variants"] = {
            key: {"sizes": entry["sizes"]}
            for key, entry in metadata.get("variants", {}).items()
        }
        atomic_json(root / "variants.json", result)
        for key, entry in metadata.get("variants", {}).items():
            target = paths(workspace, item_id, key)
            target["directory"].mkdir(parents=True, exist_ok=True)
            svg = directory / entry["file"]
            if svg.is_file():
                shutil.copyfile(svg, target["svg"])
            old = read_state(target["metadata"])
            editable = (
                target["source"].is_file()
                and target["rembg"].is_file()
                and old.main_length is not None
            )
            restored = (
                old if editable else ReviewState(status="done", rating=entry["quality"])
            )
            restored.re_review = True
            atomic_json(
                target["metadata"],
                restored.model_dump(mode="json")
                | {"outline_source": entry["source"], "preserved": not editable},
            )
    atomic_json(root / "variants.json", result)


def published_entries(metadata: dict, directory: Path) -> list[tuple[str, dict, Path]]:
    result = []
    if metadata.get("quality") != "unusable" and (directory / "outline.svg").is_file():
        result.append(("", metadata, directory / "outline.svg"))
    for key, entry in metadata.get("variants", {}).items():
        path = directory / entry["file"]
        if entry.get("quality") != "unusable" and path.is_file():
            result.append((key, entry, path))
    return result


def document_variants(workspace, item_id: str) -> dict:
    result = {}
    for key, entry in collection(workspace, item_id)["variants"].items():
        target = paths(workspace, item_id, key)
        state = read_state(target["metadata"])
        raw = (
            json.loads(target["metadata"].read_text())
            if target["metadata"].exists()
            else {}
        )
        result[key] = {
            "sizes": entry["sizes"],
            "quality": state.rating,
            "source": raw.get(
                "outline_source",
                "alternative" if target["alternative"].exists() else "catalog",
            ),
            "file": f"variants/{key}.svg",
        }
    return result


def write_variants(directory: Path, entries: dict, source_directory: Path) -> None:
    target = directory / "variants"
    if target.is_dir():
        shutil.rmtree(target)
    for key, entry in entries.items():
        if entry["quality"] != "unusable":
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                source_directory / "variants" / key / "outline.svg",
                directory / entry["file"],
            )


def copy_outline(origin: dict, destination: dict) -> None:
    """Move/copy an outline's image layers and SVG revision baseline together."""
    destination["directory"].mkdir(parents=True, exist_ok=True)
    for name, source in origin.items():
        if name != "directory" and source.is_file():
            shutil.copyfile(source, destination[name])
    from .svg_revision import OUTLINE_REVISION_FILES

    for name in OUTLINE_REVISION_FILES:
        target = destination["directory"] / name
        target.unlink(missing_ok=True)
        source = origin["directory"] / name
        if source.is_file():
            shutil.copyfile(source, target)


def register(app, workspace) -> None:
    from fastapi import Request
    from urllib.parse import quote

    def guard(item_id, request):
        from . import revisions

        workspace.require_item(item_id)
        if workspace.hosted_store:
            from .hosted import ClaimError

            try:
                workspace.hosted_store.require_claim(
                    item_id, request.state.user, workspace.discard_work
                )
            except ClaimError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
        if (
            revisions.saved_records(workspace, item_id)
            and not read_state(workspace.paths(item_id)["metadata"]).re_review
        ):
            raise HTTPException(
                status_code=409, detail="select Re-review before changing variants"
            )
        if (
            workspace.hosted_store
            and workspace.hosted_store.submission(item_id)
            and not revisions.document(workspace, item_id)
        ):
            raise HTTPException(status_code=409, detail="product is pending review")

    @app.get("/api/items/{item_id}/variants")
    def get_variants(item_id: str, request: Request) -> dict:
        workspace.require_item(item_id)
        from . import revisions

        products = workspace.catalog_by_stem.get(item_id, [])
        independent = revisions.independent_record(workspace, item_id)
        revision = revisions.document(workspace, item_id)
        root_state = read_state(workspace.paths(item_id)["metadata"])
        editing = root_state.re_review and (
            not workspace.hosted_store
            or workspace.hosted_store.claims().get(item_id, {}).get("user_id")
            == request.state.user.id
        )
        metadata = (
            revision["records"][0]
            if revision and revision["independent"] and editing
            else independent[0]
            if independent
            else None
        )
        size_sets = [
            {size_key(s) for s in p.get("sz", {}).get("s", []) if size_key(s)}
            for p in products
        ]
        known = (
            {size_key(size, True) for size in metadata.get("sizes", [])}
            if metadata
            else set.intersection(*size_sets)
            if size_sets
            else set()
        )
        published = revisions.saved_records(workspace, item_id)
        pending = (
            workspace.hosted_store.submission(item_id)
            if workspace.hosted_store
            else None
        )
        previews = {}
        if pending and not editing:
            raw = json.loads(workspace.pending_paths(item_id)["metadata"].read_text())
            metadata = raw.get("record") or raw.get("records", [{}])[0]
            if independent and not metadata:
                metadata = independent[0]
            info = {
                "general": (workspace.pending_paths(item_id)["svg"]).exists(),
                "variants": metadata.get("variants", {}),
            }
            for key in ([""] if info["general"] else []) + list(info["variants"]):
                previews[key] = (
                    f"/api/submissions/{quote(item_id, safe='')}/outline.svg?variant={quote(key)}&show_length=true"
                )
        elif published and not editing:
            metadata, directory = published[0]
            info = {
                "general": (directory / "outline.svg").exists()
                or not metadata.get("variants"),
                "variants": metadata.get("variants", {}),
            }
            for key, _, _ in published_entries(metadata, directory):
                previews[key] = (
                    f"/api/community/{quote(item_id, safe='')}/outline.svg?variant={quote(key)}&show_length=true&invert_colors=true"
                    if independent
                    else f"/api/items/{quote(item_id, safe='')}/file/svg?variant={quote(key)}"
                    if directory == workspace.paths(item_id)["directory"]
                    else f"/api/products/{products[0]['id']}/outline.svg?variant={quote(key)}&show_length=true&invert_colors=true"
                )
            if directory == workspace.paths(item_id)["directory"]:
                for key in ([""] if info["general"] else []) + list(info["variants"]):
                    if paths(workspace, item_id, key)["svg"].is_file():
                        previews[key] = (
                            f"/api/items/{quote(item_id, safe='')}/file/svg?variant={quote(key)}"
                        )
        else:
            info = collection(workspace, item_id)
        entries = (
            [
                {
                    "id": "",
                    "label": "General",
                    "sizes": [],
                    "preview_url": previews.get(""),
                }
            ]
            if info["general"]
            else []
        )
        entries.extend(
            {
                "id": key,
                "label": " + ".join(entry["sizes"]),
                "sizes": entry["sizes"],
                "preview_url": previews.get(key),
            }
            for key, entry in info["variants"].items()
        )
        ordered_sizes = (
            [size_key(size, True) for size in metadata.get("sizes", [])]
            if independent
            else list(
                dict.fromkeys(
                    size_key(size)
                    for size in products[0].get("sz", {}).get("s", [])
                    if size_key(size) in known
                )
            )
            if products or independent
            else []
        )
        size_labels = (
            {
                size_key(size, True): size.get("short_label") or size_key(size, True)
                for size in metadata.get("sizes", [])
            }
            if independent
            else {
                size_key(size): size.get("ShortLabel") or size_key(size)
                for size in products[0].get("sz", {}).get("s", [])
                if size_key(size) in known
            }
            if products or independent
            else {}
        )
        return {
            "outlines": entries,
            "sizes": ordered_sizes,
            "size_labels": size_labels,
            "general": info["general"],
        }

    @app.post("/api/items/{item_id}/variants")
    def change_variant(item_id: str, change: VariantChange, request: Request) -> dict:
        guard(item_id, request)
        initialize(workspace, item_id)
        info = collection(workspace, item_id)
        known = set(get_variants(item_id, request)["sizes"])
        key = change.id
        root = workspace.paths(item_id)["directory"]
        try:
            if change.action in {"add", "assign"}:
                if not ID_PATTERN.fullmatch(key) or key == "general":
                    raise ValueError("invalid variant ID")
                if change.action == "add" and key in info["variants"]:
                    raise ValueError("variant already exists")
                if change.action == "assign" and key not in info["variants"]:
                    raise ValueError("unknown variant")
                info["variants"][key] = {"sizes": change.sizes}
                validate_variants(info["variants"], known)
                if change.move_from == "":
                    if not info["general"]:
                        raise ValueError("no general outline to assign")
                    destination = item_paths(root / "variants", key)
                    destination["directory"].mkdir(parents=True, exist_ok=True)
                    copy_outline(workspace.paths(item_id), destination)
                    info["general"] = False
                elif change.action == "add":
                    destination = item_paths(root / "variants", key)
                    atomic_json(
                        destination["metadata"],
                        ReviewState(re_review=True).model_dump(mode="json")
                        | {"waiting_image": True},
                    )
            elif change.action == "general":
                if info["general"]:
                    raise ValueError("general outline already exists")
                info["general"] = True
                if change.move_from is not None:
                    if change.move_from not in info["variants"]:
                        raise ValueError("unknown variant")
                    origin = paths(workspace, item_id, change.move_from)
                    destination = workspace.paths(item_id)
                    copy_outline(origin, destination)
                    del info["variants"][change.move_from]
                    shutil.rmtree(origin["directory"])
                else:
                    workspace.reset_review(
                        workspace.paths(item_id),
                        item_id,
                        "catalog",
                        re_review=bool(workspace.published_item(item_id)),
                        keep_prepared=False,
                    )
                    target = workspace.paths(item_id)
                    # A newly added fallback uses the same blank-image flow as any outline.
                    raw = json.loads(target["metadata"].read_text())
                    atomic_json(target["metadata"], raw | {"waiting_image": True})
                    target["alternative"].unlink(missing_ok=True)
            elif change.action == "remove":
                if len(info["variants"]) + int(info["general"]) <= 1:
                    raise ValueError("keep at least one outline")
                if not key:
                    info["general"] = False
                else:
                    if key not in info["variants"]:
                        raise ValueError("unknown variant")
                    del info["variants"][key]
                    shutil.rmtree(root / "variants" / key, ignore_errors=True)
                if not info["general"] and not info["variants"]:
                    raise ValueError("keep at least one outline")
            else:
                raise ValueError("unknown variant action")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        atomic_json(root / "variants.json", info)
        return get_variants(item_id, request)


def export_outline(target: dict, state: ReviewState) -> None:
    import numpy as np
    from PIL import Image
    from outline import trace_aligned_svg
    from .artifacts import atomic_image, final_mask, validate_length

    if state.rating == "unusable":
        for kind in ("svg", "mask", "cutout"):
            target[kind].unlink(missing_ok=True)
        return
    if state.rating is None:
        raise ValueError("choose a quality rating")
    raw = (
        json.loads(target["metadata"].read_text())
        if target["metadata"].exists()
        else {}
    )
    if raw.get("preserved") and target["svg"].is_file():
        return
    if state.main_length is None:
        raise ValueError("usable outlines require a base-to-tip line")
    from .svg_revision import export_saved_svg

    if export_saved_svg(target, state):
        return
    with Image.open(target["source"]) as image:
        validate_length(state.main_length, *image.size)
        cutout = image.convert("RGBA")
    mask = final_mask(target["rembg"], target["edits"], state.alpha_threshold)
    mask_image = Image.fromarray(mask.astype(np.uint8) * 255)
    atomic_image(target["mask"], mask_image)
    cutout.putalpha(mask_image)
    atomic_image(target["cutout"], cutout)
    temporary = target["directory"] / ".outline.svg.tmp"
    trace_aligned_svg(mask, temporary, state.main_length.start, state.main_length.end)
    temporary.replace(target["svg"])


def finish_collection(workspace, item_id: str) -> tuple[ReviewState, dict]:
    info = collection(workspace, item_id)
    outlines = ([""] if info["general"] else []) + list(info["variants"])
    # Validate the entire product before generating or publishing any entry.
    for key in outlines:
        target = paths(workspace, item_id, key)
        state = read_state(target["metadata"])
        raw = (
            json.loads(target["metadata"].read_text())
            if target["metadata"].exists()
            else {}
        )
        label = " + ".join(info["variants"][key]["sizes"]) if key else "General"
        if not state.rating:
            raise ValueError(f"{label}: choose a quality rating")
        if (
            state.rating != "unusable"
            and not state.main_length
            and not (raw.get("preserved") and target["svg"].is_file())
        ):
            raise ValueError(f"{label}: add a base-to-tip line")
    for key in outlines:
        target = paths(workspace, item_id, key)
        state = read_state(target["metadata"])
        export_outline(target, state)
        raw = json.loads(target["metadata"].read_text())
        state.status = "done"
        atomic_json(target["metadata"], raw | state.model_dump(mode="json"))
    general = (
        read_state(workspace.paths(item_id)["metadata"])
        if info["general"]
        else ReviewState(status="done", rating="unusable")
    )
    return general, document_variants(workspace, item_id)
