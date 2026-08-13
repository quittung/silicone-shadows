"""Read and write review artifacts on disk."""

import json
import math
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from outline import largest_component

from .models import MainLength, ReviewState


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


def item_directory(root: Path, item_id: str) -> Path:
    root = root.resolve()
    if not item_id or item_id in {".", ".."} or Path(item_id).name != item_id:
        raise ValueError("unsafe item ID")
    candidate = root / item_id
    if candidate.is_symlink():
        raise ValueError("unsafe item ID")
    directory = candidate.resolve()
    if directory.parent != root:
        raise ValueError("unsafe item ID")
    return directory


def item_paths(work_dir: Path, item_id: str) -> dict[str, Path]:
    directory = item_directory(work_dir, item_id)
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
