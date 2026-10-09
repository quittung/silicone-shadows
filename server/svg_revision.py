"""Editable SVG silhouettes and lossless main-length realignment."""

import json
import math
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from outline import SVG_NAMESPACE, svg_number, write_svg, ALIGNED_SVG_LONGEST_SIDE
from .artifacts import atomic_bytes, atomic_json, final_mask, svg_main_length
from .models import MainLength, ReviewState


OUTLINE_REVISION_FILES = (
    "revision.svg",
    "revision-outline.json",
    "revision-mask.svg",
    "revision-rembg.png",
    "revision-edits.png",
    "revision-photo.png",
)


def initialize_outline(
    target: dict, saved_svg: Path, state: ReviewState, editable: bool, reference: Path
) -> dict:
    raw = {}
    atomic_bytes(target["svg"], saved_svg.read_bytes())
    atomic_bytes(target["directory"] / "revision.svg", saved_svg.read_bytes())
    if editable:
        # Similarity mapping from the original editor pixels to canonical SVG units.
        baseline = svg_main_length(saved_svg)
        start, end = state.main_length.start, state.main_length.end
        dx, dy = end[0] - start[0], end[1] - start[1]
        ux, uy = (
            baseline.end[0] - baseline.start[0],
            baseline.end[1] - baseline.start[1],
        )
        length_squared = dx * dx + dy * dy
        a = (ux * dx + uy * dy) / length_squared
        b = (uy * dx - ux * dy) / length_squared
        mapping = [
            a,
            b,
            -b,
            a,
            baseline.start[0] - a * start[0] + b * start[1],
            baseline.start[1] - b * start[0] - a * start[1],
        ]
        for kind in ("rembg", "edits"):
            if target[kind].exists():
                shutil.copyfile(
                    target[kind], target["directory"] / f"revision-{kind}.png"
                )
    else:
        for kind in (
            "source",
            "rembg",
            "edits",
            "alternative",
            "mask",
            "cutout",
        ):
            target[kind].unlink(missing_ok=True)
        svg = ET.parse(saved_svg).getroot()
        x, y, width, height = map(float, svg.get("viewBox").split())
        baseline = svg_main_length(saved_svg)
        maximum_x = max(x + width, baseline.start[0], baseline.end[0])
        maximum_y = max(y + height, baseline.start[1], baseline.end[1])
        x = min(x, baseline.start[0], baseline.end[0])
        y = min(y, baseline.start[1], baseline.end[1])
        width, height = maximum_x - x, maximum_y - y
        scale = 1200 / max(width, height)
        padding = 40
        canvas_width, canvas_height = (
            math.ceil(width * scale) + padding * 2,
            math.ceil(height * scale) + padding * 2,
        )
        mapping = [
            1 / scale,
            0,
            0,
            1 / scale,
            x - padding / scale,
            y - padding / scale,
        ]
        state.main_length = MainLength(
            start=(
                (baseline.start[0] - x) * scale + padding,
                (baseline.start[1] - y) * scale + padding,
            ),
            end=(
                (baseline.end[0] - x) * scale + padding,
                (baseline.end[1] - y) * scale + padding,
            ),
        )
        raw["svg_canvas"] = {"width": canvas_width, "height": canvas_height}
        svg.set(
            "viewBox",
            f"{x - padding / scale} {y - padding / scale} {canvas_width / scale} {canvas_height / scale}",
        )
        svg.set("width", str(canvas_width))
        svg.set("height", str(canvas_height))
        # A silhouette only: never burn the scaling vector into the mask.
        for element in list(svg):
            if element.get("id") == "main-length":
                svg.remove(element)
        atomic_bytes(target["directory"] / "revision-mask.svg", ET.tostring(svg))
    if reference.is_file():
        atomic_bytes(target["directory"] / "revision-photo.png", reference.read_bytes())
    atomic_json(
        target["directory"] / "revision-outline.json",
        {
            "mapping": mapping,
            "main_length": state.main_length.model_dump(mode="json"),
            "alpha_threshold": state.alpha_threshold,
        },
    )
    return raw


def export_saved_svg(target: dict, state: ReviewState) -> bool:
    """Keep SVG paths exact unless the painted mask actually changed."""
    directory = target["directory"]
    baseline_path = directory / "revision-outline.json"
    if not baseline_path.is_file():
        return False
    baseline = json.loads(baseline_path.read_text())
    if target["rembg"].is_file():
        original_rembg = directory / "revision-rembg.png"
        original_edits = directory / "revision-edits.png"
        before = final_mask(
            original_rembg if original_rembg.is_file() else target["rembg"],
            original_edits,
            baseline["alpha_threshold"],
        )
        after = final_mask(target["rembg"], target["edits"], state.alpha_threshold)
        if not np.array_equal(before, after):
            return False
    original = directory / "revision.svg"
    if state.main_length.model_dump(mode="json") == baseline["main_length"]:
        atomic_bytes(target["svg"], original.read_bytes())
        return True
    root = ET.parse(original).getroot()
    a, b, c, d, tx, ty = baseline["mapping"]

    def svg_point(point):
        x, y = point
        return a * x + c * y + tx, b * x + d * y + ty

    start, end = svg_point(state.main_length.start), svg_point(state.main_length.end)
    dx, dy = end[0] - start[0], end[1] - start[1]
    squared = dx * dx + dy * dy
    a, b, c, d = -dy / squared, -dx / squared, dx / squared, -dy / squared
    x, y, width, height = map(float, root.get("viewBox").split())
    corners = [
        (a * px + c * py, b * px + d * py)
        for px, py in [(x, y), (x + width, y), (x, y + height), (x + width, y + height)]
    ]
    tx, ty = -min(p[0] for p in corners), -min(p[1] for p in corners)
    group = ET.Element(
        f"{{{SVG_NAMESPACE}}}g",
        {
            "id": "revision-orientation",
            "transform": f"matrix({' '.join(svg_number(v) for v in (a, b, c, d, tx, ty))})",
        },
    )
    for child in list(root):
        root.remove(child)
        if child.get("id") == "revision-orientation":
            old_a, old_b, old_c, old_d, old_tx, old_ty = map(
                float, child.get("transform")[7:-1].split()
            )
            combined = (
                a * old_a + c * old_b,
                b * old_a + d * old_b,
                a * old_c + c * old_d,
                b * old_c + d * old_d,
                a * old_tx + c * old_ty + tx,
                b * old_tx + d * old_ty + ty,
            )
            group.set(
                "transform",
                f"matrix({' '.join(svg_number(value) for value in combined)})",
            )
            group.extend(list(child))
        elif child.get("id") != "main-length":
            group.append(child)
    root.append(group)
    ET.SubElement(
        root,
        f"{{{SVG_NAMESPACE}}}line",
        {
            "id": "main-length",
            "data-role": "main-length",
            "display": "none",
            "x1": svg_number(a * start[0] + c * start[1] + tx),
            "y1": svg_number(b * start[0] + d * start[1] + ty),
            "x2": svg_number(a * end[0] + c * end[1] + tx),
            "y2": svg_number(b * end[0] + d * end[1] + ty),
        },
    )
    temporary = directory / ".outline.svg.tmp"
    write_svg(
        root,
        temporary,
        max(p[0] for p in corners) + tx,
        max(p[1] for p in corners) + ty,
        ALIGNED_SVG_LONGEST_SIDE,
    )
    temporary.replace(target["svg"])
    return True
