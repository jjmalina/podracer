"""FastAPI dependencies and request-validation helpers shared by the web routes."""
import ipaddress
import socket
import sqlite3
from collections.abc import Iterator
from urllib.parse import urlparse

from fastapi import HTTPException, Request

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


def validate_external_url(url: str) -> None:
    """SSRF guard for user-supplied URLs the server will fetch.

    Requires an http(s) URL whose host resolves only to public addresses,
    so the web UI can't be used to probe LAN services (router, Proxmox,
    the whisper host, ...). Resolution happens again at fetch time, so a
    DNS-rebinding attacker isn't fully stopped — acceptable for a
    LAN-deployed single-user app; the goal is blocking casual pivots.

    Raises HTTPException(400) on any failure.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="feed_url must be an http(s) URL")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="feed_url host did not resolve") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise HTTPException(status_code=400, detail="feed_url must resolve to a public address")
