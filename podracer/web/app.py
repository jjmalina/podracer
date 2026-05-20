import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from podracer.config import Config, load_config
from podracer.db import init_db
from podracer.web.routes.episodes import router as episodes_router
from podracer.web.routes.podcasts import router as podcasts_router
from podracer.web.routes.search import router as search_router

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


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
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(podcasts_router)
    app.include_router(episodes_router)
    app.include_router(search_router)

    return app


app = create_app(load_config())
