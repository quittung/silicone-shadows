"""Browser-owned drafts: open a baseline, process transient images, commit once."""

import asyncio
from functools import partial

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response

from .. import editor_document as documents
from ..hosted import ClaimError
from ..workspace import MAX_IMAGE_BYTES, CatalogImageUnavailable


def register(app, workspace):
    store = workspace.hosted_store
    processing = asyncio.Semaphore(1)

    def claim(item_id, user, acquire=False):
        if not store:
            return None
        try:
            if acquire:
                return store.acquire_claim(item_id, user)[1]
            store.require_claim(item_id, user)
        except ClaimError as error:
            detail = (
                {"message": str(error), "current_item_id": error.current_item_id}
                if error.current_item_id
                else str(error)
            )
            raise HTTPException(409, detail) from error

    @app.post("/api/editor/items/{item_id}/open")
    def open_document(
        item_id: str, request: Request, allow_invalid_certificate: bool = False
    ):
        base = documents.baseline(workspace, item_id)
        documents.authorize_edit(base, request.state.user)
        expires = claim(item_id, request.state.user, acquire=True)
        try:
            result = documents.describe(
                base, request.state.user, allow_invalid_certificate
            )
        except CatalogImageUnavailable as error:
            sizes, labels = base.sizes()
            result = {
                "id": item_id,
                "baseline": base.version,
                "revision": False,
                "outlines": [
                    {
                        "id": "",
                        "original_id": None,
                        "mode": "blank",
                        "state": {},
                        "sizes": [],
                        "source": "catalog",
                    }
                ],
                "sizes": sizes,
                "size_labels": labels,
                "general": True,
                "source_unavailable": True,
                "certificate_error": error.certificate_error,
            }
        return result | {"claim_expires_at": expires}

    @app.post("/api/editor/items/{item_id}/claim")
    def heartbeat_document(item_id: str, request: Request):
        workspace.require_item(item_id)
        if not store:
            return {"expires_at": None}
        try:
            return {"expires_at": store.heartbeat(item_id, request.state.user)}
        except ClaimError as error:
            message = (
                "Your 15-minute claim expired. Resume editing to continue."
                if "expired" in str(error)
                else str(error)
            )
            raise HTTPException(409, message) from error

    @app.post("/api/editor/items/{item_id}/release")
    def release_document(item_id: str, request: Request):
        if store:
            store.release_claims(request.state.user, item_id)
        return Response(status_code=204)

    @app.get("/api/editor/items/{item_id}/asset/{kind}")
    def asset(
        item_id: str, kind: str, request: Request, baseline: str, variant: str = ""
    ):
        base = documents.baseline(workspace, item_id)
        documents.authorize_edit(base, request.state.user)
        if base.version != baseline:
            raise HTTPException(
                409, "The saved product changed. Reopen it before editing."
            )
        if kind == "catalog":
            path = workspace.sources().get(item_id) or workspace.download_source(
                item_id
            )
        elif kind == "reference":
            path = base.photo(variant)
        elif kind in {"source", "mask"} and not base.records and not variant:
            paths = workspace.paths(item_id)
            path = paths["source" if kind == "source" else "rembg"]
        else:
            path = None
        if path is None or not path.is_file():
            raise HTTPException(404, "Editor asset not found.")
        return FileResponse(path, headers={"Cache-Control": "private, no-cache"})

    @app.post("/api/editor/process")
    async def process_image(request: Request):
        async with request.form(max_part_size=MAX_IMAGE_BYTES) as form:
            upload = form.get("image")
            if not hasattr(upload, "read"):
                raise HTTPException(400, "An image is required.")
            data = await upload.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Image is too large.")
        async with processing:
            image = await asyncio.to_thread(documents.decode_image, data)
            # Normalize orientation before inference; retain transparency.
            from io import BytesIO

            normalized = BytesIO()
            await asyncio.to_thread(image.save, normalized, format="PNG")
            result = await asyncio.to_thread(
                workspace.remove_background, normalized.getvalue()
            )
        return Response(
            result, media_type="image/png", headers={"Cache-Control": "no-store"}
        )

    @app.post("/api/editor/items/{item_id}/commit", response_model=None)
    async def commit_document(
        item_id: str, request: Request, download_only: bool = False
    ):
        claim(item_id, request.state.user)
        async with request.form(
            max_files=65, max_fields=2, max_part_size=MAX_IMAGE_BYTES
        ) as form:
            try:
                document = documents.BrowserDocument.model_validate_json(
                    form.get("document", "")
                )
            except ValueError as error:
                raise HTTPException(400, "Invalid editor document.") from error
            files = {}
            total = 0
            for name, upload in form.multi_items():
                if hasattr(upload, "read"):
                    data = await upload.read(MAX_IMAGE_BYTES + 1)
                    total += len(data)
                    if len(data) > MAX_IMAGE_BYTES or total > 128 * 1024 * 1024:
                        raise HTTPException(413, "Submission images are too large.")
                    if name in files:
                        raise HTTPException(400, "Duplicate image parts.")
                    files[name] = data
        async with processing:
            # Recheck the lease after a queued operation, before committing.
            claim(item_id, request.state.user)
            return await asyncio.to_thread(
                partial(
                    documents.commit,
                    workspace,
                    item_id,
                    request.state.user,
                    document,
                    files,
                    download_only,
                )
            )
