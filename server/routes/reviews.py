"""Catalog listing, review editing, saving, statistics, and comparison routes."""

import json
import secrets
import shutil
import zipfile
from io import BytesIO
from urllib.parse import quote

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps

from outline import trace_aligned_svg

from ..artifacts import (
    atomic_bytes,
    atomic_image,
    atomic_json,
    final_mask,
    length_preview,
    read_state,
    svg_main_length,
    validate_length,
)
from ..hosted import ClaimError, User
from ..models import GuestMetadata, IndependentUpdate, PrefetchSelection, ReviewState
from ..workspace import (
    ALLOWED_IMAGE_FORMATS,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    Workspace,
)


def register(app: FastAPI, workspace: Workspace) -> None:
    store = workspace.hosted_store

    def acquire_claim(item_id: str, user: User) -> float:
        workspace.require_item(item_id)
        try:
            _, expires_at = store.acquire_claim(item_id, user, workspace.discard_work)
        except ClaimError as error:
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
        items = [
            workspace.item_summary(
                item_id,
                source,
                request.state.user,
                claims,
                submissions,
            )
            for item_id, source in workspace.queue_items().items()
        ]
        pending_independent = {}
        if store:
            for row in submissions.values():
                if row["kind"] == "independent_update":
                    update = IndependentUpdate.model_validate_json(row["state_json"])
                    pending_independent[update.record_id] = row
        items.extend(
            workspace.independent_item_summary(
                record_id,
                metadata,
                directory,
                pending=record_id in pending_independent,
            )
            for record_id, (
                metadata,
                directory,
            ) in workspace.independent_records().items()
        )
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
        show_length: bool = False,
        invert_colors: bool = False,
    ) -> Response:
        record = workspace.independent_records().get(record_id)
        path = record[1] / "outline.svg" if record else None
        if not path or not path.is_file():
            raise HTTPException(status_code=404, detail="outline does not exist")
        headers = {"Cache-Control": "no-store"}
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
    def stats() -> dict:
        records = workspace.catalog_records()
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
            "summary": summary,
            "vendors": workspace.breakdown(records, "vn"),
            "product_types": workspace.breakdown(records, "pt"),
        }

    @app.get("/api/comparison/products")
    def comparison_products() -> dict:
        products = []
        for record in workspace.catalog_records():
            if not record["comparable"]:
                continue
            product = record["product"]
            main_length = svg_main_length(record["directory"] / "outline.svg")
            products.append(
                {
                    "id": product["id"],
                    "item_id": record["item_id"],
                    "n": product.get("n", ""),
                    "vn": product.get("vn", ""),
                    "pt": product.get("pt", ""),
                    "rating": record["rating"],
                    "main_length": main_length.model_dump(mode="json"),
                    "svg_url": f"/api/products/{quote(str(product['id']), safe='')}/outline.svg",
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
                        }
                        for index, size in enumerate(record["sizes"])
                    ],
                }
            )
        products.sort(key=lambda product: (product["vn"], product["n"], product["id"]))
        return {"products": products}

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
    def prepare_item(item_id: str, request: Request) -> dict:
        claim_expires_at = None
        if store and store.submission(item_id):
            raise HTTPException(status_code=409, detail="item is pending review")
        if (
            workspace.published_item(item_id)
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
        paths, width, height = workspace.prepare(item_id)
        encoded_id = quote(item_id, safe="")
        return {
            "id": item_id,
            "width": width,
            "height": height,
            "state": read_state(paths["metadata"]).model_dump(mode="json"),
            "claim_expires_at": claim_expires_at,
            "source_url": f"/api/items/{encoded_id}/file/source",
            "rembg_url": f"/api/items/{encoded_id}/file/rembg",
            "edits_url": (
                f"/api/items/{encoded_id}/file/edits"
                if paths["edits"].exists()
                else None
            ),
        }

    @app.post("/api/items/{item_id}/rereview")
    def rereview_item(item_id: str, request: Request) -> dict:
        if store and store.submission(item_id):
            raise HTTPException(status_code=409, detail="item is pending review")
        if not workspace.published_item(item_id):
            raise HTTPException(status_code=400, detail="item is not published")
        if store:
            acquire_claim(item_id, request.state.user)
            workspace.select_prefetch(request.state.user.id, [item_id])
            workspace.discard_work(item_id)
        else:
            workspace.set_active(item_id)
        source = workspace.source_for(item_id)
        paths = workspace.paths(item_id)
        with workspace.session_lock:
            workspace.reset_review(
                paths,
                item_id,
                source.name,
                re_review=True,
                keep_prepared=True,
            )
        claims = store.claims() if store else {}
        return workspace.item_summary(
            item_id, workspace.queue_items()[item_id], request.state.user, claims
        )

    @app.get("/api/products/{catalog_id}/outline.svg")
    def published_outline(
        catalog_id: str,
        show_length: bool = False,
        invert_colors: bool = False,
    ) -> Response:
        product = workspace.catalog_by_id.get(catalog_id)
        published = workspace.published_record(product) if product else None
        if not published:
            raise HTTPException(
                status_code=404, detail="published product does not exist"
            )
        path = published[1] / "outline.svg"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="product has no usable outline")
        headers = {"Cache-Control": "no-store"}
        if show_length:
            return Response(
                length_preview(path, invert_colors),
                media_type="image/svg+xml",
                headers=headers,
            )
        return FileResponse(path, media_type="image/svg+xml", headers=headers)

    @app.post("/api/items/{item_id}/alternative")
    async def replace_source(
        item_id: str, request: Request, image: UploadFile = File()
    ) -> dict:
        if store:
            require_claim(item_id, request.state.user)
        else:
            workspace.set_active(item_id)
        workspace.source_for(item_id)
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

        paths = workspace.paths(item_id)
        re_review = (
            bool(workspace.published_item(item_id))
            or read_state(paths["metadata"]).re_review
        )
        with workspace.session_lock:
            atomic_image(paths["alternative"], alternative)
            workspace.reset_review(
                paths, item_id, paths["alternative"].name, re_review=re_review
            )
        _, width, height = workspace.prepare(item_id)
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
    def restore_catalog_source(item_id: str, request: Request) -> dict:
        if store:
            require_claim(item_id, request.state.user)
        else:
            workspace.set_active(item_id)
        paths = workspace.paths(item_id)
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
        _, width, height = workspace.prepare(item_id)
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
    def get_file(item_id: str, kind: str, request: Request) -> FileResponse:
        if store:
            require_claim(item_id, request.state.user)
        workspace.source_for(item_id)
        if kind not in {"source", "rembg", "edits", "mask", "cutout", "svg"}:
            raise HTTPException(status_code=404, detail="unknown artifact")
        path = workspace.paths(item_id)[kind]
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
    ) -> dict | Response:
        if store:
            require_claim(item_id, request.state.user)
        else:
            workspace.set_active(item_id)
        paths, width, height = workspace.prepare(item_id)
        try:
            state = ReviewState.model_validate_json(state_json)
            validate_length(state.main_length, width, height)
        except (ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        if store and state.status != "done":
            raise HTTPException(
                status_code=400, detail="hosted mode does not save drafts"
            )
        if state.status == "done" and state.rating is None:
            raise HTTPException(status_code=400, detail="a rating is required")
        if (
            state.status == "done"
            and state.rating != "unusable"
            and state.main_length is None
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

        if state.status == "done" and state.rating == "unusable":
            for kind in ("mask", "cutout", "svg"):
                paths[kind].unlink(missing_ok=True)
        elif state.status == "done":
            try:
                mask = final_mask(paths["rembg"], paths["edits"], state.alpha_threshold)
                mask_image = Image.fromarray(mask.astype(np.uint8) * 255)
                atomic_image(paths["mask"], mask_image)
                with Image.open(paths["source"]) as image:
                    cutout = image.convert("RGBA")
                cutout.putalpha(mask_image)
                atomic_image(paths["cutout"], cutout)
                temporary_svg = paths["directory"] / ".outline.svg.tmp"
                trace_aligned_svg(
                    mask,
                    temporary_svg,
                    state.main_length.start,
                    state.main_length.end,
                )
                temporary_svg.replace(paths["svg"])
            except (OSError, ValueError) as error:
                raise HTTPException(status_code=400, detail=str(error)) from error

        if state.status == "done":
            state.re_review = False
        source_path = workspace.source_for(item_id)
        source_kind = "alternative" if paths["alternative"].exists() else "catalog"
        if download_only:
            if state.rating == "unusable":
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
                for name, metadata in documents:
                    bundle.writestr(
                        name,
                        json.dumps(metadata, indent=2) + "\n",
                    )
                bundle.writestr("outline.svg", paths["svg"].read_bytes())
            return Response(
                archive.getvalue(),
                media_type="application/zip",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": f'attachment; filename="{item_id}.zip"',
                },
            )
        document = {
            "version": 1,
            "id": item_id,
            "source": source_path.name,
            **state.model_dump(mode="json"),
        }
        if store:
            if store.submission(item_id):
                raise HTTPException(
                    status_code=409, detail="item is already pending review"
                )
            pending = workspace.pending_paths(item_id)
            if pending["directory"].exists():
                shutil.rmtree(pending["directory"])
            atomic_json(
                pending["metadata"],
                {
                    "version": 1,
                    "item_id": item_id,
                    "source": source_kind,
                    "state": state.model_dump(mode="json"),
                    "records": [
                        workspace.record_document(product, state, source_kind)
                        for product in workspace.catalog_by_stem.get(item_id, [])
                    ],
                },
            )
            if state.rating != "unusable":
                atomic_bytes(pending["svg"], paths["svg"].read_bytes())
            if paths["alternative"].exists():
                atomic_bytes(pending["alternative"], paths["alternative"].read_bytes())
            try:
                store.put_submission(
                    item_id,
                    request.state.user,
                    source_kind,
                    state.model_dump_json(),
                )
            except Exception:
                shutil.rmtree(pending["directory"])
                raise
            workspace.select_prefetch(request.state.user.id, [])
            workspace.discard_work(item_id)
        else:
            atomic_json(paths["metadata"], document)
        if state.status == "done" and not store:
            workspace.publish(
                item_id,
                state,
                paths["svg"] if state.rating != "unusable" else None,
            )
        elif not store and not state.re_review:
            workspace.unpublish(item_id)
        return workspace.item_summary(
            item_id,
            workspace.queue_items()[item_id],
            request.state.user,
            store.claims() if store else {},
        )
