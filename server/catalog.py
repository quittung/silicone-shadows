"""Download and identify products from the pinned catalog."""

import json
import re
import ssl
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .artifacts import atomic_bytes

MAX_CATALOG_BYTES = 16 * 1024 * 1024
ROOT_YE_CERTIFICATE = (
    Path(__file__).resolve().parents[1] / "certificates" / "isrg-root-ye.pem"
)


def ssl_context_for(url: str):
    if urlparse(url).hostname != "fantasytoybox.net":
        return None
    # Fantasy Toybox omits the Root YE link needed by OpenSSL. Add the official
    # ISRG root while retaining normal certificate and hostname verification.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=ROOT_YE_CERTIFICATE)
    return context


def validate_catalog(data: bytes) -> None:
    if len(data) > MAX_CATALOG_BYTES:
        raise ValueError("catalog is too large")
    try:
        catalog = json.loads(data)
    except json.JSONDecodeError as error:
        raise ValueError("downloaded catalog is not JSON") from error
    if not isinstance(catalog, list):
        raise ValueError("downloaded catalog is not a product list")


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

    try:
        request = Request(url, headers={"User-Agent": "Batch Outliner/1.0"})
        with urlopen(request, timeout=30, context=ssl_context_for(url)) as response:
            if urlparse(response.geturl()).hostname != urlparse(url).hostname:
                raise ValueError("catalog redirected to another host")
            data = response.read(MAX_CATALOG_BYTES + 1)
        validate_catalog(data)
    except (OSError, ValueError) as error:
        fallbacks = sorted(
            (
                path
                for path in cache.parent.glob("products_v*.json")
                if path.stem.removeprefix("products_v").isdigit()
            ),
            key=lambda path: int(path.stem.removeprefix("products_v")),
            reverse=True,
        )
        for fallback in fallbacks:
            try:
                validate_catalog(fallback.read_bytes())
            except (OSError, ValueError):
                continue
            print(
                f"WARNING: Catalog v{version} unavailable ({error}); "
                f"using cached {fallback.name}",
                file=sys.stderr,
                flush=True,
            )
            return fallback
        raise
    atomic_bytes(cache, data)
    print(f"Downloaded catalog v{version}: {cache}")
    return cache


def slug(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"
