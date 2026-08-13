"""Catalog, image-cache, review-artifact, and prefetch operations."""

from __future__ import annotations

import json
import os
import shutil
import threading
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from fastapi import HTTPException
from PIL import Image, ImageOps

from .artifacts import (
    atomic_bytes,
    atomic_image,
    atomic_json,
    item_directory,
    item_paths,
    read_state,
)
from .catalog import slug, ssl_context_for
from .hosted import HostedStore, PublicQueue, User
from .models import GuestMetadata, IndependentSubmission, ReviewState

SOURCE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
RATINGS = {"unusable", "bad_perspective", "good"}
PREFETCH_LOW_WATER = 2
PREFETCH_HIGH_WATER = 10
PREFETCH_CACHE_LIMIT = 50
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
ALLOWED_IMAGE_FORMATS = {"BMP", "JPEG", "PNG", "TIFF", "WEBP"}


class Workspace:
    """The mutable state and domain operations for one application process."""

    def __init__(
        self,
        input_dir: Path,
        work_dir: Path,
        products_path: Path | None = None,
        image_base_url: str | None = None,
        dataset_dir: Path | None = None,
        hosted_store: HostedStore | None = None,
        pending_dir: Path | None = None,
    ):
        self.input_dir = input_dir.resolve()
        self.work_dir = work_dir.resolve()
        self.dataset_dir = dataset_dir.resolve() if dataset_dir else None
        self.pending_dir = pending_dir.resolve() if pending_dir else None
        self.image_base_url = image_base_url
        self.hosted_store = hosted_store
        self.static_dir = Path(__file__).resolve().parents[1] / "static"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if hosted_store:
            if not self.pending_dir:
                raise ValueError("hosted mode requires a pending directory")
            if not self.dataset_dir:
                raise ValueError("hosted mode requires a dataset directory")
            self.pending_dir.mkdir(parents=True, exist_ok=True)

        path = products_path.resolve() if products_path else None
        self.catalog = json.loads(path.read_text()) if path else []
        if not isinstance(self.catalog, list):
            raise ValueError("product catalog must contain a JSON list")
        size_pair_counts: dict[str, dict[str, int]] = {}
        for product in self.catalog:
            for size in product.get("sz", {}).get("s", []):
                label = size.get("sl")
                short_label = size.get("ShortLabel")
                if not label or not short_label:
                    continue
                labels = size_pair_counts.setdefault(short_label, {})
                labels[label] = labels.get(label, 0) + 1
        size_pairs = []
        for short_label, labels in size_pair_counts.items():
            exact = next(
                (
                    label
                    for label in labels
                    if label.casefold() == short_label.casefold()
                ),
                None,
            )
            label = exact or min(
                labels,
                key=lambda value: (-labels[value], len(value), value.casefold()),
            )
            size_pairs.append({"label": label, "short_label": short_label})
        self.metadata_options = {
            "product_types": sorted(
                {product.get("pt") for product in self.catalog if product.get("pt")}
            ),
            "species": sorted(
                {product.get("sp") for product in self.catalog if product.get("sp")}
            ),
            "tags": sorted(
                {
                    tag
                    for product in self.catalog
                    for tag in product.get("tags", [])
                    if isinstance(tag, str) and tag
                },
                key=str.casefold,
            ),
            "features": sorted(
                {
                    feature
                    for product in self.catalog
                    for feature, enabled in product.get("feat", {}).items()
                    if enabled
                }
            ),
            "size_labels": sorted(
                size_pairs,
                key=lambda pair: pair["short_label"].casefold(),
            ),
            "width_labels": sorted(
                {
                    product.get("sz", {}).get("wl")
                    for product in self.catalog
                    if product.get("sz", {}).get("wl")
                }
            ),
        }

        pics_by_stem: dict[str, set[str]] = {}
        for product in self.catalog:
            if not isinstance(product, dict) or not product.get("pic"):
                raise ValueError("every catalog product must have a pic path")
            link = product.get("link")
            if link and (
                urlparse(str(link)).scheme != "https"
                or not urlparse(str(link)).hostname
            ):
                raise ValueError(f"unsafe catalog product URL: {link}")
            pic = Path(product["pic"])
            if not pic.name or pic.suffix.lower() not in SOURCE_SUFFIXES:
                raise ValueError(f"unsupported catalog image path: {product['pic']}")
            pics_by_stem.setdefault(pic.stem, set()).add(product["pic"])

        self.catalog_by_stem: dict[str, list[dict]] = {}
        self.catalog_by_id: dict[str, dict] = {}
        self.catalog_sources: dict[str, Path] = {}
        self.catalog_pics: dict[str, str] = {}
        self.catalog_item_ids: dict[str, str] = {}
        catalog_ids: set[object] = set()
        for product in self.catalog:
            if product.get("id") in catalog_ids:
                raise ValueError(f"duplicate catalog product id: {product.get('id')}")
            catalog_ids.add(product.get("id"))
            self.catalog_by_id[str(product["id"])] = product
            pic = Path(product["pic"])
            item_id = (
                pic.stem
                if len(pics_by_stem[pic.stem]) == 1
                else f"{pic.stem}--{pic.parent.name}"
            )
            target = self.input_dir / f"{item_id}{pic.suffix.lower()}"
            if (
                item_id in self.catalog_pics
                and self.catalog_pics[item_id] != product["pic"]
            ):
                raise ValueError(f"duplicate catalog image id: {item_id}")
            self.catalog_by_stem.setdefault(item_id, []).append(product)
            self.catalog_sources[item_id] = target
            self.catalog_pics[item_id] = product["pic"]
            self.catalog_item_ids[product["pic"]] = item_id

        self.record_paths: dict[object, Path] = {}
        if self.dataset_dir:
            candidates: dict[Path, list[dict]] = {}
            for product in self.catalog:
                record_path = (
                    self.dataset_dir
                    / slug(product.get("vn"))
                    / slug(product.get("pt"))
                    / slug(product.get("n"))
                )
                candidates.setdefault(record_path, []).append(product)
            for record_path, products in candidates.items():
                for product in products:
                    self.record_paths[product["id"]] = (
                        record_path
                        if len(products) == 1
                        else record_path.with_name(
                            f"{record_path.name}--{product['id']}"
                        )
                    )

        self._rembg_session = None
        self._image_ssl_context = ssl_context_for(image_base_url or "")
        self.session_lock = threading.RLock()
        self._download_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active_id: str | None = None
        self._prefetch_ids: dict[int, list[str]] | None = {} if hosted_store else None
        self._prefetch_wake = threading.Event()
        self._prefetch_stop = threading.Event()
        self.public_queue = PublicQueue() if hosted_store else None

    def paths(self, item_id: str) -> dict[str, Path]:
        try:
            return item_paths(self.work_dir, item_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="unknown item") from error

    def require_item(self, item_id: str) -> None:
        if item_id not in self.queue_items():
            raise HTTPException(status_code=404, detail="unknown item")

    def validate_metadata_options(self, metadata: GuestMetadata) -> None:
        options = self.metadata_options
        if metadata.product_type not in options["product_types"]:
            raise ValueError("unknown product type")
        if metadata.species and metadata.species not in options["species"]:
            raise ValueError("unknown species")
        if not set(metadata.tags) <= set(options["tags"]):
            raise ValueError("unknown tag")
        if not set(metadata.features) <= set(options["features"]):
            raise ValueError("unknown feature")
        size_pairs = {
            (pair["label"], pair["short_label"]) for pair in options["size_labels"]
        }
        for size in metadata.sizes:
            if (size.label, size.short_label) not in size_pairs:
                raise ValueError("unknown size name pair")
            if size.widest_label and size.widest_label not in options["width_labels"]:
                raise ValueError("unknown widest-point label")

    def pending_paths(self, item_id: str) -> dict[str, Path]:
        if not self.pending_dir:
            raise RuntimeError("pending paths require hosted mode")
        try:
            directory = item_directory(self.pending_dir, item_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="unknown item") from error
        return {
            "directory": directory,
            "metadata": directory / "metadata.json",
            "svg": directory / "outline.svg",
            "alternative": directory / "alternative.png",
        }

    def discard_work(self, item_id: str) -> None:
        try:
            paths = item_paths(self.work_dir, item_id)
        except ValueError:
            return
        with self._cache_lock:
            keep_catalog_cache = (
                item_id in self.catalog_sources
                and not paths["alternative"].exists()
                and paths["source"].is_file()
                and paths["rembg"].is_file()
            )
            if keep_catalog_cache:
                for kind in ("edits", "mask", "cutout", "svg", "metadata"):
                    paths[kind].unlink(missing_ok=True)
                os.utime(paths["directory"])
            elif paths["directory"].is_dir():
                shutil.rmtree(paths["directory"])

    def _prefetch_protected(self) -> set[str]:
        with self._active_lock:
            selected = self._prefetch_ids
            protected = {self._active_id} if self._active_id else set()
            if selected is not None:
                protected.update(
                    item_id for item_ids in selected.values() for item_id in item_ids
                )
        if self.hosted_store:
            protected.update(self.hosted_store.claims())
        return protected

    def _prune_catalog_cache(self) -> None:
        entries = []
        for item_id, catalog_source in self.catalog_sources.items():
            paths = self.paths(item_id)
            candidates = [
                path for path in (catalog_source, paths["directory"]) if path.exists()
            ]
            if candidates:
                entries.append(
                    (max(path.stat().st_mtime_ns for path in candidates), item_id)
                )
        excess = len(entries) - PREFETCH_CACHE_LIMIT
        if excess <= 0:
            return
        protected = self._prefetch_protected()
        with self._cache_lock:
            for _, item_id in sorted(entries):
                if excess <= 0:
                    break
                if item_id in protected:
                    continue
                paths = self.paths(item_id)
                if paths["directory"].is_dir():
                    shutil.rmtree(paths["directory"])
                if self.hosted_store:
                    self.catalog_sources[item_id].unlink(missing_ok=True)
                excess -= 1

    def _touch_catalog_cache(self, item_id: str) -> None:
        if item_id not in self.catalog_sources:
            return
        paths = self.paths(item_id)
        with self._cache_lock:
            if paths["directory"].is_dir():
                os.utime(paths["directory"])
        self._prune_catalog_cache()

    def remove_background(self, data: bytes) -> bytes:
        from rembg import new_session, remove

        with self.session_lock:
            if self._rembg_session is None:
                self._rembg_session = new_session()
            return remove(data, session=self._rembg_session)

    def sources(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for path in sorted(self.input_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if path.stem in {".", ".."}:
                raise RuntimeError(f"unsafe input id: {path.stem}")
            if path.stem in found:
                raise RuntimeError(f"duplicate input id: {path.stem}")
            found[path.stem] = path
        return found

    def queue_items(self) -> dict[str, Path]:
        if not self.image_base_url:
            return self.sources()
        items = dict(self.catalog_sources)
        items.update(self.sources())
        return items

    def download_source(self, item_id: str) -> Path:
        try:
            target = self.catalog_sources[item_id]
            pic = self.catalog_pics[item_id]
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown item") from error
        if target.exists():
            return target
        if not self.image_base_url:
            raise HTTPException(status_code=404, detail="source image is not available")

        with self._download_lock:
            if target.exists():
                return target
            url = urljoin(self.image_base_url.rstrip("/") + "/", pic.lstrip("/"))
            base_host = urlparse(self.image_base_url).hostname
            if urlparse(url).scheme != "https" or urlparse(url).hostname != base_host:
                raise ValueError(f"unsafe catalog image URL: {url}")
            request = Request(url, headers={"User-Agent": "Batch Outliner/1.0"})
            with urlopen(
                request, timeout=30, context=self._image_ssl_context
            ) as response:
                if urlparse(response.geturl()).hostname != base_host:
                    raise ValueError("catalog image redirected to another host")
                data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError(
                    f"catalog image is larger than {MAX_IMAGE_BYTES} bytes"
                )
            try:
                with Image.open(BytesIO(data)) as image:
                    if image.format not in ALLOWED_IMAGE_FORMATS:
                        raise ValueError(
                            f"downloaded file has an unsupported format: {url}"
                        )
                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        raise ValueError(f"downloaded image has too many pixels: {url}")
                    image.verify()
            except OSError as error:
                raise ValueError(f"downloaded file is not an image: {url}") from error
            atomic_bytes(target, data)
            print(f"Downloaded image: {item_id}", flush=True)
        return target

    def source_for(self, item_id: str) -> Path:
        self.require_item(item_id)
        alternative = self.paths(item_id)["alternative"]
        if alternative.exists():
            return alternative
        source = self.sources().get(item_id)
        return source if source else self.download_source(item_id)

    def prepare(self, item_id: str) -> tuple[dict[str, Path], int, int]:
        source = self.source_for(item_id)
        paths = self.paths(item_id)
        paths["directory"].mkdir(parents=True, exist_ok=True)

        if not paths["source"].exists() or not paths["rembg"].exists():
            with self.session_lock:
                if not paths["source"].exists():
                    with Image.open(source) as image:
                        normalized = ImageOps.exif_transpose(image).convert("RGB")
                    atomic_image(paths["source"], normalized)
                if not paths["rembg"].exists():
                    atomic_bytes(
                        paths["rembg"],
                        self.remove_background(paths["source"].read_bytes()),
                    )

        with Image.open(paths["rembg"]) as image:
            width, height = image.size
        self._touch_catalog_cache(item_id)
        return paths, width, height

    def unpublish(self, item_id: str) -> None:
        for product in self.catalog_by_stem.get(item_id, []):
            directory = self.record_paths.get(product["id"])
            if directory:
                for name in ("metadata.json", "outline.svg"):
                    (directory / name).unlink(missing_ok=True)

    def published_record(self, product: dict) -> tuple[dict, Path] | None:
        directory = self.record_paths.get(product["id"])
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

    def published_item(self, item_id: str) -> list[tuple[dict, Path]] | None:
        products = self.catalog_by_stem.get(item_id, [])
        records = [self.published_record(product) for product in products]
        return records if records and all(records) else None

    @staticmethod
    def record_document(product: dict, state: ReviewState, source: str) -> dict:
        return {
            "schema_version": 1,
            "catalog_id": product["id"],
            "quality": state.rating,
            "source": source,
        }

    @staticmethod
    def catalog_download_document(
        product: dict, state: ReviewState, source: str
    ) -> dict:
        return {
            "submission_version": 1,
            "catalog_id": product["id"],
            "vendor": product.get("vn", ""),
            "product_type": product.get("pt", ""),
            "name": product.get("n", ""),
            "product_url": product.get("link"),
            "species": product.get("sp"),
            "quality": state.rating,
            "source": source,
            "tags": product.get("tags", []),
            "features": [
                feature
                for feature, enabled in product.get("feat", {}).items()
                if enabled
            ],
            "sizes": [
                {
                    "label": size.get("sl"),
                    "short_label": size.get("ShortLabel"),
                    "price": size.get("p"),
                    "length": size.get("len"),
                    "circumference": size.get("circ"),
                    "widest_circumference": size.get("wcirc"),
                    "widest_label": product.get("sz", {}).get("wl") or None,
                    "unit": "in",
                }
                for size in product.get("sz", {}).get("s", [])
            ],
            "notes": None,
        }

    @staticmethod
    def independent_document(record_id: str, metadata: GuestMetadata) -> dict:
        return {
            "schema_version": 1,
            "record_id": record_id,
            "catalog_id": None,
            "vendor": metadata.vendor,
            "product_type": metadata.product_type,
            "name": metadata.name,
            "product_url": str(metadata.product_url) if metadata.product_url else None,
            "species": metadata.species,
            "quality": metadata.quality,
            "source": "alternative",
            "tags": metadata.tags,
            "features": metadata.features,
            "sizes": [size.model_dump(mode="json") for size in metadata.sizes],
            "notes": metadata.notes,
        }

    def independent_records(self) -> dict[str, tuple[dict, Path]]:
        records = {}
        if not self.dataset_dir:
            return records
        for path in self.dataset_dir.rglob("metadata.json"):
            try:
                metadata = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            record_id = metadata.get("record_id")
            if metadata.get("catalog_id") is None and isinstance(record_id, str):
                records[record_id] = (metadata, path.parent)
        return records

    def independent_item_summary(
        self,
        record_id: str,
        metadata: dict,
        directory: Path,
        pending: bool = False,
    ) -> dict:
        return {
            "id": record_id,
            "filename": metadata["name"],
            "status": "done",
            "workflow_status": "pending_review" if pending else "in_catalog",
            "rating": metadata["quality"],
            "published": True,
            "read_only": True,
            "pending_review": pending,
            "provenance": metadata.get("source", "alternative"),
            "svg_url": (
                f"/api/community/{quote(record_id, safe='')}/outline.svg"
                "?show_length=true&invert_colors=true"
            ),
            "has_alternative": False,
            "claimed_by": None,
            "claim_expires_at": None,
            "independent": True,
            "metadata": metadata,
            "products": [
                {
                    "id": None,
                    "n": metadata["name"],
                    "vn": metadata["vendor"],
                    "pt": metadata["product_type"],
                    "link": metadata.get("product_url"),
                    "sp": metadata.get("species"),
                    "tags": metadata.get("tags", []),
                    "feat": {feature: True for feature in metadata.get("features", [])},
                    "sz": {"s": metadata.get("sizes", [])},
                }
            ],
        }

    def publish(
        self,
        item_id: str,
        state: ReviewState,
        svg_path: Path | None,
        source: str | None = None,
    ) -> None:
        for product in self.catalog_by_stem.get(item_id, []):
            directory = self.record_paths.get(product["id"])
            if not directory:
                continue
            atomic_json(
                directory / "metadata.json",
                self.record_document(
                    product,
                    state,
                    source
                    or (
                        "alternative"
                        if self.paths(item_id)["alternative"].exists()
                        else "catalog"
                    ),
                ),
            )
            published_svg = directory / "outline.svg"
            if svg_path:
                atomic_bytes(published_svg, svg_path.read_bytes())
            else:
                published_svg.unlink(missing_ok=True)

    def publish_independent(
        self,
        item_id: str,
        submission: IndependentSubmission,
        svg_path: Path,
    ) -> Path:
        if not self.dataset_dir:
            raise RuntimeError("publishing requires a dataset directory")
        metadata = submission.metadata
        directory = (
            self.dataset_dir
            / slug(metadata.vendor)
            / slug(metadata.product_type)
            / slug(metadata.name)
        )
        if directory.exists():
            directory = directory.with_name(f"{directory.name}--{item_id}")
        atomic_json(
            directory / "metadata.json",
            self.independent_document(f"community:{item_id}", metadata),
        )
        atomic_bytes(directory / "outline.svg", svg_path.read_bytes())
        return directory

    def update_independent(self, record_id: str, metadata: GuestMetadata) -> Path:
        existing = self.independent_records().get(record_id)
        if not existing:
            raise ValueError("independent record does not exist")
        _, old_directory = existing
        new_directory = (
            self.dataset_dir
            / slug(metadata.vendor)
            / slug(metadata.product_type)
            / slug(metadata.name)
        )
        if new_directory != old_directory:
            if new_directory.exists():
                new_directory = new_directory.with_name(
                    f"{new_directory.name}--{slug(record_id)}"
                )
            new_directory.parent.mkdir(parents=True, exist_ok=True)
            old_directory.replace(new_directory)
        atomic_json(
            new_directory / "metadata.json",
            self.independent_document(record_id, metadata),
        )
        return new_directory

    def reset_review(
        self,
        paths: dict[str, Path],
        item_id: str,
        source: str,
        re_review: bool = False,
        keep_prepared: bool = False,
    ) -> None:
        threshold = read_state(paths["metadata"]).alpha_threshold
        kinds = ("edits", "mask", "cutout", "svg")
        if not keep_prepared:
            kinds = ("source", "rembg", *kinds)
        for kind in kinds:
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

    def item_summary(
        self,
        item_id: str,
        source: Path,
        user: User | None = None,
        claim_records: dict[str, dict] | None = None,
        submission_records: dict[str, object] | None = None,
    ) -> dict:
        paths = self.paths(item_id)
        state = read_state(paths["metadata"])
        products = self.catalog_by_stem.get(item_id, [])
        published = self.published_item(item_id)
        claim = (claim_records or {}).get(item_id)
        submission = (submission_records or {}).get(item_id)
        if self.hosted_store and submission is None and submission_records is None:
            submission = self.hosted_store.submission(item_id)
        own_active_review = bool(
            self.hosted_store
            and user
            and claim
            and claim["user_id"] == user.id
            and state.re_review
        )
        workflow_status = "never_worked"
        read_only = False
        if self.hosted_store and submission:
            state = ReviewState.model_validate_json(submission["state_json"])
            status = "done"
            workflow_status = "pending_review"
            read_only = True
            rating = state.rating
            provenance = submission["source"]
            svg_product = None
        elif state.re_review and (not self.hosted_store or own_active_review):
            status = "pending"
            rating = state.rating
            provenance = None
            svg_product = None
        elif published:
            qualities = {record[0]["quality"] for record in published}
            origins = {record[0].get("source", "catalog") for record in published}
            status = "done"
            workflow_status = "in_catalog"
            read_only = True
            rating = qualities.pop() if len(qualities) == 1 else None
            provenance = origins.pop() if len(origins) == 1 else "mixed"
            svg_product = next(
                (
                    product
                    for product in products
                    if (self.record_paths[product["id"]] / "outline.svg").is_file()
                ),
                None,
            )
        elif self.dataset_dir and products:
            status = "pending"
            rating = None
            provenance = None
            svg_product = None
        else:
            status = state.status
            rating = state.rating
            provenance = None
            svg_product = None
        if self.hosted_store and submission and state.rating != "unusable":
            svg_url = f"/api/submissions/{quote(item_id, safe='')}/outline.svg"
        else:
            svg_url = (
                f"/api/products/{quote(str(svg_product['id']), safe='')}/outline.svg"
                "?show_length=true&invert_colors=true"
                if svg_product
                else None
            )
        return {
            "id": item_id,
            "filename": source.name,
            "status": status,
            "workflow_status": workflow_status,
            "rating": rating,
            "published": bool(published)
            and (not own_active_review if self.hosted_store else not state.re_review),
            "read_only": read_only,
            "pending_review": bool(submission),
            "provenance": provenance,
            "svg_url": svg_url,
            "has_alternative": (
                self.pending_paths(item_id)["alternative"].exists()
                if self.hosted_store and submission
                else paths["alternative"].exists()
            ),
            "claimed_by": (
                claim["name"]
                if claim and (not user or claim["user_id"] != user.id)
                else None
            ),
            "claim_expires_at": claim["expires_at"] if claim else None,
            "products": [
                {
                    "id": product["id"],
                    "n": product.get("n", ""),
                    "vn": product.get("vn", ""),
                    "pt": product.get("pt", ""),
                    "link": product.get("link"),
                    "sp": product.get("sp"),
                    "tags": product.get("tags", []),
                    "feat": product.get("feat", {}),
                    "sz": product.get("sz", {"s": []}),
                }
                for product in products
            ],
        }

    def catalog_records(self) -> list[dict]:
        records = []
        for product in self.catalog:
            item_id = self.catalog_item_ids[product["pic"]]
            published = self.published_record(product)
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

    @staticmethod
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

    def set_active(self, item_id: str) -> None:
        with self._active_lock:
            self._active_id = item_id
        self._prefetch_wake.set()

    def select_prefetch(self, owner_id: int, item_ids: list[str]) -> int:
        known = set(self.queue_items())
        unique_ids = list(dict.fromkeys(item_ids))
        unknown = [item_id for item_id in unique_ids if item_id not in known]
        if unknown:
            raise ValueError(f"unknown item: {unknown[0]}")
        with self._active_lock:
            if self._prefetch_ids is None:
                self._prefetch_ids = {}
            if unique_ids:
                self._prefetch_ids[owner_id] = unique_ids
            else:
                self._prefetch_ids.pop(owner_id, None)
        self._prefetch_wake.set()
        return len(unique_ids)

    def _prefetch_window(self) -> list[str]:
        with self._active_lock:
            current_id = self._active_id
            selected_ids = (
                None
                if self._prefetch_ids is None
                else {
                    owner_id: list(item_ids)
                    for owner_id, item_ids in self._prefetch_ids.items()
                }
            )
        items = self.queue_items()
        if selected_ids is None:
            ordered = [(0, item_id) for item_id in items]
            if current_id in items:
                index = next(
                    index
                    for index, (_, item_id) in enumerate(ordered)
                    if item_id == current_id
                )
                ordered = ordered[index + 1 :] + ordered[:index]
            limit = PREFETCH_HIGH_WATER
        else:
            ordered = []
            depth = max(
                (len(item_ids) for item_ids in selected_ids.values()), default=0
            )
            for index in range(depth):
                ordered.extend(
                    (owner_id, item_ids[index])
                    for owner_id, item_ids in selected_ids.items()
                    if index < len(item_ids)
                )
            limit = PREFETCH_CACHE_LIMIT

        claims = self.hosted_store.claims() if self.hosted_store else {}
        submissions = (
            {row["item_id"]: row for row in self.hosted_store.submissions()}
            if self.hosted_store
            else {}
        )
        window = []
        for owner_id, item_id in ordered:
            if item_id in window or item_id == current_id:
                continue
            user = User(owner_id, "", False) if self.hosted_store else None
            summary = self.item_summary(
                item_id,
                items[item_id],
                user,
                claims,
                submissions,
            )
            if summary["workflow_status"] != "never_worked" or summary["claimed_by"]:
                continue
            window.append(item_id)
            if len(window) >= limit:
                break
        return window

    def prefetch_worker(self) -> None:
        while not self._prefetch_stop.is_set():
            self._prefetch_wake.wait()
            self._prefetch_wake.clear()
            if self._prefetch_stop.is_set():
                return
            try:
                window = self._prefetch_window()
                ready = sum(self.paths(item_id)["rembg"].exists() for item_id in window)
                if not self.hosted_store and ready >= PREFETCH_LOW_WATER:
                    continue
                for item_id in window:
                    if self._prefetch_stop.is_set():
                        return
                    if self._prefetch_wake.is_set():
                        break
                    if self.paths(item_id)["rembg"].exists():
                        continue
                    self.prepare(item_id)
                    print(f"Prefetched mask: {item_id}", flush=True)
            except Exception as error:
                print(f"Mask prefetch failed: {error}", flush=True)

    def start_prefetch(self) -> threading.Thread | None:
        worker = threading.Thread(
            target=self.prefetch_worker, name="mask-prefetch", daemon=True
        )
        worker.start()
        self._prefetch_wake.set()
        return worker

    def stop_prefetch(self, worker: threading.Thread | None) -> None:
        if not worker:
            return
        self._prefetch_stop.set()
        self._prefetch_wake.set()
        worker.join(timeout=5)
