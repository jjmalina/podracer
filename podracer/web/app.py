import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from podracer import logger
from podracer.config import Config, load_config
from podracer.db import get_connection, init_db
from podracer.feed import configure_timeouts
from podracer.logging_config import configure_logging
from podracer.sentry_config import configure_sentry
from podracer.web.routes.api import API_PREFIX, SCHEMA_VERSION
from podracer.web.routes.api import router as api_router
from podracer.web.routes.episodes import router as episodes_router
from podracer.web.routes.feed import router as feed_router
from podracer.web.routes.jobs import router as jobs_router
from podracer.web.routes.podcasts import router as podcasts_router
from podracer.web.routes.search import router as search_router

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>]+"
    r"|[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*\.[a-z]{2,24}/[^\s<>]+",
    re.IGNORECASE,
)
_TRAILING_PUNCT = ".,;:!?)]>"


def linkify(text: str | None) -> Markup:
    """Escape `text` and wrap URLs (http(s)://, www., or domain.tld/path) in <a> tags."""
    if not text:
        return Markup("")
    out: list = []
    pos = 0
    for m in _URL_RE.finditer(text):
        out.append(escape(text[pos:m.start()]))
        display = m.group(0)
        trail = ""
        while display and display[-1] in _TRAILING_PUNCT:
            trail = display[-1] + trail
            display = display[:-1]
        href = display if display.lower().startswith(("http://", "https://")) else f"https://{display}"
        out.append(Markup(
            f'<a href="{escape(href)}" target="_blank" rel="noopener noreferrer">{escape(display)}</a>'
        ))
        if trail:
            out.append(escape(trail))
        pos = m.end()
    out.append(escape(text[pos:]))
    return Markup("".join(out))


def create_app(cfg: Config) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Configure logging here (not just in cli.main) so the app logs
        # consistently however it's launched — uvicorn directly, --reload
        # subprocess, or an external ASGI server. cfg.log_format applies the
        # config.toml setting (env still wins).
        configure_logging(cfg.log_format)
        # Bound the interactive feed fetches (browse/subscribe/sync) with the
        # same configured timeouts the worker uses — set here in the lifespan so
        # it applies however the app is launched (uvicorn directly, the --reload
        # subprocess, or an external ASGI server), not just from cli.cmd_serve.
        configure_timeouts(cfg.feed_connect_timeout_seconds, cfg.feed_read_timeout_seconds)
        # Apply schema + migrations once at startup; requests open their
        # own connections via the get_db dependency (see web/deps.py).
        conn = get_connection(cfg.db_path)
        try:
            init_db(conn)
        finally:
            conn.close()
        app.state.cfg = cfg
        yield

    # The interactive docs cover only the JSON API and are served under the
    # /api/v1 prefix (below); disable the default root /docs, /redoc and
    # /openapi.json so the HTML UI routes never leak into a published schema.
    app = FastAPI(
        title="podracer", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates.env.filters["linkify"] = linkify
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(feed_router)
    app.include_router(podcasts_router)
    app.include_router(episodes_router)
    app.include_router(search_router)
    app.include_router(jobs_router)
    app.include_router(api_router)

    # An outbound feed fetch can now raise (bounded timeouts + raise_for_status,
    # see feed.py) where it used to silently return an empty feed. Render a
    # friendly 502 for the HTML UI (browse/subscribe/single-sync) instead of an
    # opaque 500. The JSON API doesn't make outbound fetches, so this only ever
    # fires for the feed-fetching HTML routes.
    @app.exception_handler(httpx.HTTPError)
    async def feed_fetch_error(request: Request, exc: httpx.HTTPError) -> Response:
        logger.warning("feed_fetch_failed", path=request.url.path, error=str(exc))
        return app.state.templates.TemplateResponse(request, "error.html", {
            "request": request,
            "title": "Couldn't reach that feed",
            "message": "The podcast feed was unreachable or returned an error. "
                       "It may be temporarily down — try again in a bit.",
        }, status_code=502)

    # OpenAPI schema + Swagger UI for the JSON API, scoped to the /api/v1 routes
    # only — the HTML UI routes are filtered out so the contract stays API-only.
    @app.get(f"{API_PREFIX}/openapi.json", include_in_schema=False)
    def api_openapi() -> JSONResponse:
        return JSONResponse(get_openapi(
            title="podracer REST API",
            version=SCHEMA_VERSION,
            description="Read-only JSON API for podcasts, episodes, summaries, and transcripts.",
            routes=[r for r in app.routes if getattr(r, "path", "").startswith(API_PREFIX)],
        ))

    @app.get(f"{API_PREFIX}/docs", include_in_schema=False)
    def api_docs() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=f"{API_PREFIX}/openapi.json", title="podracer REST API — docs",
        )

    return app


# Configure logging + Sentry at import time so the module-level construction
# below — and uvicorn --reload subprocesses, which import this module directly —
# behave consistently however launched. Sentry must init before the FastAPI app
# is built so its integration instruments the app. Load config once and apply
# its [logging]/[sentry] values (env still wins). Idempotent with cli.main.
configure_logging()  # bootstrap (env/auto) for any import-time logs
_cfg = load_config()
configure_logging(_cfg.log_format)
configure_sentry(_cfg.sentry_dsn)
app = create_app(_cfg)
