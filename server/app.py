"""Compose the FastAPI application from its route groups."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .hosted import HostedStore
from .routes import moderation, pages, public, reviews
from .workspace import Workspace


def create_app(
    input_dir: Path,
    work_dir: Path,
    products_path: Path | None = None,
    image_base_url: str | None = None,
    dataset_dir: Path | None = None,
    hosted_store: HostedStore | None = None,
    pending_dir: Path | None = None,
    secure_cookies: bool = True,
    trusted_hosts: list[str] | None = None,
) -> FastAPI:
    workspace = Workspace(
        input_dir,
        work_dir,
        products_path,
        image_base_url,
        dataset_dir,
        hosted_store,
        pending_dir,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        worker = workspace.start_prefetch()
        try:
            yield
        finally:
            workspace.stop_prefetch(worker)

    app = FastAPI(
        title="Silicone Shadows", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    if hosted_store:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=trusted_hosts or ["localhost", "127.0.0.1", "testserver"],
        )
    app.state.workspace = workspace
    app.mount("/static", StaticFiles(directory=workspace.static_dir), name="static")
    pages.register(app, workspace, secure_cookies)
    public.register(app, workspace)
    reviews.register(app, workspace)
    moderation.register(app, workspace)
    return app
