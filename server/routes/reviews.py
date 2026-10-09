"""Catalog listing, review editing, saving, statistics, and comparison routes."""

import hashlib
import json
import secrets
import shutil
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps


from ..artifacts import (
    atomic_bytes,
    atomic_image,
    atomic_json,
    length_preview,
    read_state,
    svg_main_length,
    validate_length,
)
from .. import revisions
from .. import variants as variant_store
from ..hosted import ClaimError, User
from ..comparison import ComparisonSnapshot
from ..models import GuestMetadata, IndependentUpdate, PrefetchSelection, ReviewState
from ..workspace import (
    ALLOWED_IMAGE_FORMATS,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    CatalogImageUnavailable,
    Workspace,
)


def register(app: FastAPI, workspace: Workspace) -> None:
    store = workspace.hosted_store

    def acquire_claim(item_id: str, user: User) -> float:
        workspace.require_item(item_id)
        try:
            _, expires_at = store.acquire_claim(item_id, user, workspace.discard_work)
        except ClaimError as error:
            if error.current_item_id:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": str(error),
                        "current_item_id": error.current_item_id,
                    },
                ) from error
            raise HTTPException(status_code=409, detail=str(error)) from error
        return expires_at

    def require_claim(item_id: str, user: User) -> None:
        workspace.require_item(item_id)
        try:
            store.require_claim(item_id, user, workspace.discard_work)
        except ClaimError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/items")
    def list_items(request: Request) -> dict:
        claims = store.claims() if store else {}
        activity = store.item_activity() if store else {}
        submissions = (
            {row["item_id"]: row for row in store.submissions()} if store else {}
        )
        independent_records = workspace.independent_records()
        items = [
            workspace.item_summary(
                item_id,
                source,
                request.state.user,
                claims,
                submissions,
                independent_records,
            )
            for item_id, source in workspace.queue_items(independent_records).items()
        ]
        if store:
            for item in items:
                item["last_opened_at"] = activity.get(item["id"])
        return {
            "items": items,
            "total": len(items),
            "done": sum(item["status"] == "done" for item in items),
        }

    @app.get("/api/community/{record_id}/outline.svg")
    def independent_outline(
        record_id: str,
        v: str | None = None,
        show_length: bool = False,
        invert_colors: bool = False,
        variant: str = "",
    ) -> Response:
        record = workspace.independent_records().get(record_id)
        if variant and (not record or variant not in record[0].get("variants", {})):
            raise HTTPException(status_code=404, detail="unknown variant")
        path = (
            record[1]
            / (record[0]["variants"][variant]["file"] if variant else "outline.svg")
            if record
            else None
        )
        if not path or not path.is_file():
            raise HTTPException(status_code=404, detail="outline does not exist")
        headers = {
            "Cache-Control": (
                "private, max-age=31536000, immutable"
                if v and not show_length
                else "no-store"
            )
        }
        if show_length:
            return Response(
                length_preview(path, invert_colors),
                media_type="image/svg+xml",
                headers=headers,
            )
        return FileResponse(path, media_type="image/svg+xml", headers=headers)

    @app.post("/api/community/{record_id}/metadata")
    def submit_independent_metadata(
        record_id: str, metadata: GuestMetadata, request: Request
    ) -> dict:
        if not store:
            raise HTTPException(status_code=404)
        if metadata.catalog_id is not None:
            raise HTTPException(
                status_code=400, detail="independent records cannot have a catalog ID"
            )
        workspace.validate_metadata_options(metadata)
        record = workspace.independent_records().get(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="record does not exist")
        for row in store.submissions():
            if row["kind"] != "independent_update":
                continue
            update = IndependentUpdate.model_validate_json(row["state_json"])
            if update.record_id == record_id:
                raise HTTPException(
                    status_code=409, detail="metadata update is already pending"
                )
        item_id = f"independent-update-{secrets.token_urlsafe(12)}"
        update = IndependentUpdate(record_id=record_id, metadata=metadata)
        pending = workspace.pending_paths(item_id)
        try:
            atomic_json(
                pending["metadata"],
                {"kind": "independent_update", **update.model_dump(mode="json")},
            )
            atomic_bytes(pending["svg"], (record[1] / "outline.svg").read_bytes())
            store.put_submission(
                item_id,
                request.state.user,
                "alternative",
                update.model_dump_json(),
                kind="independent_update",
            )
        except Exception:
            if pending["directory"].is_dir():
                shutil.rmtree(pending["directory"])
            raise
        return {"item_id": item_id, "status": "pending_review"}

    @app.post("/api/prefetch")
    def select_prefetch(selection: PrefetchSelection, request: Request) -> dict:
        owner_id = request.state.user.id if store else 0
        try:
            selected = workspace.select_prefetch(owner_id, selection.item_ids)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"selected": selected}

    @app.get("/api/stats")
    def stats(measured_only: bool = False) -> dict:
        records = workspace.catalog_records()
        if measured_only:
            records = [record for record in records if record["has_measurements"]]
        reviewed = sum(record["reviewed"] for record in records)
        pending_items = (
            {row["item_id"] for row in store.submissions()} if store else set()
        )
        pending_review = sum(record["item_id"] in pending_items for record in records)
        in_catalog = sum(
            record["reviewed"] and record["item_id"] not in pending_items
            for record in records
        )
        summary = {
            "products": len(records),
            "reviewed": reviewed,
            "pending": len(records) - reviewed,
            "in_catalog": in_catalog,
            "pending_review": pending_review,
            "never_worked": len(records) - in_catalog - pending_review,
            "good": sum(
                record["rating"] == "good" and record["item_id"] not in pending_items
                for record in records
            ),
            "bad_perspective": sum(
                record["rating"] == "bad_perspective"
                and record["item_id"] not in pending_items
                for record in records
            ),
            "unusable": sum(
                record["rating"] == "unusable"
                and record["item_id"] not in pending_items
                for record in records
            ),
            "comparable": sum(
                record["comparable"] and record["item_id"] not in pending_items
                for record in records
            ),
        }
        return {
            "catalog_version": workspace.catalog_version,
            "summary": summary,
            "vendors": workspace.breakdown(records, "vn"),
            "product_types": workspace.breakdown(records, "pt"),
        }

    def build_comparison_snapshot() -> tuple[bytes, str, dict[str, tuple[Path, str]]]:
        def largest_circumference(*values: object) -> float | int | None:
            measurements = [
                value
                for value in values
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
            return max(measurements, default=None)

        products = []
        outlines = {}

        def outline_url(kind: str, item_id: object, path: Path) -> str:
            outline_id = hashlib.sha256(f"{kind}:{item_id}".encode()).hexdigest()
            version = str(path.stat().st_mtime_ns)
            outlines[outline_id] = (path, version)
            return f"/api/comparison/outlines/{outline_id}.svg?v={version}"

        for record in workspace.catalog_records():
            if not record["comparable"]:
                continue
            product = record["product"]
            outline = record["directory"] / "outline.svg"
            metadata = workspace.published_record(product)[0]
            available = variant_store.published_entries(metadata, record["directory"])
            representative = available[0][2]
            main_length = svg_main_length(representative)
            vendor_url = product.get("link")
            if not isinstance(vendor_url, str) or urlparse(vendor_url).scheme not in {
                "http",
                "https",
            }:
                vendor_url = None
            product_type = quote(str(product.get("pt") or "other").lower(), safe="")
            vendor_term = quote(
                str(product.get("vn") or "").replace(" ", "_"), safe="_"
            )
            search_term = quote(str(product.get("n") or "").replace(" ", "_"), safe="_")
            products.append(
                {
                    "id": product["id"],
                    "item_id": record["item_id"],
                    "n": product.get("n", ""),
                    "vn": product.get("vn", ""),
                    "pt": product.get("pt", ""),
                    "rating": record["rating"],
                    "vendor_url": vendor_url,
                    "toybox_url": (
                        f"https://fantasytoybox.net/products/{product_type}/"
                        f"FilterOptions=Vendor[{vendor_term}]&SearchTerm[{search_term}]!"
                    ),
                    "main_length": main_length.model_dump(mode="json"),
                    "svg_url": outline_url("catalog", product["id"], representative),
                    "sizes": [
                        {
                            "index": index,
                            "label": size.get("ShortLabel")
                            or size.get("sl")
                            or str(index + 1),
                            "name": size.get("sl")
                            or size.get("ShortLabel")
                            or str(index + 1),
                            "length_in": size["len"],
                            "circumference_in": largest_circumference(
                                size.get("circ"), size.get("wcirc")
                            ),
                            "price_usd": size.get("p"),
                        }
                        for index, size in enumerate(record["sizes"])
                    ],
                }
            )
        inches_per_unit = {"in": 1, "cm": 1 / 2.54, "mm": 1 / 25.4}
        for record_id, (metadata, directory) in workspace.independent_records().items():
            outline = directory / "outline.svg"
            available = variant_store.published_entries(metadata, directory)
            if not available:
                continue
            outline = available[0][2]
            sizes = []
            for index, size in enumerate(metadata.get("sizes", [])):
                conversion = inches_per_unit.get(size.get("unit"))
                length = size.get("length")
                if (
                    not conversion
                    or not isinstance(length, (int, float))
                    or length <= 0
                ):
                    continue
                circumference = largest_circumference(
                    size.get("circumference"), size.get("widest_circumference")
                )
                sizes.append(
                    {
                        "index": index,
                        "label": size.get("short_label")
                        or size.get("label")
                        or str(index + 1),
                        "name": size.get("label")
                        or size.get("short_label")
                        or str(index + 1),
                        "length_in": length * conversion,
                        "circumference_in": (
                            circumference * conversion
                            if isinstance(circumference, (int, float))
                            else None
                        ),
                        "price_usd": size.get("price"),
                    }
                )
            if not sizes:
                continue
            vendor_url = metadata.get("product_url")
            if not isinstance(vendor_url, str) or urlparse(vendor_url).scheme not in {
                "http",
                "https",
            }:
                vendor_url = None
            products.append(
                {
                    "id": record_id,
                    "item_id": record_id,
                    "n": metadata.get("name", ""),
                    "vn": metadata.get("vendor", ""),
                    "pt": metadata.get("product_type", ""),
                    "rating": metadata.get("quality"),
                    "vendor_url": vendor_url,
                    "toybox_url": None,
                    "main_length": svg_main_length(outline).model_dump(mode="json"),
                    "svg_url": outline_url("community", record_id, outline),
                    "sizes": sizes,
                }
            )
        # Resolve each size once. Unassigned sizes require a usable general fallback.
        for product in products:
            if isinstance(product["id"], int):
                catalog_product = workspace.catalog_by_id[str(product["id"])]
                metadata, directory = workspace.published_record(catalog_product)
                source_sizes = catalog_product.get("sz", {}).get("s", [])
                # Existing indexes refer to the measured subset for catalog records.
                measured = [
                    size
                    for size in source_sizes
                    if isinstance(size.get("len"), (int, float)) and size["len"] > 0
                ]
                keys = [variant_store.size_key(size) for size in measured]
            else:
                metadata, directory = workspace.independent_records()[product["id"]]
                keys = [
                    variant_store.size_key(size, True)
                    for size in metadata.get("sizes", [])
                ]
            # Full labels identify sizes; entries remain stable when the catalog is reordered.
            variant_store.validate_variants(metadata.get("variants", {}))
            assignments = {
                size: (key, entry)
                for key, entry in metadata.get("variants", {}).items()
                for size in entry["sizes"]
            }
            resolved = []
            for size in product["sizes"]:
                key = keys[size["index"]]
                assignment = assignments.get(key)
                if assignment:
                    variant_id, entry = assignment
                    path = directory / entry["file"]
                else:
                    variant_id, entry, path = "", metadata, directory / "outline.svg"
                if entry.get("quality") == "unusable" or not path.is_file():
                    continue
                size.update(
                    svg_url=outline_url("size", f"{product['id']}:{variant_id}", path),
                    main_length=svg_main_length(path).model_dump(mode="json"),
                    rating=entry["quality"],
                )
                resolved.append(size)
            product["sizes"] = resolved
        products = [product for product in products if product["sizes"]]
        products.sort(
            key=lambda product: (product["vn"], product["n"], str(product["id"]))
        )
        body = json.dumps(
            {"products": products}, ensure_ascii=False, separators=(",", ":")
        ).encode()
        return body, f'"{hashlib.sha256(body).hexdigest()}"', outlines

    comparison_snapshot = ComparisonSnapshot(build_comparison_snapshot)
    app.state.comparison = comparison_snapshot
    workspace.on_dataset_change = comparison_snapshot.schedule

    @app.get("/api/comparison/products")
    def comparison_products(request: Request) -> Response:
        comparison_body, comparison_etag, _ = comparison_snapshot.get()
        headers = {
            "Cache-Control": "public, no-cache",
            "ETag": comparison_etag,
        }
        if request.headers.get("if-none-match") == comparison_etag:
            return Response(status_code=304, headers=headers)
        return Response(
            comparison_body,
            media_type="application/json",
            headers=headers,
        )

    @app.get("/api/comparison/outlines/{outline_id}.svg")
    def comparison_outline(outline_id: str, v: str | None = None) -> Response:
        _, _, comparison_outlines = comparison_snapshot.get()
        outline = comparison_outlines.get(outline_id)
        if not outline or v != outline[1] or not outline[0].is_file():
            raise HTTPException(status_code=404, detail="outline does not exist")
        return FileResponse(
            outline[0],
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.post("/api/comparison/reload")
    def reload_comparison(request: Request) -> Response:
        user = request.state.user
        if store and (not user or not user.reviewer):
            raise HTTPException(status_code=403, detail="reviewer access required")
        comparison_snapshot.refresh()
        return Response(status_code=204)

    @app.post("/api/items/{item_id}/claim")
    def heartbeat_claim(item_id: str, request: Request) -> dict:
        if not store:
            return {"expires_at": None}
        workspace.require_item(item_id)
        try:
            expires_at = store.heartbeat(
                item_id, request.state.user, workspace.discard_work
            )
        except ClaimError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"expires_at": expires_at}

    @app.post("/api/items/{item_id}/release")
    def release_claim(item_id: str, request: Request) -> Response:
        if store:
            workspace.require_item(item_id)
            workspace.select_prefetch(request.state.user.id, [])
            store.release_claims(request.state.user, item_id, workspace.discard_work)
        return Response(status_code=204)

    @app.post("/api/items/{item_id}/prepare")
    def prepare_item(
        item_id: str,
        request: Request,
        allow_invalid_certificate: bool = False,
        variant: str = "",
    ) -> dict:
        claim_expires_at = None
        if (
            store
            and store.submission(item_id)
            and not revisions.document(workspace, item_id)
        ):
            raise HTTPException(status_code=409, detail="item is pending review")
        if (
            revisions.saved_records(workspace, item_id)
            and not read_state(workspace.paths(item_id)["metadata"]).re_review
        ):
            raise HTTPException(
                status_code=409,
                detail="published items are read-only until re-review is selected",
            )
        if store:
            claim_expires_at = acquire_claim(item_id, request.state.user)
            workspace.select_prefetch(request.state.user.id, [item_id])
        else:
            workspace.set_active(item_id)
        selected_paths = variant_store.paths(workspace, item_id, variant)
        if selected_paths["metadata"].is_file():
            raw = json.loads(selected_paths["metadata"].read_text())
            if raw.get("waiting_image") and not selected_paths["source"].exists():
                return {
                    "id": item_id,
                    "blank": True,
                    "state": read_state(selected_paths["metadata"]).model_dump(
                        mode="json"
                    ),
                    "claim_expires_at": claim_expires_at,
                }
            if raw.get("svg_canvas") and not selected_paths["rembg"].is_file():
                return {
                    "id": item_id,
                    "state": read_state(selected_paths["metadata"]).model_dump(
                        mode="json"
                    ),
                    "outline_mask": True,
                    **raw["svg_canvas"],
                    "mask_url": f"/api/items/{quote(item_id, safe='')}/file/revision-mask?variant={quote(variant)}",
                    "claim_expires_at": claim_expires_at,
                }
        try:
            paths, width, height = workspace.prepare(
                item_id, allow_invalid_certificate, variant
            )
        except CatalogImageUnavailable as error:
            return {
                "id": item_id,
                "source_unavailable": True,
                "certificate_error": error.certificate_error,
                "claim_expires_at": claim_expires_at,
            }
        encoded_id = quote(item_id, safe="")
        return {
            "id": item_id,
            "width": width,
            "height": height,
            "reference_url": f"/api/items/{encoded_id}/file/revision-photo?variant={quote(variant)}"
            if (paths["directory"] / "revision-photo.png").is_file()
            else None,
            "saved_outline": bool(
                json.loads(paths["metadata"].read_text()).get("svg_canvas")
            )
            if paths["metadata"].exists()
            else False,
            "has_alternative": paths["alternative"].exists(),
            "state": read_state(paths["metadata"]).model_dump(mode="json"),
            "claim_expires_at": claim_expires_at,
            "source_url": f"/api/items/{encoded_id}/file/source?variant={quote(variant)}",
            "rembg_url": f"/api/items/{encoded_id}/file/rembg?variant={quote(variant)}",
            "edits_url": (
                f"/api/items/{encoded_id}/file/edits?variant={quote(variant)}"
                if paths["edits"].exists()
                else None
            ),
        }

    @app.post("/api/items/{item_id}/remask-crop")
    def remask_crop(
        item_id: str,
        request: Request,
        left: int = Form(...),
        top: int = Form(...),
        right: int = Form(...),
        bottom: int = Form(...),
        variant: str = "",
    ) -> Response:
        if store:
            require_claim(item_id, request.state.user)
        else:
            workspace.set_active(item_id)
        paths, width, height = workspace.prepare(item_id, variant=variant)
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise HTTPException(status_code=400, detail="crop is outside the image")
        with Image.open(paths["source"]) as image:
            crop = image.convert("RGB").crop((left, top, right, bottom))
        data = BytesIO()
        crop.save(data, format="PNG")
        return Response(
            workspace.remove_background(data.getvalue()),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/items/{item_id}/rereview")
    def rereview_item(item_id: str, request: Request) -> dict:
        workspace.require_item(item_id)
        pending = revisions.pending_item(workspace, item_id)
        if pending and not request.state.user.reviewer:
            raise HTTPException(status_code=403, detail="reviewer access required")
        if not pending and not revisions.saved_records(workspace, item_id):
            raise HTTPException(status_code=400, detail="item has no saved result")
        if store:
            acquire_claim(item_id, request.state.user)
            workspace.select_prefetch(request.state.user.id, [item_id])
        else:
            workspace.set_active(item_id)
        with workspace.session_lock:
            revisions.start(workspace, item_id, request.state.user)
        return workspace.item_summary(
            item_id,
            workspace.queue_items()[item_id],
            request.state.user,
            store.claims() if store else {},
        )

    @app.post("/api/items/{item_id}/revision-metadata")
    def revision_metadata(
        item_id: str, metadata: GuestMetadata, request: Request
    ) -> dict:
        if store:
            require_claim(item_id, request.state.user)
        revision = revisions.require_current(workspace, item_id)
        if (
            not revision
            or not revision["independent"]
            or metadata.catalog_id is not None
        ):
            raise HTTPException(status_code=400, detail="independent revision required")
        try:
            workspace.validate_metadata_options(metadata)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        known = {size.label for size in metadata.sizes}
        entries = variant_store.collection(workspace, item_id)["variants"]
        try:
            variant_store.validate_variants(entries, known)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        revision["records"][0].update(
            workspace.independent_document(revision["record_id"], metadata)
        )
        if variant_store.collection(workspace, item_id)["general"]:
            root_path = workspace.paths(item_id)["metadata"]
            root_raw = json.loads(root_path.read_text())
            atomic_json(root_path, root_raw | {"rating": metadata.quality})
        atomic_json(workspace.paths(item_id)["directory"] / "revision.json", revision)
        return {"metadata": revision["records"][0]}

    @app.post("/api/items/{item_id}/cancel-revision")
    def cancel_revision(item_id: str, request: Request) -> Response:
        workspace.require_item(item_id)
        if store:
            require_claim(item_id, request.state.user)
        with workspace.session_lock:
            revisions.cancel(workspace, item_id)
        if store:
            workspace.select_prefetch(request.state.user.id, [])
            store.release_claims(request.state.user, item_id)
        return Response(status_code=204)

    @app.post("/api/items/{item_id}/revision-mask")
    async def restore_revision_mask(
        item_id: str, request: Request, mask: UploadFile = File(), variant: str = ""
    ) -> Response:
        if store:
            require_claim(item_id, request.state.user)
        paths = variant_store.paths(workspace, item_id, variant)
        raw = json.loads(paths["metadata"].read_text())
        dimensions = raw.get("svg_canvas")
        if not dimensions or paths["rembg"].exists():
            raise HTTPException(
                status_code=409, detail="outline mask is already initialized"
            )
        try:
            data = await mask.read(MAX_IMAGE_BYTES + 1)
        finally:
            await mask.close()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="mask is too large")
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format != "PNG" or image.size != (
                    dimensions["width"],
                    dimensions["height"],
                ):
                    raise ValueError("mask dimensions do not match the saved outline")
                rgba = image.convert("RGBA")
                rgba.load()
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        source = Image.new("RGB", rgba.size, "#ecece8")
        source.paste(Image.new("RGB", rgba.size, "#343b45"), mask=rgba.getchannel("A"))
        atomic_image(paths["source"], source)
        atomic_image(paths["rembg"], rgba)
        atomic_image(paths["directory"] / "revision-rembg.png", rgba)
        return Response(status_code=204)

    @app.get("/api/products/{catalog_id}/outline.svg")
    def published_outline(
        catalog_id: str,
        v: str | None = None,
        show_length: bool = False,
        invert_colors: bool = False,
        variant: str = "",
    ) -> Response:
        product = workspace.catalog_by_id.get(catalog_id)
        published = workspace.published_record(product) if product else None
        if not published:
            raise HTTPException(
                status_code=404, detail="published product does not exist"
            )
        entry = published[0].get("variants", {}).get(variant) if variant else None
        if variant and not entry:
            raise HTTPException(status_code=404, detail="unknown variant")
        path = published[1] / entry["file"] if entry else published[1] / "outline.svg"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="product has no usable outline")
        headers = {
            "Cache-Control": (
                "private, max-age=31536000, immutable"
                if v and not show_length
                else "no-store"
            )
        }
        if show_length:
            return Response(
                length_preview(path, invert_colors),
                media_type="image/svg+xml",
                headers=headers,
            )
        return FileResponse(path, media_type="image/svg+xml", headers=headers)

    @app.post("/api/items/{item_id}/alternative")
    async def replace_source(
        item_id: str, request: Request, image: UploadFile = File(), variant: str = ""
    ) -> dict:
        if store:
            require_claim(item_id, request.state.user)
        else:
            workspace.set_active(item_id)
        workspace.require_item(item_id)
        try:
            data = await image.read(MAX_IMAGE_BYTES + 1)
        finally:
            await image.close()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413, detail="alternative image is too large"
            )
        try:
            with Image.open(BytesIO(data)) as uploaded:
                if uploaded.format not in ALLOWED_IMAGE_FORMATS:
                    raise HTTPException(
                        status_code=400, detail="unsupported alternative image format"
                    )
                if uploaded.width * uploaded.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(
                        status_code=413, detail="alternative image has too many pixels"
                    )
                alternative = ImageOps.exif_transpose(uploaded).convert("RGB")
                alternative.load()
        except OSError as error:
            raise HTTPException(
                status_code=400, detail="invalid alternative image"
            ) from error

        variant_store.initialize(workspace, item_id)
        paths = variant_store.paths(workspace, item_id, variant)
        re_review = (
            bool(workspace.published_item(item_id))
            or read_state(paths["metadata"]).re_review
        )
        with workspace.session_lock:
            atomic_image(paths["alternative"], alternative)
            workspace.reset_review(
                paths, item_id, paths["alternative"].name, re_review=re_review
            )
        _, width, height = workspace.prepare(item_id, variant=variant)
        return {
            "item": workspace.item_summary(
                item_id,
                workspace.queue_items()[item_id],
                request.state.user,
                store.claims() if store else {},
            ),
            "width": width,
            "height": height,
        }

    @app.delete("/api/items/{item_id}/alternative")
    def restore_catalog_source(
        item_id: str, request: Request, variant: str = ""
    ) -> dict:
        if store:
            require_claim(item_id, request.state.user)
        else:
            workspace.set_active(item_id)
        variant_store.initialize(workspace, item_id)
        paths = variant_store.paths(workspace, item_id, variant)
        if not paths["alternative"].exists():
            raise HTTPException(
                status_code=400, detail="no alternative image is active"
            )
        catalog_source = workspace.sources().get(item_id)
        if catalog_source is None:
            catalog_source = workspace.download_source(item_id)
        re_review = (
            bool(workspace.published_item(item_id))
            or read_state(paths["metadata"]).re_review
        )
        with workspace.session_lock:
            paths["alternative"].unlink()
            workspace.reset_review(
                paths, item_id, catalog_source.name, re_review=re_review
            )
        _, width, height = workspace.prepare(item_id, variant=variant)
        return {
            "item": workspace.item_summary(
                item_id,
                workspace.queue_items()[item_id],
                request.state.user,
                store.claims() if store else {},
            ),
            "width": width,
            "height": height,
        }

    @app.get("/api/items/{item_id}/file/{kind}")
    def get_file(
        item_id: str, kind: str, request: Request, variant: str = ""
    ) -> FileResponse:
        if store:
            require_claim(item_id, request.state.user)
        if kind not in {
            "source",
            "rembg",
            "edits",
            "mask",
            "cutout",
            "svg",
            "revision-mask",
            "revision-photo",
        }:
            raise HTTPException(status_code=404, detail="unknown artifact")
        paths = variant_store.paths(workspace, item_id, variant)
        path = (
            paths["directory"]
            / ("revision-mask.svg" if kind == "revision-mask" else "revision-photo.png")
            if kind.startswith("revision-")
            else paths[kind]
        )
        if not path.is_file():
            raise HTTPException(status_code=404, detail="artifact does not exist")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.post("/api/items/{item_id}/save", response_model=None)
    async def save_item(
        item_id: str,
        request: Request,
        state_json: str = Form(),
        download_only: bool = Form(False),
        edits: UploadFile | None = File(default=None),
        variant: str = "",
    ) -> dict | Response:
        if store:
            require_claim(item_id, request.state.user)
        else:
            workspace.set_active(item_id)
        revision = revisions.require_current(workspace, item_id)
        paths = variant_store.paths(workspace, item_id, variant)
        raw = (
            json.loads(paths["metadata"].read_text())
            if paths["metadata"].exists()
            else {}
        )
        preserved = (
            (raw.get("preserved") or raw.get("svg_canvas"))
            and paths["svg"].is_file()
            and not paths["source"].is_file()
        )
        if preserved:
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
            try:
                data = await edits.read(MAX_IMAGE_BYTES + 1)
            finally:
                await edits.close()
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="paint layer is too large")
            try:
                with Image.open(BytesIO(data)) as image:
                    paint = image.convert("RGBA")
                    paint.load()
            except OSError as error:
                raise HTTPException(
                    status_code=400, detail="invalid paint PNG"
                ) from error
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
            revisions.save_independent(
                workspace, item_id, state, entries or {}, request.state.user
            )
            if not store:
                reload_comparison(request)
            return workspace.item_summary(
                item_id,
                workspace.queue_items().get(item_id, Path(item_id)),
                request.state.user,
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
                        variant_store.paths(workspace, item_id, key)[
                            "svg"
                        ].read_bytes(),
                    )
                    variant_paths = variant_store.paths(workspace, item_id, key)
                    alternative = variant_paths["alternative"]
                    if not alternative.exists():
                        alternative = variant_paths["directory"] / "revision-photo.png"
                    if alternative.exists():
                        atomic_bytes(
                            target / "alternative.png", alternative.read_bytes()
                        )
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
                    request.state.user,
                    source_kind,
                    state.model_dump_json(),
                    replace=bool(revision and revision["pending"]),
                )
            except Exception:
                shutil.rmtree(pending["directory"], ignore_errors=True)
                raise
            workspace.select_prefetch(request.state.user.id, [])
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
            reload_comparison(request)
        elif not store and not state.re_review and not variant:
            workspace.unpublish(item_id)
        return workspace.item_summary(
            item_id,
            workspace.queue_items()[item_id],
            request.state.user,
            store.claims() if store else {},
        )
