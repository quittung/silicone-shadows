"""Transient guest processing and independent-product submission routes."""

import asyncio
import json
import secrets
import shutil
import zipfile
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageOps
from pydantic import ValidationError

from outline import largest_component, trace_aligned_svg, trace_svg

from ..artifacts import atomic_bytes, atomic_image, atomic_json, validate_length
from ..hosted import QueueError
from ..models import GuestMetadata, IndependentSubmission, MainLength, PublicTicket
from ..workspace import Workspace

MAX_PUBLIC_IMAGE_BYTES = 16 * 1024 * 1024
MAX_PUBLIC_IMAGE_PIXELS = 25_000_000
ALLOWED_IMAGE_FORMATS = {"BMP", "JPEG", "PNG", "TIFF", "WEBP"}


def _address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _decode_mask(data: bytes) -> tuple[np.ndarray, int, int]:
    try:
        with Image.open(BytesIO(data)) as uploaded:
            if uploaded.format != "PNG":
                raise HTTPException(status_code=400, detail="mask must be a PNG")
            width, height = uploaded.size
            if width * height > MAX_PUBLIC_IMAGE_PIXELS:
                raise HTTPException(status_code=413, detail="mask has too many pixels")
            alpha = np.asarray(uploaded.convert("RGBA").getchannel("A"), dtype=np.uint8)
    except OSError as error:
        raise HTTPException(status_code=400, detail="invalid mask") from error
    return largest_component(alpha >= 128), width, height


async def _read_mask(mask: UploadFile) -> tuple[np.ndarray, int, int]:
    try:
        data = await mask.read(MAX_PUBLIC_IMAGE_BYTES + 1)
    finally:
        await mask.close()
    if len(data) > MAX_PUBLIC_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="mask is too large")
    return await asyncio.to_thread(_decode_mask, data)


def _decode_source(data: bytes) -> Image.Image:
    try:
        with Image.open(BytesIO(data)) as uploaded:
            if uploaded.format not in ALLOWED_IMAGE_FORMATS:
                raise HTTPException(status_code=400, detail="unsupported image format")
            if uploaded.width * uploaded.height > MAX_PUBLIC_IMAGE_PIXELS:
                raise HTTPException(
                    status_code=413, detail="source image has too many pixels"
                )
            image = ImageOps.exif_transpose(uploaded).convert("RGB")
            image.load()
            return image
    except OSError as error:
        raise HTTPException(status_code=400, detail="invalid source image") from error


async def _read_source(source: UploadFile) -> Image.Image:
    try:
        data = await source.read(MAX_PUBLIC_IMAGE_BYTES + 1)
    finally:
        await source.close()
    if len(data) > MAX_PUBLIC_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="source image is too large")
    return await asyncio.to_thread(_decode_source, data)


def _submission(
    workspace: Workspace,
    metadata_json: str,
    main_length_json: str | None,
    width: int,
    height: int,
) -> IndependentSubmission:
    try:
        metadata = GuestMetadata.model_validate_json(metadata_json)
        workspace.validate_metadata_options(metadata)
        line = (
            MainLength.model_validate_json(main_length_json)
            if main_length_json
            else None
        )
        validate_length(line, width, height)
        if line is None and any(size.length is not None for size in metadata.sizes):
            raise ValueError("size length requires a marked usable-length line")
        return IndependentSubmission(metadata=metadata, main_length=line)
    except (ValidationError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _svg_bytes(foreground: np.ndarray, line: MainLength | None) -> bytes:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "outline.svg"
        if line:
            trace_aligned_svg(foreground, path, line.start, line.end)
        else:
            trace_svg(foreground, path)
        return path.read_bytes()


def _locked_svg_bytes(
    workspace: Workspace, foreground: np.ndarray, line: MainLength | None
) -> bytes:
    with workspace.session_lock:
        return _svg_bytes(foreground, line)


def _archive(metadata: GuestMetadata, svg: bytes) -> bytes:
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            "metadata.json",
            json.dumps(metadata.model_dump(mode="json", exclude_none=True), indent=2)
            + "\n",
        )
        bundle.writestr("outline.svg", svg)
    return archive.getvalue()


def register(app: FastAPI, workspace: Workspace) -> None:
    queue = workspace.public_queue
    store = workspace.hosted_store

    @app.get("/api/public/metadata-options")
    def metadata_options() -> dict:
        return workspace.metadata_options

    @app.post("/api/public/queue")
    def join_public_queue(request: Request) -> dict:
        if not queue:
            raise HTTPException(status_code=404)
        try:
            return queue.create(_address(request))
        except QueueError as error:
            raise HTTPException(error.status_code, str(error)) from error

    @app.post("/api/public/queue/status")
    def public_queue_status(request: Request, body: PublicTicket) -> dict:
        if not queue:
            raise HTTPException(status_code=404)
        try:
            return queue.status(body.ticket, _address(request))
        except QueueError as error:
            raise HTTPException(error.status_code, str(error)) from error

    @app.post("/api/public/rembg")
    async def public_rembg(
        request: Request,
        ticket: str = Form(min_length=16, max_length=128),
        image: UploadFile = File(),
    ) -> Response:
        if not queue:
            raise HTTPException(status_code=404)
        try:
            queue.begin(ticket, _address(request))
        except QueueError as error:
            await image.close()
            raise HTTPException(error.status_code, str(error)) from error
        try:
            succeeded = False
            try:
                data = await image.read(MAX_PUBLIC_IMAGE_BYTES + 1)
            finally:
                await image.close()
            if len(data) > MAX_PUBLIC_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="image is too large")
            try:
                with Image.open(BytesIO(data)) as uploaded:
                    image_format = uploaded.format
                    width, height = uploaded.size
                    uploaded.verify()
            except OSError as error:
                raise HTTPException(status_code=400, detail="invalid image") from error
            if image_format not in ALLOWED_IMAGE_FORMATS:
                raise HTTPException(status_code=400, detail="unsupported image format")
            if width * height > MAX_PUBLIC_IMAGE_PIXELS:
                raise HTTPException(status_code=413, detail="image has too many pixels")
            result = await asyncio.to_thread(workspace.remove_background, data)
            succeeded = True
        finally:
            archive_token = queue.finish(
                ticket, _address(request) if succeeded else None
            )
        return Response(
            result,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Content-Disposition": 'attachment; filename="background-removed.png"',
                "X-Archive-Token": archive_token,
            },
        )

    @app.post("/api/public/archive")
    async def public_archive(
        request: Request,
        proof: str = Form(min_length=16, max_length=128),
        metadata_json: str = Form(max_length=10_000),
        main_length_json: str | None = Form(default=None, max_length=1_000),
        mask: UploadFile = File(),
    ) -> Response:
        if not queue:
            raise HTTPException(status_code=404)
        try:
            queue.consume_archive(proof, _address(request))
        except QueueError as error:
            await mask.close()
            raise HTTPException(error.status_code, str(error)) from error
        foreground, width, height = await _read_mask(mask)
        submission = _submission(
            workspace, metadata_json, main_length_json, width, height
        )
        if submission.metadata.catalog_id is not None:
            raise HTTPException(
                status_code=400,
                detail="independent submissions cannot have a Toybox catalog ID",
            )
        try:
            svg = await asyncio.to_thread(
                _locked_svg_bytes, workspace, foreground, submission.main_length
            )
            archive = _archive(submission.metadata, svg)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return Response(
            archive,
            media_type="application/zip",
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Content-Disposition": 'attachment; filename="silicone-shadows-submission.zip"',
            },
        )

    @app.post("/api/public/submit")
    async def submit_independent(
        request: Request,
        metadata_json: str = Form(max_length=10_000),
        main_length_json: str | None = Form(default=None, max_length=1_000),
        mask: UploadFile = File(),
        source: UploadFile = File(),
    ) -> dict:
        user = request.state.user
        if not store or not user:
            raise HTTPException(status_code=401, detail="authentication required")
        foreground, width, height = await _read_mask(mask)
        submission = _submission(
            workspace, metadata_json, main_length_json, width, height
        )
        if submission.metadata.catalog_id is not None:
            raise HTTPException(
                status_code=400,
                detail="independent submissions cannot have a Toybox catalog ID",
            )
        source_image = await _read_source(source)
        if source_image.size != (width, height):
            raise HTTPException(
                status_code=400, detail="source and mask dimensions do not match"
            )
        try:
            svg = await asyncio.to_thread(
                _locked_svg_bytes, workspace, foreground, submission.main_length
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        item_id = f"independent-{secrets.token_urlsafe(12)}"
        pending = workspace.pending_paths(item_id)
        try:
            atomic_json(
                pending["metadata"],
                {
                    "kind": "independent",
                    "item_id": item_id,
                    **submission.model_dump(mode="json"),
                },
            )
            atomic_bytes(pending["svg"], svg)
            atomic_image(pending["alternative"], source_image)
            store.put_submission(
                item_id,
                user,
                "contributor_photo",
                submission.model_dump_json(),
                kind="independent",
            )
        except Exception:
            if pending["directory"].is_dir():
                shutil.rmtree(pending["directory"])
            raise
        return {"item_id": item_id, "status": "pending_review"}
