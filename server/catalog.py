"""Download and identify products from the pinned catalog."""

import json
import re
import ssl
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


def slug(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"
