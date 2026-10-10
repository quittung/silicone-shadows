"""Existing PNG transparency survives import, masking and public processing."""

import json
import sys
import tempfile
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from server import create_app
from server.artifacts import normalize_source
from server.hosted import HostedStore
from server.routes.public import _decode_source
from tests.test_review import png_bytes


class ImageImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = self.root / "images"
        self.images.mkdir()
        pixels = np.zeros((24, 32, 4), dtype=np.uint8)
        pixels[4:20, 8:24] = (0, 0, 0, 255)
        pixels[4:20, 7] = (50, 60, 70, 100)  # Preserve the soft alpha edge.
        pixels[10:14, 14:18] = 0  # Preserve an interior hole.
        self.source = Image.fromarray(pixels)
        self.source.save(self.images / "sample.png")
        self.app = create_app(self.images, self.root / "work")
        self.addCleanup(self.app.state.comparison.close)
        self.client = TestClient(self.app)
        self.rembg = types.ModuleType("rembg")
        self.rembg.new_session = Mock(return_value=object())
        self.rembg.remove = Mock(side_effect=lambda data, session: data)

    def assert_rgba(self, path, expected):
        with Image.open(path) as image:
            self.assertEqual(image.mode, "RGBA")
            np.testing.assert_array_equal(np.asarray(image), np.asarray(expected))

    def test_initial_source_and_alternative_keep_alpha_and_skip_rembg(self):
        with patch.dict(sys.modules, {"rembg": self.rembg}):
            response = self.client.post("/api/items/sample/prepare")
            self.assertEqual(response.status_code, 200, response.text)
            self.assert_rgba(self.root / "work/sample/source.png", self.source)
            self.assert_rgba(self.root / "work/sample/rembg.png", self.source)
            alternative = self.source.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            response = self.client.post(
                "/api/items/sample/alternative",
                files={"image": ("cutout.png", png_bytes(alternative), "image/png")},
            )
            self.assertEqual(response.status_code, 200, response.text)
            for name in ["alternative", "source", "rembg"]:
                self.assert_rgba(self.root / f"work/sample/{name}.png", alternative)
            # Existing tracing uses the imported alpha, including its interior hole.
            response = self.client.post(
                "/api/items/sample/save",
                data={
                    "state_json": json.dumps(
                        {
                            "status": "done",
                            "rating": "good",
                            "main_length": {"start": [16, 19], "end": [16, 4]},
                            "alpha_threshold": 128,
                        }
                    )
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
        with Image.open(self.root / "work/sample/mask.png") as mask:
            np.testing.assert_array_equal(
                np.asarray(mask) > 0, np.asarray(alternative.getchannel("A")) >= 128
            )
        self.rembg.new_session.assert_not_called()
        self.rembg.remove.assert_not_called()

    def test_opaque_rgba_still_runs_background_removal(self):
        opaque = Image.new("RGBA", (20, 16), (80, 90, 100, 255))
        with patch.dict(sys.modules, {"rembg": self.rembg}):
            response = self.client.post(
                "/api/items/sample/alternative",
                files={"image": ("opaque.png", png_bytes(opaque), "image/png")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.rembg.new_session.assert_called_once()
        self.rembg.remove.assert_called_once()

    def test_palette_transparency_is_preserved(self):
        indexed = Image.new("P", (8, 8), 0)
        indexed.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
        indexed.paste(1, (2, 2, 6, 6))
        indexed.info["transparency"] = 0
        data = png_bytes(indexed)
        with patch.dict(sys.modules, {"rembg": self.rembg}):
            response = self.client.post(
                "/api/items/sample/alternative",
                files={"image": ("indexed.png", data, "image/png")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_rgba(self.root / "work/sample/rembg.png", indexed.convert("RGBA"))
        self.rembg.remove.assert_not_called()

    def test_source_decoder_orients_without_losing_alpha(self):
        source = self.source.copy()
        source.getexif()[274] = 6
        encoded = BytesIO()
        source.save(encoded, format="PNG", exif=source.getexif())
        decoded = _decode_source(encoded.getvalue())
        np.testing.assert_array_equal(
            np.asarray(decoded),
            np.asarray(self.source.transpose(Image.Transpose.ROTATE_270)),
        )
        self.assertEqual(normalize_source(Image.new("RGB", (2, 2))).mode, "RGB")

    def test_explicit_redetection_uses_light_opaque_background(self):
        with patch.dict(sys.modules, {"rembg": self.rembg}):
            self.client.post("/api/items/sample/prepare")
            response = self.client.post(
                "/api/items/sample/remask-crop",
                data={"left": 0, "top": 0, "right": 32, "bottom": 24},
            )
        self.assertEqual(response.status_code, 200, response.text)
        data = self.rembg.remove.call_args.args[0]
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.getpixel((0, 0)), (236, 236, 232))
            self.assertEqual(image.getpixel((8, 4)), (0, 0, 0))

    def test_public_processing_keeps_alpha_without_loading_model(self):
        store = HostedStore(self.root / "state.sqlite3")
        app = create_app(
            self.images,
            self.root / "public-work",
            dataset_dir=self.root / "dataset",
            hosted_store=store,
            pending_dir=self.root / "pending",
            secure_cookies=False,
        )
        self.addCleanup(app.state.comparison.close)
        client = TestClient(app)
        ticket = client.post("/api/public/queue").json()["ticket"]
        client.post("/api/public/queue/status", json={"ticket": ticket})
        with patch.dict(sys.modules, {"rembg": self.rembg}):
            response = client.post(
                "/api/public/rembg",
                data={"ticket": ticket},
                files={"image": ("cutout.png", png_bytes(self.source), "image/png")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("x-archive-token", response.headers)
        with Image.open(BytesIO(response.content)) as image:
            np.testing.assert_array_equal(np.asarray(image), np.asarray(self.source))
        np.testing.assert_array_equal(
            np.asarray(_decode_source(png_bytes(self.source))), np.asarray(self.source)
        )
        self.rembg.new_session.assert_not_called()
        self.rembg.remove.assert_not_called()
