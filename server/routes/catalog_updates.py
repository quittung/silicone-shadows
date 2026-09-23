"""Catalog controls for local users and hosted reviewers."""

import asyncio

from fastapi import HTTPException, Request


def register(app, updates):
    # Keep ordinary requests concurrent; activation alone drains in-flight requests.
    condition = asyncio.Condition()
    active = 0
    updating = False

    @app.middleware("http")
    async def catalog_snapshot(request: Request, call_next):
        nonlocal active, updating
        exclusive = request.method == "POST" and request.url.path == "/api/catalog/update"
        async with condition:
            await condition.wait_for(lambda: not updating)
            if exclusive:
                updating = True
                try:
                    await condition.wait_for(lambda: active == 0)
                except BaseException:
                    updating = False
                    condition.notify_all()
                    raise
            else:
                active += 1
        try:
            return await call_next(request)
        finally:
            async with condition:
                if exclusive:
                    updating = False
                else:
                    active -= 1
                condition.notify_all()

    def authorize(request):
        workspace = app.state.workspace
        if workspace.hosted_store and not getattr(request.state.user, "reviewer", False):
            raise HTTPException(403, "reviewer access required")
        if updates is None:
            raise HTTPException(404, "Updates are unavailable for a custom catalog.")

    @app.get("/api/catalog")
    def status(request: Request):
        authorize(request)
        return updates.status()

    @app.post("/api/catalog/check")
    def check(request: Request):
        authorize(request)
        return updates.check(force=True)

    @app.post("/api/catalog/update")
    def update(request: Request):
        authorize(request)
        try:
            return updates.apply()
        except (OSError, ValueError) as error:
            raise HTTPException(409, str(error)) from error
