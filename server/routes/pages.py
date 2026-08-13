"""Authentication, session, and HTML page routes."""

import html

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from ..hosted import SESSION_SECONDS
from ..workspace import Workspace

SESSION_COOKIE = "silicone_shadows_session"


def register(app: FastAPI, workspace: Workspace, secure_cookies: bool) -> None:
    store = workspace.hosted_store

    def static_page(name: str) -> FileResponse:
        return FileResponse(
            workspace.static_dir / name,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request.state.user = None
        if not store:
            return await call_next(request)
        path = request.scope["path"]
        if path in {
            "/api/public/rembg",
            "/api/public/archive",
            "/api/public/submit",
        }:
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    limit = (
                        34 * 1024 * 1024
                        if path == "/api/public/submit"
                        else 17 * 1024 * 1024
                    )
                    too_large = int(content_length) > limit
                except ValueError:
                    too_large = True
                if too_large:
                    return JSONResponse({"detail": "image is too large"}, 413)
        request.state.user = store.user_for_session(request.cookies.get(SESSION_COOKIE))
        public = (
            path == "/"
            or path == "/api/session"
            or path
            in {
                "/static/public.css",
                "/static/public.js",
                "/static/metadata.css",
                "/static/metadata.js",
            }
            or path.startswith("/invite/")
            or path.startswith("/api/public/")
        )
        if not request.state.user and not public:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "authentication required"}, 401)
            return RedirectResponse("/", status_code=303)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if store:
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
            response.headers["Permissions-Policy"] = (
                "camera=(), microphone=(), geolocation=()"
            )
            if secure_cookies:
                response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/")
    def index() -> FileResponse:
        return static_page("public.html" if store else "index.html")

    @app.get("/review")
    def review_page() -> FileResponse:
        return static_page("index.html")

    @app.get("/stats")
    def stats_page() -> FileResponse:
        return static_page("stats.html")

    @app.get("/compare")
    def compare_page() -> FileResponse:
        return static_page("compare.html")

    @app.get("/moderate")
    def moderate_page(request: Request) -> FileResponse:
        user = request.state.user
        if not user or not user.reviewer:
            raise HTTPException(status_code=403, detail="reviewer access required")
        return static_page("moderate.html")

    @app.get("/invite/{token}")
    def invite_page(token: str) -> HTMLResponse:
        if not store:
            raise HTTPException(status_code=404)
        return HTMLResponse(
            "<!doctype html><meta name=viewport content='width=device-width'>"
            "<title>Accept invitation</title><link rel=stylesheet href=/static/public.css>"
            "<body class=invite-page><main class=invite-card>"
            "<h1>Silicone Shadows</h1><p>Accept this one-use contributor invitation?</p>"
            f"<form method=post action='/invite/{html.escape(token, quote=True)}'>"
            "<button class=primary>Accept invitation</button></form></main>"
        )

    @app.post("/invite/{token}")
    def redeem_invite(token: str):
        if not store:
            raise HTTPException(status_code=404)
        redeemed = store.redeem_invite(token)
        if not redeemed:
            return HTMLResponse(
                "<!doctype html><meta name=viewport content='width=device-width'>"
                "<title>Invalid invitation</title><link rel=stylesheet href=/static/public.css>"
                "<body class=invite-page><main class=invite-card>"
                "<h1>This invite is invalid, expired, or already used.</h1></main>",
                400,
            )
        session_token, _ = redeemed
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=secure_cookies,
            samesite="strict",
        )
        return response

    @app.get("/api/session")
    def session_info(request: Request) -> dict:
        user = request.state.user
        return {
            "hosted": bool(store),
            "user": ({"name": user.name, "reviewer": user.reviewer} if user else None),
        }

    @app.post("/api/logout")
    def logout(request: Request) -> Response:
        if store:
            if request.state.user:
                store.release_claims(request.state.user, discard=workspace.discard_work)
            store.logout(request.cookies.get(SESSION_COOKIE))
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE)
        return response
