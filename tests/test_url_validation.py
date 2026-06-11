"""Tests for the SSRF guard on user-supplied feed URLs."""
import pytest
from fastapi import HTTPException

from podracer.web.deps import validate_external_url


def _rejected(url: str) -> bool:
    try:
        validate_external_url(url)
    except HTTPException as e:
        assert e.status_code == 400
        return True
    return False


# IP literals and /etc/hosts names only — no network access needed.

def test_public_ip_allowed():
    validate_external_url("https://1.1.1.1/feed.xml")


@pytest.mark.parametrize("url", [
    "ftp://example.com/feed.xml",
    "file:///etc/passwd",
    "gopher://example.com/",
    "not-a-url",
    "https://",
])
def test_non_http_or_malformed_rejected(url):
    assert _rejected(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9200/_search",      # loopback
    "http://localhost:8080/jobs",          # loopback by name
    "http://192.168.1.1/",                 # RFC 1918
    "http://10.0.0.5:8006/",               # RFC 1918 (Proxmox-ish)
    "http://172.16.0.1/",                  # RFC 1918
    "http://169.254.169.254/latest/meta-data/",  # link-local / metadata
    "http://[::1]:9000/v1/transcribe",     # IPv6 loopback
    "http://0.0.0.0/",                     # unspecified
])
def test_internal_addresses_rejected(url):
    assert _rejected(url)


def test_unresolvable_host_rejected():
    assert _rejected("https://definitely-not-a-real-host.invalid/feed.xml")
