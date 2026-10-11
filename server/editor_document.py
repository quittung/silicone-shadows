"""Read-only editor baselines and explicit, whole-product browser commits."""

import copy
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException
from PIL import Image
from pydantic import BaseModel, Field

from . import revisions, variants
from .artifacts import atomic_image, atomic_json, normalize_source, validate_length
from .models import GuestMetadata, ReviewState
from .review_commit import save_review
from .svg_revision import OUTLINE_REVISION_FILES, initialize_outline, outline_canvas
from .workspace import ALLOWED_IMAGE_FORMATS, MAX_IMAGE_PIXELS


class BrowserOutline(BaseModel):
    id: str = Field(max_length=64, pattern=r"^$|^[a-z0-9][a-z0-9-]*$")
    original_id: str | None = Field(default=None, max_length=64)
    sizes: list[str] = Field(default_factory=list, max_length=20)
    state: ReviewState
    mode: str = Field(pattern=r"^(svg|photo|blank)$")
    shape_changed: bool = False
    source: str = Field(default="alternative", pattern=r"^(catalog|alternative)$")


class BrowserDocument(BaseModel):
    baseline: str = Field(min_length=64, max_length=64)
    outlines: list[BrowserOutline] = Field(min_length=1, max_length=21)
    metadata: GuestMetadata | None = None


@dataclass
class Baseline:
    workspace: object
    item_id: str
    records: list
    directory: Path | None
    pending: object
    version: str

    @property
    def metadata(self):
        return self.records[0][0] if self.records else {}

    @property
    def general(self):
        return (
            not self.directory
            or (self.directory / "outline.svg").is_file()
            or not self.metadata.get("variants")
        )

    def svg(self, key):
        if not self.directory:
            return None
        entry = self.metadata.get("variants", {}).get(key) if key else self.metadata
        if key and not entry:
            raise HTTPException(400, "Unknown original outline.")
        if key:
            nested = self.directory / f"variants/{key}/outline.svg"
            if nested.is_file():
                return nested
        path = self.directory / (
            entry.get("file", "outline.svg") if key else "outline.svg"
        )
        return path if path.is_file() else None

    def photo(self, key):
        if not self.directory:
            return None
        if key:
            nested = self.directory / f"variants/{key}/alternative.png"
            if nested.is_file():
                return nested
        path = self.directory / "alternative.png"
        return path if path.is_file() else None

    def sizes(self):
        independent = not self.workspace.catalog_by_stem.get(self.item_id)
        if independent:
            sizes = self.metadata.get("sizes", [])
            return [s["label"] for s in sizes], {
                s["label"]: s.get("short_label", s["label"]) for s in sizes
            }
        products = self.workspace.catalog_by_stem.get(self.item_id, [])
        sets = [
            {variants.size_key(s) for s in p.get("sz", {}).get("s", [])}
            for p in products
        ]
        known = set.intersection(*sets) if sets else set()
        sizes = products[0].get("sz", {}).get("s", []) if products else []
        keys = list(
            dict.fromkeys(
                variants.size_key(s) for s in sizes if variants.size_key(s) in known
            )
        )
        return keys, {
            variants.size_key(s): s.get("ShortLabel") or variants.size_key(s)
            for s in sizes
        }


def baseline(workspace, item_id):
    workspace.require_item(item_id)
    pending = revisions.pending_item(workspace, item_id)
    if pending and pending["item_id"] != item_id:
        raise HTTPException(
            409, "An update is already pending. Edit it from moderation."
        )
    records = revisions.saved_records(workspace, item_id) or []
    directory = records[0][1] if records else None
    if pending:
        directory = workspace.pending_paths(item_id)["directory"]
        raw = json.loads((directory / "metadata.json").read_text())
        metadata = raw.get("record") or (raw.get("records") or [None])[0]
        if metadata is None:
            metadata = workspace.independent_document(
                f"community:{item_id}", GuestMetadata.model_validate(raw["metadata"])
            )
        if pending["kind"] == "independent_update" and not raw.get("outline_revision"):
            # Metadata-only pending updates still use the approved geometry.
            metadata, directory = records[0]
        records = [(metadata, directory)]
    digest = hashlib.sha256(
        json.dumps(workspace.catalog_by_stem.get(item_id, []), sort_keys=True).encode()
    )
    if pending:
        digest.update(
            revisions.fingerprint(
                workspace.pending_paths(item_id)["directory"]
            ).encode()
        )
    for _, path in records:
        digest.update(revisions.fingerprint(path).encode())
    return Baseline(workspace, item_id, records, directory, pending, digest.hexdigest())


def authorize_edit(base, user):
    if base.pending and (not user or not user.reviewer):
        raise HTTPException(
            403, "Reviewer access is required to edit a pending submission."
        )


def asset_url(base, kind, key=""):
    return f"/api/editor/items/{quote(base.item_id, safe='')}/asset/{kind}?variant={quote(key)}&baseline={base.version}"


def describe(base, user, allow_invalid_certificate=False):
    workspace = base.workspace
    sizes, labels = base.sizes()
    keys = ([""] if base.general else []) + list(base.metadata.get("variants", {}))
    outlines = []
    for key in keys:
        entry = base.metadata.get("variants", {}).get(key) if key else base.metadata
        state = ReviewState(rating=entry.get("quality"), re_review=bool(base.records))
        outline = {
            "id": key,
            "original_id": key if base.records else None,
            "sizes": entry.get("sizes", []) if key else [],
            "mode": "blank",
            "state": state.model_dump(mode="json"),
            "source": entry.get("source", base.metadata.get("source", "catalog")),
            "reference_url": asset_url(base, "reference", key)
            if base.photo(key)
            else None,
        }
        svg = base.svg(key)
        if svg:
            dimensions, _, state.main_length, mask_svg = outline_canvas(svg)
            outline.update(
                mode="svg",
                **dimensions,
                mask_svg=mask_svg.decode(),
                state=state.model_dump(mode="json"),
            )
        elif not base.records:
            # Prepared photos are a reusable processing cache, not an editing draft.
            paths, width, height = workspace.prepare(
                base.item_id, allow_invalid_certificate
            )
            outline.update(
                mode="photo",
                width=width,
                height=height,
                source_url=asset_url(base, "source"),
                mask_url=asset_url(base, "mask"),
            )
        outlines.append(outline)
    metadata = (
        GuestMetadata.model_validate(base.metadata).model_dump(mode="json")
        if base.metadata.get("record_id")
        else None
    )
    return {
        "id": base.item_id,
        "baseline": base.version,
        "revision": bool(base.records),
        "pending_review": bool(base.pending),
        "metadata": metadata,
        "outlines": outlines,
        "sizes": sizes,
        "size_labels": labels,
        "general": base.general,
    }


def decode_image(data):
    try:
        with Image.open(BytesIO(data)) as uploaded:
            if (
                uploaded.format not in ALLOWED_IMAGE_FORMATS
                or uploaded.width * uploaded.height > MAX_IMAGE_PIXELS
            ):
                raise ValueError("Unsupported image format or image too large.")
            image = normalize_source(uploaded)
            image.load()
            return image
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise HTTPException(400, str(error)) from error


def prepare_commit(workspace, base, document, files, scratch, user):
    """Materialize browser state only for the duration of Save/export."""
    working = copy.copy(workspace)
    working.work_dir = scratch
    if base.directory == workspace.paths(base.item_id)["directory"]:
        shutil.copytree(
            base.directory, working.paths(base.item_id)["directory"], dirs_exist_ok=True
        )
    # A submission's fingerprint must be checked against the real baseline.
    if base.records:
        revisions.start(working, base.item_id, user)
    root = working.paths(base.item_id)
    manifest = {
        "general": any(o.id == "" for o in document.outlines),
        "variants": {o.id: {"sizes": o.sizes} for o in document.outlines if o.id},
    }
    allowed_files = set()
    for outline in document.outlines:
        if not outline.id and outline.sizes:
            raise HTTPException(400, "The general outline cannot have assigned sizes.")
        if outline.mode == "photo":
            allowed_files.update(
                f"{kind}:{outline.id}" for kind in ("source", "mask", "edits")
            )
        elif outline.mode == "svg" and outline.shape_changed:
            allowed_files.update(f"{kind}:{outline.id}" for kind in ("mask", "edits"))
    if set(files) - allowed_files:
        raise HTTPException(400, "Unexpected image parts in the editor document.")
    ids = [o.id for o in document.outlines]
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "Duplicate outline IDs.")
    known, _ = base.sizes()
    if document.metadata is not None:
        if (
            not base.metadata.get("record_id")
            or document.metadata.catalog_id is not None
        ):
            raise HTTPException(400, "Only independent product metadata is editable.")
        workspace.validate_metadata_options(document.metadata)
        known = [s.label for s in document.metadata.sizes]
        revision = revisions.document(working, base.item_id)
        revision["records"][0].update(
            workspace.independent_document(revision["record_id"], document.metadata)
        )
        atomic_json(root["directory"] / "revision.json", revision)
    variants.validate_variants(manifest["variants"], set(known))
    atomic_json(root["directory"] / "variants.json", manifest)
    for outline in document.outlines:
        target = variants.paths(working, base.item_id, outline.id)
        target["directory"].mkdir(parents=True, exist_ok=True)
        state = outline.state.model_copy(
            update={"status": "pending", "re_review": bool(base.records)}
        )
        raw = {"outline_source": outline.source}
        if outline.mode == "svg":
            if outline.original_id is None:
                raise HTTPException(400, "Saved outlines require an original outline.")
            svg = base.svg(outline.original_id)
            if not svg:
                raise HTTPException(400, "Original outline no longer exists.")
            original_state = ReviewState(rating=state.rating)
            raw.update(
                initialize_outline(
                    target,
                    svg,
                    original_state,
                    False,
                    base.photo(outline.original_id) or scratch / "no-reference",
                )
            )
            width, height = raw["svg_canvas"]["width"], raw["svg_canvas"]["height"]
            if outline.shape_changed:
                mask = decode_image(files.get(f"mask:{outline.id}", b""))
                if mask.size != (width, height):
                    raise HTTPException(
                        400, "Mask dimensions do not match the original outline."
                    )
                mask = mask.convert("RGBA")
                atomic_image(target["rembg"], mask)
                atomic_image(target["directory"] / "revision-rembg.png", mask)
                atomic_image(target["source"], Image.new("RGB", mask.size, "#ecece8"))
        elif outline.mode == "photo":
            source = decode_image(files.get(f"source:{outline.id}", b""))
            mask = decode_image(files.get(f"mask:{outline.id}", b""))
            if source.size != mask.size:
                raise HTTPException(400, "Source and mask dimensions do not match.")
            width, height = mask.size
            atomic_image(target["source"], source)
            atomic_image(target["rembg"], mask.convert("RGBA"))
            if outline.source == "alternative":
                atomic_image(target["alternative"], source)
            # This is a new image basis; do not preserve the previous SVG mapping.
            for filename in OUTLINE_REVISION_FILES:
                (target["directory"] / filename).unlink(missing_ok=True)
            raw.pop("svg_canvas", None)
        else:
            width = height = 0
            if state.rating != "unusable":
                raise HTTPException(
                    400, "Paste or drop an image for each usable outline."
                )
        validate_length(state.main_length, width, height)
        paint_data = files.get(f"edits:{outline.id}")
        if paint_data:
            paint = decode_image(paint_data)
            if paint.size != (width, height):
                raise HTTPException(400, "Paint dimensions do not match the outline.")
            atomic_image(target["edits"], paint.convert("RGBA"))
        atomic_json(target["metadata"], raw | state.model_dump(mode="json"))
    if not manifest["general"]:
        atomic_json(
            root["metadata"],
            ReviewState(re_review=bool(base.records)).model_dump(mode="json"),
        )
    return working


def commit(workspace, item_id, user, document, files, download_only=False):
    with workspace.session_lock:
        base = baseline(workspace, item_id)
        authorize_edit(base, user)
        if base.version != document.baseline:
            raise HTTPException(
                409, "The saved product changed. Cancel and reopen it before saving."
            )
        with tempfile.TemporaryDirectory(prefix="editor-save-") as temporary:
            scratch = Path(temporary)
            try:
                working = prepare_commit(
                    workspace, base, document, files, scratch, user
                )
                state = ReviewState(
                    status="done", rating="unusable", re_review=bool(base.records)
                )
                if any(o.id == "" for o in document.outlines):
                    state = next(
                        o.state for o in document.outlines if o.id == ""
                    ).model_copy(update={"status": "done"})
                result = save_review(
                    working, item_id, user, state.model_dump_json(), download_only
                )
                if download_only:
                    return result
                # Work-only local products are committed here, never during editing.
                if not workspace.hosted_store and not workspace.dataset_dir:
                    shutil.copytree(
                        working.paths(item_id)["directory"],
                        workspace.paths(item_id)["directory"],
                        dirs_exist_ok=True,
                    )
                workspace.dataset_changed()
                return workspace.item_summary(
                    item_id,
                    workspace.queue_items()[item_id],
                    user,
                    workspace.hosted_store.claims() if workspace.hosted_store else {},
                )
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
