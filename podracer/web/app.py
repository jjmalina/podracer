import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from podracer.config import Config, load_config
from podracer.db import init_db
from podracer.web.routes.episodes import router as episodes_router
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
        db_path = cfg.db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        init_db(conn)
        app.state.db = conn
        app.state.cfg = cfg
        yield
        conn.close()

    app = FastAPI(title="podracer", lifespan=lifespan)
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates.env.filters["linkify"] = linkify
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(podcasts_router)
    app.include_router(episodes_router)
    app.include_router(search_router)
    app.include_router(jobs_router)

    return app


app = create_app(load_config())
