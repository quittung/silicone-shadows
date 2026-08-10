#!/usr/bin/env python3
"""Convert a rembg alpha image or binary mask into one SVG object."""

from __future__ import annotations

import argparse
import math
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

MASK_SUFFIXES = {".bmp", ".png", ".tif", ".tiff", ".webp"}
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
ALIGNED_SVG_LONGEST_SIDE = 1000


def load_foreground(path: Path, threshold: int) -> np.ndarray:
    with Image.open(path) as image:
        image.load()
        if "A" in image.getbands():
            alpha = np.asarray(image.getchannel("A"), dtype=np.uint8)
        elif image.mode in {"1", "L"}:
            alpha = np.asarray(image.convert("L"), dtype=np.uint8)
        elif "transparency" in image.info:
            alpha = np.asarray(image.convert("RGBA").getchannel("A"), dtype=np.uint8)
        else:
            raise ValueError(
                f"{path} has neither an alpha channel nor a grayscale mask"
            )

    return alpha >= threshold


def largest_component(mask: np.ndarray) -> np.ndarray:
    labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=np.uint8))
    if count == 0:
        raise ValueError("the alpha threshold removed all foreground pixels")

    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    return labels == sizes.argmax()


def normalize_mask(
    mask: np.ndarray, longest_side: float = 100
) -> tuple[np.ndarray, int, int, float]:
    y, x = np.nonzero(mask)
    if not len(x):
        raise ValueError("the mask contains no foreground pixels")
    left, top = int(x.min()), int(y.min())
    cropped = mask[top : int(y.max()) + 1, left : int(x.max()) + 1]
    scale = longest_side / max(cropped.shape)
    return cropped, left, top, scale


def traced_root(mask: np.ndarray, width: float, height: float) -> ET.Element:
    with tempfile.TemporaryDirectory() as temp_dir:
        pbm_path = Path(temp_dir) / "mask.pbm"
        svg_path = Path(temp_dir) / "outline.svg"
        # Potrace traces black pixels.
        Image.fromarray(np.where(mask, 0, 255).astype(np.uint8)).convert("1").save(
            pbm_path
        )
        subprocess.run(
            [
                "potrace",
                "--svg",
                "--flat",
                "--turdsize",
                "0",
                "--width",
                f"{width:.12g}pt",
                "--height",
                f"{height:.12g}pt",
                "--output",
                str(svg_path),
                str(pbm_path),
            ],
            check=True,
        )
        return ET.parse(svg_path).getroot()


def svg_number(value: float) -> str:
    return "0" if abs(value) < 1e-12 else f"{value:.12g}"


def write_svg(
    root: ET.Element,
    output: Path,
    width: float,
    height: float,
    intrinsic_longest_side: float | None = None,
) -> None:
    width_text, height_text = svg_number(width), svg_number(height)
    intrinsic_scale = (
        intrinsic_longest_side / max(width, height) if intrinsic_longest_side else 1
    )
    root.set("width", svg_number(width * intrinsic_scale))
    root.set("height", svg_number(height * intrinsic_scale))
    root.set("viewBox", f"0 0 {width_text} {height_text}")
    path = root.find(f".//{{{SVG_NAMESPACE}}}path")
    if path is None:
        raise ValueError("Potrace produced an SVG without a path")
    path.set("id", "outline")
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.register_namespace("", SVG_NAMESPACE)
    ET.ElementTree(root).write(output, encoding="unicode", xml_declaration=True)


def trace_svg(mask: np.ndarray, output: Path) -> tuple[int, int, float]:
    mask, left, top, scale = normalize_mask(mask)
    height, width = mask.shape
    svg_width, svg_height = width * scale, height * scale
    write_svg(traced_root(mask, svg_width, svg_height), output, svg_width, svg_height)
    return left, top, scale


def trace_aligned_svg(
    mask: np.ndarray,
    output: Path,
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Trace, rotate base-to-tip upward, and make that vector one SVG unit."""
    mask, left, top, _ = normalize_mask(mask)
    height, width = mask.shape
    start = (start[0] - left, start[1] - top)
    end = (end[0] - left, end[1] - top)
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if not math.isfinite(length_squared) or length_squared < 1:
        raise ValueError("main-length endpoints are too close together")

    # SVG y increases downward. This matrix maps end-start to exactly (0, -1).
    a, c = -dy / length_squared, dx / length_squared
    b, d = -dx / length_squared, -dy / length_squared
    y, x = np.nonzero(mask)
    transformed_x = a * x + c * y
    transformed_y = b * x + d * y
    minimum_x = float(transformed_x.min() + min(0, a) + min(0, c))
    maximum_x = float(transformed_x.max() + max(0, a) + max(0, c))
    minimum_y = float(transformed_y.min() + min(0, b) + min(0, d))
    maximum_y = float(transformed_y.max() + max(0, b) + max(0, d))
    translate_x, translate_y = -minimum_x, -minimum_y

    root = traced_root(mask, width, height)
    group = root.find(f"{{{SVG_NAMESPACE}}}g")
    if group is None:
        raise ValueError("Potrace produced an SVG without a group")
    index = list(root).index(group)
    root.remove(group)
    wrapper = ET.Element(
        f"{{{SVG_NAMESPACE}}}g",
        {
            "id": "canonical-orientation",
            "transform": "matrix({})".format(
                " ".join(
                    svg_number(value)
                    for value in (a, b, c, d, translate_x, translate_y)
                )
            ),
        },
    )
    wrapper.append(group)
    root.insert(index, wrapper)

    def transform(point: tuple[float, float]) -> tuple[float, float]:
        x_value, y_value = point
        return (
            a * x_value + c * y_value + translate_x,
            b * x_value + d * y_value + translate_y,
        )

    aligned_start, aligned_end = transform(start), transform(end)
    ET.SubElement(
        root,
        f"{{{SVG_NAMESPACE}}}line",
        {
            "id": "main-length",
            "data-role": "main-length",
            "x1": svg_number(aligned_start[0]),
            "y1": svg_number(aligned_start[1]),
            "x2": svg_number(aligned_end[0]),
            "y2": svg_number(aligned_end[1]),
            "display": "none",
        },
    )
    write_svg(
        root,
        output,
        maximum_x - minimum_x,
        maximum_y - minimum_y,
        ALIGNED_SVG_LONGEST_SIDE,
    )
    return aligned_start, aligned_end


def outline_file(source: Path, output: Path, threshold: int) -> None:
    mask = largest_component(load_foreground(source, threshold))
    trace_svg(mask, output)


def outline_directory(source: Path, output: Path, threshold: int) -> list[Path]:
    inputs = sorted(
        path
        for path in source.iterdir()
        if path.is_file() and path.suffix.lower() in MASK_SUFFIXES
    )
    if not inputs:
        raise ValueError(f"no supported mask images found in {source}")
    if output.exists() and not output.is_dir():
        raise ValueError(f"directory output is a file: {output}")

    outputs = []
    for input_path in inputs:
        output_path = output / f"{input_path.stem}.svg"
        try:
            outline_file(input_path, output_path, threshold)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            raise ValueError(f"{input_path}: {error}") from error
        outputs.append(output_path)
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Threshold a rembg alpha channel and trace its largest object."
    )
    parser.add_argument(
        "input", type=Path, help="RGBA rembg output, grayscale mask, or mask directory"
    )
    parser.add_argument(
        "output", nargs="?", type=Path, help="output SVG or directory"
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=128,
        metavar="0-255",
        help="minimum alpha included in the outline (default: 128)",
    )
    args = parser.parse_args()

    if not 1 <= args.threshold <= 255:
        parser.error("--threshold must be between 1 and 255")
    if not args.input.exists():
        parser.error(f"input does not exist: {args.input}")

    try:
        if args.input.is_dir():
            output = args.output or args.input.with_name(f"{args.input.name}_svg")
            outputs = outline_directory(args.input, output, args.threshold)
        else:
            output = args.output or args.input.with_suffix(".svg")
            outline_file(args.input, output, args.threshold)
            outputs = [output]
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    for output in outputs:
        print(output)


if __name__ == "__main__":
    main()
