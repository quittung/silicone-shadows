import json
import hashlib
import ssl
import sys
import tempfile
import time
import types
import unittest
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from server import create_app, ensure_catalog
from server.catalog import ROOT_YE_CERTIFICATE, ssl_context_for


def png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class ReviewAppTest(unittest.TestCase):
    def test_toybox_uses_verified_pinned_root(self) -> None:
        context = ssl_context_for("https://fantasytoybox.net/data/products.json")
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        certificate = ssl.PEM_cert_to_DER_cert(ROOT_YE_CERTIFICATE.read_text())
        self.assertEqual(
            hashlib.sha256(certificate).hexdigest(),
            "e14ffcad5b0025731006caa43a121a22d8e9700f4fb9cf852f02a708aa5d5666",
        )

    def test_catalog_source_is_downloaded_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            descriptor = root / "catalog_source.json"
            descriptor.write_text(
                json.dumps(
                    {
                        "version": 7,
                        "url_template": "https://catalog.example/products_v{version}.json",
                    }
                )
            )
            catalog_data = b'[{"id": 1}]'

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return None

                def geturl(self):
                    return "https://catalog.example/products_v7.json"

                def read(self, _size=-1):
                    return catalog_data

            with patch("server.catalog.urlopen", return_value=Response()) as download:
                catalog_path = ensure_catalog(descriptor)
                self.assertEqual(
                    catalog_path,
                    root / ".local/catalog/products_v7.json",
                )
                self.assertEqual(catalog_path.read_bytes(), catalog_data)
                self.assertEqual(ensure_catalog(descriptor), catalog_path)
                self.assertEqual(download.call_count, 1)

    def test_catalog_images_are_downloaded_and_masked_by_prefetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "in"
            work_dir = root / "work"
            input_dir.mkdir()
            catalog_path = root / "products.json"
            catalog_path.write_text(
                json.dumps(
                    [
                        {
                            "id": index,
                            "n": f"Item {index}",
                            "vn": "Vendor",
                            "pt": "Type",
                            "pic": f"images/item-{index:02}.png",
                        }
                        for index in range(12)
                    ]
                    + [
                        {
                            "id": 12,
                            "n": "Shared A",
                            "vn": "Vendor",
                            "pt": "Type",
                            "pic": "images/a/shared.png",
                        },
                        {
                            "id": 13,
                            "n": "Shared B",
                            "vn": "Vendor",
                            "pt": "Type",
                            "pic": "images/b/shared.png",
                        },
                    ]
                )
            )
            image_data = png_bytes(Image.new("RGB", (16, 12), "white"))

            class Response:
                def __init__(self, request):
                    self.url = request.full_url

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return None

                def geturl(self):
                    return self.url

                def read(self, _size=-1):
                    return image_data

            fake_rembg = types.ModuleType("rembg")
            fake_rembg.new_session = lambda: object()
            fake_rembg.remove = lambda data, session: data
            with (
                patch(
                    "server.workspace.urlopen",
                    side_effect=lambda request, **_: Response(request),
                ),
                patch.dict(sys.modules, {"rembg": fake_rembg}),
                TestClient(
                    create_app(
                        input_dir,
                        work_dir,
                        catalog_path,
                        "https://images.example/",
                    )
                ) as client,
            ):
                items = client.get("/api/items").json()
                self.assertEqual(items["total"], 14)
                self.assertIn("shared--a", {item["id"] for item in items["items"]})
                self.assertIn("shared--b", {item["id"] for item in items["items"]})
                deadline = time.monotonic() + 3
                while len(list(work_dir.glob("*/rembg.png"))) < 10:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                self.assertEqual(len(list(input_dir.glob("*.png"))), 10)

    def test_mask_prefetch_refills_from_two_to_ten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "in"
            work_dir = root / "work"
            input_dir.mkdir()
            for index in range(12):
                Image.new("RGB", (16, 12), "white").save(
                    input_dir / f"item-{index:02}.png"
                )

            calls = []

            def fake_remove(data: bytes, session: object) -> bytes:
                calls.append(session)
                with Image.open(BytesIO(data)) as image:
                    return png_bytes(image.convert("RGBA"))

            fake_rembg = types.ModuleType("rembg")
            fake_rembg.new_session = lambda: object()
            fake_rembg.remove = fake_remove
            with patch.dict(sys.modules, {"rembg": fake_rembg}):
                with TestClient(create_app(input_dir, work_dir)) as client:
                    deadline = time.monotonic() + 3
                    while len(list(work_dir.glob("*/rembg.png"))) < 10:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    self.assertEqual(len(list(work_dir.glob("*/rembg.png"))), 10)

                    for index in range(9):
                        metadata = work_dir / f"item-{index:02}" / "metadata.json"
                        metadata.write_text(
                            json.dumps(
                                {
                                    "status": "done",
                                    "rating": "unusable",
                                    "alpha_threshold": 128,
                                    "main_length": None,
                                }
                            )
                        )
                    response = client.post(
                        "/api/prefetch", json={"item_ids": ["item-11"]}
                    )
                    self.assertEqual(response.status_code, 200, response.text)

                    deadline = time.monotonic() + 3
                    while not (work_dir / "item-11" / "rembg.png").exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    self.assertFalse((work_dir / "item-10" / "rembg.png").exists())
                    self.assertEqual(len(calls), 11)
                    self.assertEqual(
                        client.post(
                            "/api/prefetch", json={"item_ids": ["missing"]}
                        ).status_code,
                        400,
                    )

    def test_review_export_and_unusable_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "in"
            work_dir = root / "work"
            item_dir = work_dir / "sample"
            input_dir.mkdir()
            item_dir.mkdir(parents=True)

            source = Image.new("RGB", (64, 48), "white")
            source.save(input_dir / "sample.jpg")
            source.save(item_dir / "source.png")

            alpha = np.zeros((48, 64), dtype=np.uint8)
            alpha[8:40, 12:52] = 255
            alpha[20:28, 26:38] = 0
            alpha[2:5, 2:5] = 255
            rgba = np.zeros((48, 64, 4), dtype=np.uint8)
            rgba[..., :3] = 120
            rgba[..., 3] = alpha
            Image.fromarray(rgba).save(item_dir / "rembg.png")

            catalog_path = root / "products.json"
            catalog_path.write_text(
                json.dumps(
                    [
                        {
                            "id": 1,
                            "n": "Sample",
                            "vn": "Vendor A",
                            "pt": "Type A",
                            "pic": "images/sample.jpg",
                            "sz": {
                                "s": [
                                    {"sl": "Small", "ShortLabel": "S", "len": 5},
                                    {"sl": "Large", "ShortLabel": "L", "len": 8},
                                ]
                            },
                        },
                        {
                            "id": 2,
                            "n": "Sample Legacy",
                            "vn": "Vendor A",
                            "pt": "Type A",
                            "pic": "images/sample.jpg",
                            "sz": {
                                "s": [{"sl": "One size", "ShortLabel": "OS", "len": 6}]
                            },
                        },
                        {
                            "id": 3,
                            "n": "Missing",
                            "vn": "Vendor B",
                            "pt": "Type B",
                            "pic": "images/missing.jpg",
                            "sz": {
                                "s": [{"sl": "Medium", "ShortLabel": "M", "len": 7}]
                            },
                        },
                    ]
                )
            )
            dataset_dir = root / "dataset"
            app = create_app(
                input_dir,
                work_dir,
                catalog_path,
                dataset_dir=dataset_dir,
            )
            with TestClient(app) as client:
                listing = client.get("/api/items").json()
                self.assertEqual(listing["total"], 1)
                self.assertEqual(listing["done"], 0)
                self.assertEqual(len(listing["items"][0]["products"]), 2)
                self.assertIn("canvas", client.get("/").text)
                self.assertEqual(client.get("/static/review.css").status_code, 200)
                self.assertEqual(client.get("/static/review.js").status_code, 200)
                self.assertEqual(client.get("/stats").status_code, 200)
                self.assertEqual(client.get("/compare").status_code, 200)

                prepared = client.post("/api/items/sample/prepare").json()
                self.assertEqual((prepared["width"], prepared["height"]), (64, 48))
                missing_line = client.post(
                    "/api/items/sample/save",
                    data={
                        "state_json": json.dumps(
                            {
                                "status": "done",
                                "rating": "bad_perspective",
                                "alpha_threshold": 128,
                                "main_length": None,
                            }
                        )
                    },
                )
                self.assertEqual(missing_line.status_code, 400)

                edits = np.zeros((48, 64, 4), dtype=np.uint8)
                edits[10:13, 15:18] = (0, 0, 0, 255)
                edits[1:4, 58:61] = (255, 255, 255, 255)
                state = {
                    "status": "done",
                    "rating": "good",
                    "alpha_threshold": 128,
                    "main_length": {"start": [15, 35], "end": [48, 10]},
                }
                response = client.post(
                    "/api/items/sample/save",
                    data={"state_json": json.dumps(state)},
                    files={
                        "edits": (
                            "edits.png",
                            png_bytes(Image.fromarray(edits)),
                            "image/png",
                        )
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)

                with Image.open(item_dir / "mask.png") as image:
                    mask = np.asarray(image)
                self.assertEqual(mask[3, 3], 0)
                self.assertEqual(mask[2, 59], 0)
                self.assertEqual(mask[11, 16], 0)
                self.assertEqual(mask[10, 20], 255)

                svg = ET.parse(item_dir / "outline.svg").getroot()
                namespace = {"svg": "http://www.w3.org/2000/svg"}
                self.assertIsNotNone(svg.find(".//svg:path[@id='outline']", namespace))
                line = svg.find(".//svg:line[@id='main-length']", namespace)
                self.assertIsNotNone(line)
                self.assertEqual(line.attrib["display"], "none")
                self.assertAlmostEqual(
                    float(line.attrib["x1"]), float(line.attrib["x2"])
                )
                self.assertAlmostEqual(
                    float(line.attrib["y1"]) - float(line.attrib["y2"]), 1
                )
                _, _, view_width, view_height = map(
                    float, svg.attrib["viewBox"].split()
                )
                self.assertEqual(
                    max(float(svg.attrib["width"]), float(svg.attrib["height"])),
                    1000,
                )
                self.assertAlmostEqual(
                    float(svg.attrib["width"]) / float(svg.attrib["height"]),
                    view_width / view_height,
                )

                metadata = json.loads((item_dir / "metadata.json").read_text())
                self.assertEqual(metadata["rating"], "good")
                published = [
                    dataset_dir / "vendor-a/type-a/sample",
                    dataset_dir / "vendor-a/type-a/sample-legacy",
                ]
                for index, directory in enumerate(published, start=1):
                    record = json.loads((directory / "metadata.json").read_text())
                    self.assertEqual(
                        set(record),
                        {
                            "schema_version",
                            "catalog_id",
                            "quality",
                            "source",
                        },
                    )
                    self.assertEqual(record["catalog_id"], index)
                    self.assertEqual(record["quality"], "good")
                    self.assertEqual(record["source"], "catalog")
                    self.assertEqual(
                        (directory / "outline.svg").read_bytes(),
                        (item_dir / "outline.svg").read_bytes(),
                    )
                self.assertEqual(client.get("/api/items").json()["done"], 1)

                stats = client.get("/api/stats").json()
                self.assertEqual(stats["summary"]["products"], 3)
                self.assertEqual(stats["summary"]["reviewed"], 2)
                self.assertEqual(stats["summary"]["pending"], 1)
                self.assertEqual(stats["summary"]["good"], 2)
                self.assertEqual(stats["summary"]["comparable"], 2)
                self.assertNotIn("missing_products", stats)

                comparison = client.get("/api/comparison/products").json()["products"]
                self.assertEqual(len(comparison), 2)
                self.assertEqual(
                    [size["label"] for size in comparison[0]["sizes"]], ["S", "L"]
                )
                comparison_line = comparison[0]["main_length"]
                self.assertAlmostEqual(
                    comparison_line["start"][0], comparison_line["end"][0]
                )
                self.assertAlmostEqual(
                    comparison_line["start"][1] - comparison_line["end"][1], 1
                )

                fresh_work = root / "fresh-work"
                with TestClient(
                    create_app(
                        input_dir,
                        fresh_work,
                        catalog_path,
                        dataset_dir=dataset_dir,
                    )
                ) as fresh_client:
                    fresh_item = fresh_client.get("/api/items").json()["items"][0]
                    self.assertEqual(fresh_item["status"], "done")
                    self.assertTrue(fresh_item["published"])
                    self.assertEqual(fresh_item["provenance"], "catalog")
                    self.assertEqual(
                        fresh_client.post("/api/items/sample/prepare").status_code,
                        409,
                    )
                    fresh_comparison = fresh_client.get(
                        "/api/comparison/products"
                    ).json()["products"]
                    self.assertEqual(len(fresh_comparison), 2)
                    self.assertEqual(
                        fresh_client.get(fresh_comparison[0]["svg_url"]).status_code,
                        200,
                    )
                    restarted = fresh_client.post("/api/items/sample/rereview")
                    self.assertEqual(restarted.status_code, 200, restarted.text)
                    self.assertEqual(restarted.json()["status"], "pending")
                    self.assertFalse(restarted.json()["published"])
                    for directory in published:
                        self.assertTrue((directory / "metadata.json").exists())
                    self.assertEqual(
                        fresh_client.get("/api/stats").json()["summary"]["reviewed"],
                        2,
                    )

                unusable = {
                    "status": "done",
                    "rating": "unusable",
                    "alpha_threshold": 128,
                    "main_length": None,
                }
                response = client.post(
                    "/api/items/sample/save",
                    data={"state_json": json.dumps(unusable)},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse((item_dir / "mask.png").exists())
                self.assertFalse((item_dir / "cutout.png").exists())
                self.assertFalse((item_dir / "outline.svg").exists())
                for directory in published:
                    record = json.loads((directory / "metadata.json").read_text())
                    self.assertEqual(record["quality"], "unusable")
                    self.assertNotIn("main_length", record)
                    self.assertFalse((directory / "outline.svg").exists())

                alternative = Image.new("RGB", (32, 24), "gray")

                def fake_remove(data: bytes, session: object) -> bytes:
                    with Image.open(BytesIO(data)) as image:
                        return png_bytes(image.convert("RGBA"))

                fake_rembg = types.ModuleType("rembg")
                fake_rembg.new_session = lambda: object()
                fake_rembg.remove = fake_remove
                with patch.dict(sys.modules, {"rembg": fake_rembg}):
                    response = client.post(
                        "/api/items/sample/alternative",
                        files={
                            "image": (
                                "alternative.jpg",
                                png_bytes(alternative),
                                "image/png",
                            )
                        },
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    (response.json()["width"], response.json()["height"]), (32, 24)
                )
                self.assertTrue((item_dir / "alternative.png").exists())
                self.assertTrue((item_dir / "rembg.png").exists())
                metadata = json.loads((item_dir / "metadata.json").read_text())
                self.assertEqual(metadata["status"], "pending")
                self.assertIsNone(metadata["rating"])
                self.assertIsNone(metadata["main_length"])
                self.assertTrue(metadata["re_review"])
                for directory in published:
                    record = json.loads((directory / "metadata.json").read_text())
                    self.assertEqual(record["quality"], "unusable")
                    self.assertEqual(record["source"], "catalog")
                self.assertTrue(
                    client.get("/api/items").json()["items"][0]["has_alternative"]
                )

                response = client.post(
                    "/api/items/sample/save",
                    data={"state_json": json.dumps(unusable)},
                )
                self.assertEqual(response.status_code, 200, response.text)
                for directory in published:
                    record = json.loads((directory / "metadata.json").read_text())
                    self.assertEqual(record["source"], "alternative")
                self.assertEqual(response.json()["provenance"], "alternative")

                with patch.dict(sys.modules, {"rembg": fake_rembg}):
                    response = client.delete("/api/items/sample/alternative")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    (response.json()["width"], response.json()["height"]), (64, 48)
                )
                self.assertFalse((item_dir / "alternative.png").exists())
                metadata = json.loads((item_dir / "metadata.json").read_text())
                self.assertEqual(metadata["status"], "pending")
                self.assertIsNone(metadata["rating"])
                self.assertFalse(
                    client.get("/api/items").json()["items"][0]["has_alternative"]
                )


if __name__ == "__main__":
    unittest.main()
