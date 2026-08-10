#!/usr/bin/env python3
"""Local browser UI and API for reviewing product silhouettes."""

from __future__ import annotations

import argparse
import json
import math
import re
import ssl
import threading
import unicodedata
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from outline import largest_component, trace_aligned_svg

SOURCE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
RATINGS = {"unusable", "bad_perspective", "good"}
PREFETCH_LOW_WATER = 2
PREFETCH_HIGH_WATER = 10
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_CATALOG_BYTES = 16 * 1024 * 1024


class MainLength(BaseModel):
    start: tuple[float, float]
    end: tuple[float, float]


class ReviewState(BaseModel):
    status: Literal["pending", "done"] = "pending"
    rating: Literal["unusable", "bad_perspective", "good"] | None = None
    alpha_threshold: int = Field(default=128, ge=1, le=255)
    main_length: MainLength | None = None
    re_review: bool = False


class PrefetchSelection(BaseModel):
    item_ids: list[str] = Field(max_length=10_000)


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def atomic_json(path: Path, value: dict) -> None:
    atomic_bytes(path, (json.dumps(value, indent=2) + "\n").encode())


def atomic_image(path: Path, image: Image.Image) -> None:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    atomic_bytes(path, buffer.getvalue())


def ssl_context_for(url: str):
    # fantasytoybox.net currently serves only its leaf certificate, so CLI TLS
    # clients cannot build the chain; keep this exception scoped to that host.
    return (
        ssl._create_unverified_context()
        if urlparse(url).hostname == "fantasytoybox.net"
        else None
    )


def ensure_catalog(config_path: Path) -> Path:
    config_path = config_path.resolve()
    config = json.loads(config_path.read_text())
    try:
        version = int(config["version"])
        url = str(config["url_template"]).format(version=version)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid catalog source descriptor") from error
    cache = config_path.parent / ".local" / "catalog" / f"products_v{version}.json"
    if cache.exists():
        return cache

    request = Request(url, headers={"User-Agent": "Batch Outliner/1.0"})
    with urlopen(request, timeout=30, context=ssl_context_for(url)) as response:
        if urlparse(response.geturl()).hostname != urlparse(url).hostname:
            raise ValueError("catalog redirected to another host")
        data = response.read(MAX_CATALOG_BYTES + 1)
    if len(data) > MAX_CATALOG_BYTES:
        raise ValueError("catalog is too large")
    try:
        catalog = json.loads(data)
    except json.JSONDecodeError as error:
        raise ValueError("downloaded catalog is not JSON") from error
    if not isinstance(catalog, list):
        raise ValueError("downloaded catalog is not a product list")
    atomic_bytes(cache, data)
    print(f"Downloaded catalog v{version}: {cache}")
    return cache


def item_paths(work_dir: Path, item_id: str) -> dict[str, Path]:
    directory = work_dir / item_id
    return {
        "directory": directory,
        "alternative": directory / "alternative.png",
        "source": directory / "source.png",
        "rembg": directory / "rembg.png",
        "edits": directory / "edits.png",
        "mask": directory / "mask.png",
        "cutout": directory / "cutout.png",
        "svg": directory / "outline.svg",
        "metadata": directory / "metadata.json",
    }


def read_state(path: Path) -> ReviewState:
    if not path.exists():
        return ReviewState()
    return ReviewState.model_validate_json(path.read_text())


def validate_length(line: MainLength | None, width: int, height: int) -> None:
    if line is None:
        return
    points = (*line.start, *line.end)
    if not all(math.isfinite(value) for value in points):
        raise ValueError("main-length coordinates must be finite")
    for x, y in (line.start, line.end):
        if not 0 <= x <= width or not 0 <= y <= height:
            raise ValueError("main-length endpoints must be inside the image")
    if math.dist(line.start, line.end) < 1:
        raise ValueError("main-length endpoints are too close together")


def slug(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"


def final_mask(rembg_path: Path, edits_path: Path, threshold: int) -> np.ndarray:
    with Image.open(rembg_path) as image:
        alpha = np.asarray(image.convert("RGBA").getchannel("A"), dtype=np.uint8)
    mask = alpha >= threshold

    if edits_path.exists():
        with Image.open(edits_path) as image:
            edits = np.asarray(image.convert("RGBA"), dtype=np.uint8)
        if edits.shape[:2] != mask.shape:
            raise ValueError("saved paint layer dimensions do not match the image")
        touched = edits[..., 3] > 0
        mask[touched] = edits[..., 0][touched] >= 128

    return largest_component(mask)


def svg_main_length(svg_path: Path) -> MainLength:
    root = ET.parse(svg_path).getroot()
    line = next(
        (element for element in root.iter() if element.get("id") == "main-length"),
        None,
    )
    if line is None:
        raise ValueError("SVG has no main-length vector")
    return MainLength(
        start=(float(line.get("x1")), float(line.get("y1"))),
        end=(float(line.get("x2")), float(line.get("y2"))),
    )


def create_app(
    input_dir: Path,
    work_dir: Path,
    products_path: Path | None = None,
    image_base_url: str | None = None,
    dataset_dir: Path | None = None,
) -> FastAPI:
    input_dir = input_dir.resolve()
    work_dir = work_dir.resolve()
    products_path = products_path.resolve() if products_path else None
    dataset_dir = dataset_dir.resolve() if dataset_dir else None
    static_dir = Path(__file__).resolve().parent / "static"
    work_dir.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(products_path.read_text()) if products_path else []
    if not isinstance(catalog, list):
        raise ValueError("product catalog must contain a JSON list")
    pics_by_stem: dict[str, set[str]] = {}
    for product in catalog:
        if not isinstance(product, dict) or not product.get("pic"):
            raise ValueError("every catalog product must have a pic path")
        pic = Path(product["pic"])
        if not pic.name or pic.suffix.lower() not in SOURCE_SUFFIXES:
            raise ValueError(f"unsupported catalog image path: {product['pic']}")
        pics_by_stem.setdefault(pic.stem, set()).add(product["pic"])

    catalog_by_stem: dict[str, list[dict]] = {}
    catalog_by_id: dict[str, dict] = {}
    catalog_sources: dict[str, Path] = {}
    catalog_pics: dict[str, str] = {}
    catalog_item_ids: dict[str, str] = {}
    catalog_ids: set[object] = set()
    for product in catalog:
        if product.get("id") in catalog_ids:
            raise ValueError(f"duplicate catalog product id: {product.get('id')}")
        catalog_ids.add(product.get("id"))
        catalog_by_id[str(product["id"])] = product
        pic = Path(product["pic"])
        item_id = (
            pic.stem
            if len(pics_by_stem[pic.stem]) == 1
            else f"{pic.stem}--{pic.parent.name}"
        )
        target = input_dir / f"{item_id}{pic.suffix.lower()}"
        if item_id in catalog_pics and catalog_pics[item_id] != product["pic"]:
            raise ValueError(f"duplicate catalog image id: {item_id}")
        catalog_by_stem.setdefault(item_id, []).append(product)
        catalog_sources[item_id] = target
        catalog_pics[item_id] = product["pic"]
        catalog_item_ids[product["pic"]] = item_id

    record_paths: dict[object, Path] = {}
    if dataset_dir:
        candidates: dict[Path, list[dict]] = {}
        for product in catalog:
            path = (
                dataset_dir
                / slug(product.get("vn"))
                / slug(product.get("pt"))
                / slug(product.get("n"))
            )
            candidates.setdefault(path, []).append(product)
        for path, products in candidates.items():
            for product in products:
                record_paths[product["id"]] = (
                    path
                    if len(products) == 1
                    else path.with_name(f"{path.name}--{product['id']}")
                )
    session = None
    image_ssl_context = ssl_context_for(image_base_url or "")
    session_lock = threading.Lock()
    download_lock = threading.Lock()
    active_lock = threading.Lock()
    active_id = None
    prefetch_ids = None
    prefetch_wake = threading.Event()
    prefetch_stop = threading.Event()

    def sources() -> dict[str, Path]:
        found: dict[str, Path] = {}
        for path in sorted(input_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if path.stem in {".", ".."}:
                raise RuntimeError(f"unsafe input id: {path.stem}")
            if path.stem in found:
                raise RuntimeError(f"duplicate input id: {path.stem}")
            found[path.stem] = path
        return found

    def queue_items() -> dict[str, Path]:
        if not image_base_url:
            return sources()
        items = dict(catalog_sources)
        items.update(sources())
        return items

    def download_source(item_id: str) -> Path:
        try:
            target = catalog_sources[item_id]
            pic = catalog_pics[item_id]
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown item") from error
        if target.exists():
            return target
        if not image_base_url:
            raise HTTPException(status_code=404, detail="source image is not available")

        with download_lock:
            if target.exists():
                return target
            url = urljoin(image_base_url.rstrip("/") + "/", pic.lstrip("/"))
            base_host = urlparse(image_base_url).hostname
            if urlparse(url).scheme != "https" or urlparse(url).hostname != base_host:
                raise ValueError(f"unsafe catalog image URL: {url}")
            request = Request(url, headers={"User-Agent": "Batch Outliner/1.0"})
            with urlopen(request, timeout=30, context=image_ssl_context) as response:
                if urlparse(response.geturl()).hostname != base_host:
                    raise ValueError("catalog image redirected to another host")
                data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError(f"catalog image is larger than {MAX_IMAGE_BYTES} bytes")
            try:
                with Image.open(BytesIO(data)) as image:
                    image.verify()
            except OSError as error:
                raise ValueError(f"downloaded file is not an image: {url}") from error
            atomic_bytes(target, data)
            print(f"Downloaded image: {item_id}", flush=True)
        return target

    def source_for(item_id: str) -> Path:
        if item_id not in queue_items():
            raise HTTPException(status_code=404, detail="unknown item")
        alternative = item_paths(work_dir, item_id)["alternative"]
        if alternative.exists():
            return alternative
        source = sources().get(item_id)
        return source if source else download_source(item_id)

    def prepare(item_id: str) -> tuple[dict[str, Path], int, int]:
        nonlocal session
        source = source_for(item_id)
        paths = item_paths(work_dir, item_id)
        paths["directory"].mkdir(parents=True, exist_ok=True)

        if not paths["source"].exists() or not paths["rembg"].exists():
            with session_lock:
                if not paths["source"].exists():
                    with Image.open(source) as image:
                        normalized = ImageOps.exif_transpose(image).convert("RGB")
                    atomic_image(paths["source"], normalized)

                if not paths["rembg"].exists():
                    from rembg import new_session, remove

                    if session is None:
                        session = new_session()
                    result = remove(paths["source"].read_bytes(), session=session)
                    atomic_bytes(paths["rembg"], result)

        with Image.open(paths["rembg"]) as image:
            width, height = image.size
        return paths, width, height

    def unpublish(item_id: str) -> None:
        for product in catalog_by_stem.get(item_id, []):
            directory = record_paths.get(product["id"])
            if directory:
                for name in ("metadata.json", "outline.svg"):
                    (directory / name).unlink(missing_ok=True)

    def published_record(product: dict) -> tuple[dict, Path] | None:
        directory = record_paths.get(product["id"])
        metadata_path = directory / "metadata.json" if directory else None
        if not metadata_path or not metadata_path.is_file():
            return None
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if (
            metadata.get("catalog_id") != product["id"]
            or metadata.get("quality") not in RATINGS
        ):
            return None
        return metadata, directory

    def published_item(item_id: str) -> list[tuple[dict, Path]] | None:
        products = catalog_by_stem.get(item_id, [])
        records = [published_record(product) for product in products]
        return records if records and all(records) else None

    def publish(
        item_id: str,
        state: ReviewState,
        svg_path: Path | None,
    ) -> None:
        for product in catalog_by_stem.get(item_id, []):
            directory = record_paths.get(product["id"])
            if not directory:
                continue
            atomic_json(
                directory / "metadata.json",
                {
                    "schema_version": 1,
                    "catalog_id": product["id"],
                    "vendor": product.get("vn", ""),
                    "product_type": product.get("pt", ""),
                    "name": product.get("n", ""),
                    "quality": state.rating,
                    "source": (
                        "alternative"
                        if item_paths(work_dir, item_id)["alternative"].exists()
                        else "catalog"
                    ),
                },
            )
            published_svg = directory / "outline.svg"
            if svg_path:
                atomic_bytes(published_svg, svg_path.read_bytes())
            else:
                published_svg.unlink(missing_ok=True)

    def reset_review(
        paths: dict[str, Path], item_id: str, source: str, re_review: bool = False
    ) -> None:
        threshold = read_state(paths["metadata"]).alpha_threshold
        for kind in ("source", "rembg", "edits", "mask", "cutout", "svg"):
            paths[kind].unlink(missing_ok=True)
        state = ReviewState(alpha_threshold=threshold, re_review=re_review)
        atomic_json(
            paths["metadata"],
            {
                "version": 1,
                "id": item_id,
                "source": source,
                **state.model_dump(mode="json"),
            },
        )

    def item_summary(item_id: str, source: Path) -> dict:
        paths = item_paths(work_dir, item_id)
        state = read_state(paths["metadata"])
        products = catalog_by_stem.get(item_id, [])
        published = published_item(item_id)
        if state.re_review:
            status = "pending"
            rating = state.rating
            provenance = None
            svg_product = None
        elif published:
            qualities = {record[0]["quality"] for record in published}
            origins = {record[0].get("source", "catalog") for record in published}
            status = "done"
            rating = qualities.pop() if len(qualities) == 1 else None
            provenance = origins.pop() if len(origins) == 1 else "mixed"
            svg_product = next(
                (
                    product
                    for product in products
                    if (record_paths[product["id"]] / "outline.svg").is_file()
                ),
                None,
            )
        elif dataset_dir and products:
            status = "pending"
            rating = None
            provenance = None
            svg_product = None
        else:
            status = state.status
            rating = state.rating
            provenance = None
            svg_product = None
        return {
            "id": item_id,
            "filename": source.name,
            "status": status,
            "rating": rating,
            "published": bool(published) and not state.re_review,
            "provenance": provenance,
            "svg_url": (
                f"/api/products/{quote(str(svg_product['id']), safe='')}/outline.svg"
                if svg_product
                else None
            ),
            "has_alternative": paths["alternative"].exists(),
            "products": [
                {
                    "id": product["id"],
                    "n": product.get("n", ""),
                    "vn": product.get("vn", ""),
                    "pt": product.get("pt", ""),
                }
                for product in products
            ],
        }

    def catalog_records() -> list[dict]:
        records = []
        for product in catalog:
            item_id = catalog_item_ids[product["pic"]]
            published = published_record(product)
            metadata, directory = published if published else ({}, None)
            rating = metadata.get("quality")
            sizes = [
                size
                for size in product.get("sz", {}).get("s", [])
                if isinstance(size.get("len"), (int, float)) and size["len"] > 0
            ]
            records.append(
                {
                    "product": product,
                    "item_id": item_id,
                    "reviewed": bool(published),
                    "rating": rating,
                    "directory": directory,
                    "sizes": sizes,
                    "comparable": (
                        bool(published)
                        and rating != "unusable"
                        and (directory / "outline.svg").is_file()
                        and bool(sizes)
                    ),
                }
            )
        return records

    def breakdown(records: list[dict], field: str) -> list[dict]:
        grouped: dict[str, dict] = {}
        for record in records:
            key = str(record["product"].get(field) or "Unknown")
            row = grouped.setdefault(
                key,
                {
                    "name": key,
                    "total": 0,
                    "reviewed": 0,
                    "good": 0,
                    "bad_perspective": 0,
                    "unusable": 0,
                },
            )
            row["total"] += 1
            row["reviewed"] += int(record["reviewed"])
            if record["rating"] in RATINGS:
                row[record["rating"]] += 1
        return sorted(grouped.values(), key=lambda row: (-row["total"], row["name"]))

    def set_active(item_id: str) -> None:
        nonlocal active_id
        with active_lock:
            active_id = item_id
        prefetch_wake.set()

    def prefetch_window() -> list[str]:
        nonlocal prefetch_ids
        with active_lock:
            current_id = active_id
            selected_ids = None if prefetch_ids is None else list(prefetch_ids)
        ordered = selected_ids if selected_ids is not None else list(queue_items())
        if current_id in ordered:
            index = ordered.index(current_id)
            ordered = ordered[index + 1 :] + ordered[:index]
        return [
            item_id
            for item_id in ordered
            if item_id != current_id
            and item_summary(item_id, queue_items()[item_id])["status"] != "done"
        ][:PREFETCH_HIGH_WATER]

    def prefetch_worker() -> None:
        while not prefetch_stop.is_set():
            prefetch_wake.wait()
            prefetch_wake.clear()
            if prefetch_stop.is_set():
                return
            try:
                window = prefetch_window()
                ready = sum(
                    item_paths(work_dir, item_id)["rembg"].exists()
                    for item_id in window
                )
                if ready >= PREFETCH_LOW_WATER:
                    continue
                for item_id in window:
                    if prefetch_stop.is_set():
                        return
                    if prefetch_wake.is_set():
                        break
                    if item_paths(work_dir, item_id)["rembg"].exists():
                        continue
                    prepare(item_id)
                    print(f"Prefetched mask: {item_id}", flush=True)
            except Exception as error:
                print(f"Mask prefetch failed: {error}", flush=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        worker = threading.Thread(
            target=prefetch_worker, name="mask-prefetch", daemon=True
        )
        worker.start()
        prefetch_wake.set()
        try:
            yield
        finally:
            prefetch_stop.set()
            prefetch_wake.set()
            worker.join(timeout=5)

    app = FastAPI(
        title="Batch Outliner", docs_url=None, redoc_url=None, lifespan=lifespan
    )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/stats")
    def stats_page() -> FileResponse:
        return FileResponse(static_dir / "stats.html")

    @app.get("/compare")
    def compare_page() -> FileResponse:
        return FileResponse(static_dir / "compare.html")

    @app.get("/api/items")
    def list_items() -> dict:
        items = [
            item_summary(item_id, source) for item_id, source in queue_items().items()
        ]
        return {
            "items": items,
            "total": len(items),
            "done": sum(item["status"] == "done" for item in items),
        }

    @app.post("/api/prefetch")
    def select_prefetch(selection: PrefetchSelection) -> dict:
        nonlocal prefetch_ids
        known = set(queue_items())
        item_ids = list(dict.fromkeys(selection.item_ids))
        unknown = [item_id for item_id in item_ids if item_id not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown item: {unknown[0]}")
        with active_lock:
            prefetch_ids = item_ids
        prefetch_wake.set()
        return {"selected": len(item_ids)}

    @app.get("/api/stats")
    def stats() -> dict:
        records = catalog_records()
        reviewed = sum(record["reviewed"] for record in records)
        summary = {
            "products": len(records),
            "reviewed": reviewed,
            "pending": len(records) - reviewed,
            "good": sum(record["rating"] == "good" for record in records),
            "bad_perspective": sum(
                record["rating"] == "bad_perspective" for record in records
            ),
            "unusable": sum(record["rating"] == "unusable" for record in records),
            "comparable": sum(record["comparable"] for record in records),
        }
        return {
            "summary": summary,
            "vendors": breakdown(records, "vn"),
            "product_types": breakdown(records, "pt"),
        }

    @app.get("/api/comparison/products")
    def comparison_products() -> dict:
        products = []
        for record in catalog_records():
            if not record["comparable"]:
                continue
            product = record["product"]
            svg_path = record["directory"] / "outline.svg"
            main_length = svg_main_length(svg_path)
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
                            "label": size.get("ShortLabel") or size.get("sl") or str(index + 1),
                            "name": size.get("sl") or size.get("ShortLabel") or str(index + 1),
                            "length_in": size["len"],
                        }
                        for index, size in enumerate(record["sizes"])
                    ],
                }
            )
        products.sort(key=lambda product: (product["vn"], product["n"], product["id"]))
        return {"products": products}

    @app.post("/api/items/{item_id}/prepare")
    def prepare_item(item_id: str) -> dict:
        set_active(item_id)
        if published_item(item_id) and not read_state(
            item_paths(work_dir, item_id)["metadata"]
        ).re_review:
            raise HTTPException(
                status_code=409,
                detail="published items are read-only until re-review is selected",
            )
        paths, width, height = prepare(item_id)
        state = read_state(paths["metadata"])
        encoded_id = quote(item_id, safe="")
        return {
            "id": item_id,
            "width": width,
            "height": height,
            "state": state.model_dump(mode="json"),
            "source_url": f"/api/items/{encoded_id}/file/source",
            "rembg_url": f"/api/items/{encoded_id}/file/rembg",
            "edits_url": (
                f"/api/items/{encoded_id}/file/edits"
                if paths["edits"].exists()
                else None
            ),
        }

    @app.post("/api/items/{item_id}/rereview")
    def rereview_item(item_id: str) -> dict:
        set_active(item_id)
        if not published_item(item_id):
            raise HTTPException(status_code=400, detail="item is not published")
        source = source_for(item_id)
        paths = item_paths(work_dir, item_id)
        with session_lock:
            reset_review(paths, item_id, source.name, re_review=True)
        return item_summary(item_id, queue_items()[item_id])

    @app.get("/api/products/{catalog_id}/outline.svg")
    def published_outline(catalog_id: str) -> FileResponse:
        product = catalog_by_id.get(catalog_id)
        published = published_record(product) if product else None
        if not published:
            raise HTTPException(status_code=404, detail="published product does not exist")
        path = published[1] / "outline.svg"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="product has no usable outline")
        return FileResponse(
            path,
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/items/{item_id}/alternative")
    async def replace_source(item_id: str, image: UploadFile = File()) -> dict:
        set_active(item_id)
        source_for(item_id)
        data = await image.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="alternative image is too large")
        try:
            with Image.open(BytesIO(data)) as uploaded:
                alternative = ImageOps.exif_transpose(uploaded).convert("RGB")
                alternative.load()
        except OSError as error:
            raise HTTPException(status_code=400, detail="invalid alternative image") from error

        paths = item_paths(work_dir, item_id)
        re_review = bool(published_item(item_id)) or read_state(
            paths["metadata"]
        ).re_review
        with session_lock:
            atomic_image(paths["alternative"], alternative)
            reset_review(
                paths,
                item_id,
                paths["alternative"].name,
                re_review=re_review,
            )
        _, width, height = prepare(item_id)
        return {
            "item": item_summary(item_id, queue_items()[item_id]),
            "width": width,
            "height": height,
        }

    @app.delete("/api/items/{item_id}/alternative")
    def restore_catalog_source(item_id: str) -> dict:
        set_active(item_id)
        paths = item_paths(work_dir, item_id)
        if not paths["alternative"].exists():
            raise HTTPException(status_code=400, detail="no alternative image is active")
        catalog_source = sources().get(item_id)
        if catalog_source is None:
            catalog_source = download_source(item_id)
        with session_lock:
            paths["alternative"].unlink()
            reset_review(
                paths,
                item_id,
                catalog_source.name,
                re_review=(
                    bool(published_item(item_id))
                    or read_state(paths["metadata"]).re_review
                ),
            )
        _, width, height = prepare(item_id)
        return {
            "item": item_summary(item_id, queue_items()[item_id]),
            "width": width,
            "height": height,
        }

    @app.get("/api/items/{item_id}/file/{kind}")
    def get_file(item_id: str, kind: str) -> FileResponse:
        source_for(item_id)
        if kind not in {"source", "rembg", "edits", "mask", "cutout", "svg"}:
            raise HTTPException(status_code=404, detail="unknown artifact")
        path = item_paths(work_dir, item_id)[kind]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="artifact does not exist")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.post("/api/items/{item_id}/save")
    async def save_item(
        item_id: str,
        state_json: str = Form(),
        edits: UploadFile | None = File(default=None),
    ) -> dict:
        set_active(item_id)
        paths, width, height = prepare(item_id)
        try:
            state = ReviewState.model_validate_json(state_json)
            validate_length(state.main_length, width, height)
        except (ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

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
            data = await edits.read()
            if len(data) > 64 * 1024 * 1024:
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

        if state.status == "done" and state.rating == "unusable":
            for kind in ("mask", "cutout", "svg"):
                paths[kind].unlink(missing_ok=True)
        elif state.status == "done":
            try:
                mask = final_mask(
                    paths["rembg"], paths["edits"], state.alpha_threshold
                )
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
        document = {
            "version": 1,
            "id": item_id,
            "source": source_for(item_id).name,
            **state.model_dump(mode="json"),
        }
        atomic_json(paths["metadata"], document)
        if state.status == "done":
            publish(
                item_id,
                state,
                paths["svg"] if state.rating != "unusable" else None,
            )
        elif not state.re_review:
            unpublish(item_id)
        return item_summary(item_id, source_for(item_id))

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Batch Outliner UI.")
    parser.add_argument("--input", type=Path, default=Path(".local/images"))
    parser.add_argument("--work", type=Path, default=Path(".local/work"))
    parser.add_argument("--products", type=Path)
    parser.add_argument(
        "--catalog-source", type=Path, default=Path("catalog_source.json")
    )
    parser.add_argument("--dataset", type=Path, default=Path("dataset"))
    parser.add_argument(
        "--image-base-url", default="https://fantasytoybox.net/"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    args.input.mkdir(parents=True, exist_ok=True)
    if args.products is None:
        try:
            args.products = ensure_catalog(args.catalog_source)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            parser.error(str(error))
    elif not args.products.is_file():
        parser.error(f"product catalog does not exist: {args.products}")
    print(f"Batch Outliner: http://{args.host}:{args.port}")
    uvicorn.run(
        create_app(
            args.input,
            args.work,
            args.products,
            args.image_base_url,
            args.dataset,
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
