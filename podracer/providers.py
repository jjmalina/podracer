"""OpenRouter provider policy: the allowlist model boundary.

One place decides what a valid allowlist is, how a response's provider
display name maps onto a configured slug, and which providers are never
routed to. Config loading, ``Backend.openrouter`` and the CLI all validate
through :func:`validate_allowlist`, so an allowlist can't silently turn
itself off at any entry point.
"""
import re

# Providers that advertise response_format.json_schema support (so they pass
# require_parameters) but return prose anyway — never route to them. Baidu was
# observed returning prose (invalid_json) on every retry for multiple calls,
# discarding their output. Add a provider here once it proves it can't be
# trusted with structured output.
DENYLISTED_PROVIDERS = ["Baidu"]


class ProviderNotAllowedError(RuntimeError):
    """OpenRouter attributed a completion to a provider outside the allowlist."""


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def provider_allowed(provider: str, allowed: list[str]) -> bool:
    """Match OpenRouter's response ``provider`` (a display name such as
    "DeepInfra", "Atlas Cloud" or "Mancer 2") against configured slugs
    ("deepinfra", "atlas-cloud", "mancer"). Equal once case and punctuation
    are stripped, or the display name is the slug followed by a separated
    qualifier ("Mancer 2", "Mancer (private)") — never a bare prefix, so
    "deepinfra" does not accept "DeepInfraX"."""
    p = _normalize(provider)
    raw = provider.lower()
    for a in allowed:
        n = _normalize(a)
        if p == n:
            return True
        if raw.startswith(n) and len(raw) > len(n) and not raw[len(n)].isalnum():
            return True
    return False


def validate_allowlist(value: object, *, where: str = "openrouter_providers") -> list[str] | None:
    """Return a clean provider allowlist, or ``None`` for "unrestricted".

    Rejects anything but a non-empty list of non-empty strings. ``[]`` is an
    error rather than "unrestricted" — an allowlist that silently switches
    itself off is the wrong failure mode; omit the setting to lift the
    restriction. Also rejects a list made entirely of denylisted providers,
    which would make every request unroutable (``only`` minus ``ignore`` is
    empty, so OpenRouter 404s) while the startup log claims an active
    allowlist.
    """
    if value is None:
        return None
    ok = isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)
    items = [str(v).strip() for v in value] if ok and isinstance(value, list) else None
    if items is None:
        raise ValueError(
            f"{where} must be a list of provider slugs, "
            f"e.g. [\"deepinfra\", \"digitalocean\"]; got {value!r}",
        )
    if not items:
        raise ValueError(
            f"{where} is empty; list at least one provider slug "
            "or omit it to allow any provider",
        )
    routable = [a for a in items if not any(provider_allowed(d, [a]) for d in DENYLISTED_PROVIDERS)]
    if not routable:
        raise ValueError(
            f"{where} {items!r} contains only denylisted providers "
            f"({DENYLISTED_PROVIDERS}); nothing would be routable",
        )
    return items
