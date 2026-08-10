import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

from outline import (
    largest_component,
    load_foreground,
    outline_directory,
    trace_aligned_svg,
    trace_svg,
)


class OutlineTest(unittest.TestCase):
    def test_threshold_orphans_holes_and_single_svg_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source.png"
            output = temp / "output.svg"

            alpha = np.zeros((48, 64), dtype=np.uint8)
            alpha[8:40, 12:52] = 255
            alpha[20:28, 26:38] = 0  # A legitimate hole in the main object.
            alpha[2:5, 2:5] = 255  # An orphan that must be discarded.
            alpha[8:40, 52:55] = 100  # An ambiguous edge below threshold.
            rgba = np.zeros((48, 64, 4), dtype=np.uint8)
            rgba[..., 3] = alpha
            Image.fromarray(rgba).save(source)

            mask = largest_component(load_foreground(source, 128))
            self.assertTrue(mask[10, 15])
            self.assertFalse(mask[3, 3])
            self.assertFalse(mask[22, 30])
            self.assertFalse(mask[10, 53])

            transform = trace_svg(mask, output)
            root = ET.parse(output).getroot()
            paths = root.findall(".//{http://www.w3.org/2000/svg}path")
            self.assertEqual(transform, (12, 8, 2.5))
            self.assertEqual(root.attrib["width"], "100")
            self.assertEqual(root.attrib["height"], "80")
            self.assertEqual(root.attrib["viewBox"], "0 0 100 80")
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].attrib["id"], "outline")

            aligned_output = temp / "aligned.svg"
            trace_aligned_svg(mask, aligned_output, (15, 35), (48, 10))
            aligned = ET.parse(aligned_output).getroot()
            line = aligned.find(
                ".//{http://www.w3.org/2000/svg}line[@id='main-length']"
            )
            self.assertIsNotNone(
                aligned.find(
                    ".//{http://www.w3.org/2000/svg}g[@id='canonical-orientation']"
                )
            )
            self.assertAlmostEqual(float(line.attrib["x1"]), float(line.attrib["x2"]))
            self.assertAlmostEqual(float(line.attrib["y1"]) - float(line.attrib["y2"]), 1)
            _, _, view_width, view_height = map(float, aligned.attrib["viewBox"].split())
            self.assertEqual(
                max(float(aligned.attrib["width"]), float(aligned.attrib["height"])),
                1000,
            )
            self.assertAlmostEqual(
                float(aligned.attrib["width"]) / float(aligned.attrib["height"]),
                view_width / view_height,
            )

            mask_dir = temp / "masks"
            svg_dir = temp / "svg"
            mask_dir.mkdir()
            Image.fromarray(rgba).save(mask_dir / "one.png")
            Image.fromarray(rgba).save(mask_dir / "two.png")
            outputs = outline_directory(mask_dir, svg_dir, 128)
            self.assertEqual([path.name for path in outputs], ["one.svg", "two.svg"])
            self.assertTrue(all(path.is_file() for path in outputs))


if __name__ == "__main__":
    unittest.main()
