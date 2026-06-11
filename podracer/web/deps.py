"""FastAPI dependencies shared by the web routes."""
import sqlite3
from collections.abc import Iterator

from fastapi import Request

from podracer.db import get_connection


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Yield a per-request SQLite connection.

    Routes run in FastAPI's threadpool, so a shared connection would be
    used from multiple threads at once. A fresh connection per request is
    cheap with SQLite + WAL and keeps each request's transaction isolated.

    check_same_thread=False because FastAPI may open the dependency, run
    the endpoint, and close the dependency on different threadpool threads;
    the connection is still only used by one request at a time.
    """
    conn = get_connection(request.app.state.cfg.db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()
