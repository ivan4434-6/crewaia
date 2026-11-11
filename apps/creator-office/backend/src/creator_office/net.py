"""Outbound HTTP defaults shared by everything that calls the internet."""

from __future__ import annotations

import contextlib
import ssl
from functools import lru_cache
from typing import Any

import httpx


@lru_cache
def ssl_context() -> ssl.SSLContext:
    """Trust the operating system's CA store as well as certifi's bundle.

    httpx only trusts certifi by default, which breaks behind proxies that
    inspect TLS with a company certificate installed in the system store.
    ``SSL_CERT_FILE`` and ``SSL_CERT_DIR`` are honoured too.
    """
    context = ssl.create_default_context()
    with contextlib.suppress(Exception):
        import certifi

        context.load_verify_locations(certifi.where())
    return context


def http_client(
    *, transport: httpx.BaseTransport | None = None, **kwargs: Any
) -> httpx.Client:
    if transport is not None:
        return httpx.Client(transport=transport, **kwargs)
    return httpx.Client(verify=ssl_context(), **kwargs)


def is_certificate_error(exc: BaseException) -> bool:
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)
