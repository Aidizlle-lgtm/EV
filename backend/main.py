"""E.V. application: API + the browser interface, served together so the page
never needs privileged access of its own."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import auth, config, db
from .routes import auth_routes, chat_routes, data_routes, system_routes
from .services import jobs
from .tools import builtin  # noqa: F401 - registers the built-in tools

log = logging.getLogger("ev")

OPEN_PATHS = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/logout"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class GuardMiddleware:
    """Pure ASGI middleware (streaming-safe): session check, CSRF header, security headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = scope["path"]
        if path.startswith("/api/"):
            if request.method not in SAFE_METHODS and request.headers.get("x-ev-request") != "1":
                await JSONResponse({"detail": "Missing X-EV-Request header."}, 403)(scope, receive, send)
                return
            if path not in OPEN_PATHS and not auth.session_valid(request.cookies.get(auth.COOKIE_NAME)):
                await JSONResponse({"detail": "Not signed in."}, 401)(scope, receive, send)
                return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for k, v in SECURITY_HEADERS.items():
                    headers.setdefault(k, v)
            await send(message)

        await self.app(scope, receive, send_with_headers)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    stop = asyncio.Event()
    task = asyncio.create_task(jobs.scheduler_loop(stop))
    log.info("E.V. ready (data: %s)", config.DATA_DIR)
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def create_app(start_scheduler: bool = True) -> FastAPI:
    app = FastAPI(title="E.V.", version=system_routes.VERSION, lifespan=lifespan if start_scheduler else None,
                  docs_url=None, redoc_url=None, openapi_url=None)
    if not start_scheduler:
        db.init()
    app.add_middleware(GuardMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'][1:]) or 'body'}: {e['msg']}" for e in exc.errors()[:5])
        return JSONResponse({"detail": f"Invalid input — {problems}"}, 422)

    for r in (auth_routes.router, chat_routes.router, data_routes.router, system_routes.router):
        app.include_router(r)

    app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(config.FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(config.FRONTEND_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    return app


app = create_app()
