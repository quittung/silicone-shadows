"""Daily catalog discovery and explicit activation of validated snapshots."""

import copy
import json
import threading
import time
from pathlib import Path

from .artifacts import atomic_bytes, atomic_json, read_state
from .catalog import latest_catalog

DAY = 24 * 60 * 60
CATALOG_FIELDS = (
    "catalog_version",
    "catalog",
    "metadata_options",
    "catalog_by_stem",
    "catalog_by_id",
    "catalog_sources",
    "catalog_pics",
    "catalog_item_ids",
    "record_paths",
)


def selected_source(source: Path, state_dir: Path) -> Path:
    """Keep a runtime upgrade across deployments, but accept a newer repo pin."""
    saved = state_dir / "catalog_source.json"
    if not saved.is_file():
        return source
    pinned = json.loads(source.read_text())
    runtime = json.loads(saved.read_text())
    if any(runtime.get(key) != pinned.get(key) for key in ("provider", "url_template")):
        raise ValueError("saved catalog source does not match the configured provider")
    if type(runtime.get("version")) is not int or runtime["version"] <= 0:
        raise ValueError("invalid saved catalog version")
    return saved if runtime["version"] >= pinned["version"] else source


class CatalogUpdates:
    def __init__(self, workspace, source: Path, products_path: Path):
        self.workspace = workspace
        self.config = json.loads(source.read_text())
        self.directory = workspace.work_dir.parent
        self.source = self.directory / "catalog_source.json"
        self.state_path = self.directory / "catalog-check.json"
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.state = {
            "checked_at": None,
            "latest_version": workspace.catalog_version,
            "error": None,
        }
        if self.state_path.is_file():
            try:
                saved = json.loads(self.state_path.read_text())
                if (
                    isinstance(saved, dict)
                    and saved.get("current_version") == workspace.catalog_version
                    and isinstance(saved.get("checked_at"), (int, float))
                    and type(saved.get("latest_version")) is int
                    and saved["latest_version"] >= workspace.catalog_version
                ):
                    self.state.update(saved)
            except (OSError, ValueError):
                pass
        atomic_bytes(self.cache(workspace.catalog_version), products_path.read_bytes())
        # Export the actual loaded version, including a startup cache fallback.
        atomic_json(self.source, {**self.config, "version": workspace.catalog_version})

    def status(self):
        return {**self.state, "current_version": self.workspace.catalog_version}

    def check(self, force=False):
        with self.lock:
            if not force and time.time() - (self.state["checked_at"] or 0) < DAY:
                return self.status()
            try:
                version, _, products = latest_catalog(
                    self.workspace.catalog_version, self.config["url_template"]
                )
                atomic_bytes(self.cache(version), json.dumps(products).encode())
                self.state.update(latest_version=version, error=None)
            except (OSError, ValueError) as error:
                self.state["error"] = str(error)
            self.state["checked_at"] = time.time()
            atomic_json(self.state_path, self.status())
            return self.status()

    def cache(self, version):
        return self.directory / ".local" / "catalog" / f"products_v{version}.json"

    def apply(self):
        with self.lock, self.workspace.session_lock:
            workspace = self.workspace
            version = self.state["latest_version"]
            if version <= workspace.catalog_version:
                raise ValueError("No newer catalog is available. Check for updates first.")
            candidate = copy.copy(workspace)
            try:
                candidate.load_catalog(self.cache(version))
            except (KeyError, TypeError, AttributeError) as error:
                raise ValueError("New catalog contains invalid product data.") from error
            changed = {
                item_id
                for item_id, products in workspace.catalog_by_stem.items()
                if products != candidate.catalog_by_stem.get(item_id)
                and (
                    workspace.catalog_pics[item_id] != candidate.catalog_pics.get(item_id)
                    or {p["id"] for p in products}
                    != {p["id"] for p in candidate.catalog_by_stem.get(item_id, [])}
                )
            }
            pending = set()
            if workspace.hosted_store:
                pending.update(workspace.hosted_store.claims())
                pending.update(row["item_id"] for row in workspace.hosted_store.submissions())
            for item_id in changed:
                paths = workspace.paths(item_id)
                completed = (
                    not workspace.hosted_store
                    and read_state(paths["metadata"]).status == "done"
                    and workspace.published_item(item_id)
                )
                has_work = any(
                    paths[key].exists() for key in ("metadata", "edits", "alternative")
                )
                if item_id in pending or (has_work and not completed):
                    raise ValueError(
                        f"Finish or discard the review for {item_id} before updating; "
                        "its catalog image or product identity changed."
                    )
            for item_id in changed:
                workspace.discard_work(item_id)
                # discard_work can retain masks; these images have changed identity.
                paths = workspace.paths(item_id)
                for key in ("source", "rembg"):
                    paths[key].unlink(missing_ok=True)
                workspace.catalog_sources[item_id].unlink(missing_ok=True)
            atomic_json(self.state_path, {**self.state, "current_version": version})
            atomic_json(self.source, {**self.config, "version": version})
            workspace.__dict__.update(
                {name: getattr(candidate, name) for name in CATALOG_FIELDS}
            )
            known = workspace.queue_items()
            with workspace._active_lock:
                if workspace._active_id not in known:
                    workspace._active_id = None
                if workspace._prefetch_ids is not None:
                    workspace._prefetch_ids = {
                        owner: [item_id for item_id in ids if item_id in known]
                        for owner, ids in workspace._prefetch_ids.items()
                    }
            workspace._prefetch_wake.set()
            return self.status()

    def run(self):
        while not self.stop.is_set():
            try:
                self.check()
            except OSError as error:
                self.state["error"] = str(error)
            self.stop.wait(60)
