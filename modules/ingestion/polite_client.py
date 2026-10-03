"""
Deliverable 2.2 — Polite HTTP Layer (rate limiting + proxy management)
========================================================================
Single async HTTP entry point for all ingestion fetchers:

  • Per-host minimum interval (env ``CRAWL_DELAY_SECONDS``, default 1.5 s)
    with random jitter so bursts never hammer a government server.
  • tenacity retries with exponential backoff on HTTP 429 / 5xx and
    transport errors; honours the ``Retry-After`` response header.
  • Optional proxy pool (env ``PROXY_LIST`` comma-separated, falling back
    to ``HTTP_PROXY``/``HTTPS_PROXY``): round-robin rotation per host on
    connection failure, with a no-proxy fallback when the pool is empty.

Usage
-----
  client = PoliteClient()
  resp = await client.get("https://<socrata-domain>/api/views/<dataset-id>/rows.json")
  await client.aclose()

Tests inject ``transport=httpx.MockTransport(...)`` plus a fake clock/sleep
so every behaviour is verifiable offline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    retry_if_exception_type,
)

logger = logging.getLogger(__name__)

DEFAULT_MIN_INTERVAL = 1.5        # seconds between requests to the same host
DEFAULT_JITTER = 0.25             # extra random delay added on top of the interval
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 1.0          # exponential backoff base (1s, 2s, 4s, ...)
MAX_RETRY_AFTER = 120.0           # cap on honoured Retry-After values
PROXY_PING_URL = "https://www.google.com/generate_204"


class RetryableHTTPError(Exception):
    """Raised internally for 429/5xx so tenacity can back off and retry."""

    def __init__(self, status_code: int, url: str, retry_after: float | None = None):
        self.status_code = status_code
        self.url = url
        self.retry_after = retry_after
        super().__init__(f"HTTP {status_code} for {url}")


class ProxyRotatedError(Exception):
    """Transport failure that was handled by rotating to the next proxy."""

    def __init__(self, message: str, original: Exception | None = None):
        self.original = original
        super().__init__(message)


@dataclass
class _HostState:
    last_request: float = 0.0
    proxy_index: int = 0
    failures: int = 0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _default_proxy_list() -> list[str]:
    """PROXY_LIST (comma-separated) wins; else single HTTP(S)_PROXY; else none."""
    raw = os.environ.get("PROXY_LIST", "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(name, "").strip()
        if val:
            return [val]
    return []


class PoliteClient:
    """
    Async HTTP client that is polite to government data servers.

    Parameters
    ----------
    min_interval:
        Minimum seconds between requests to the same host.
        Defaults to env ``CRAWL_DELAY_SECONDS`` (1.5 s).
    jitter:
        Max extra random delay (seconds) added after waiting the interval.
    max_attempts / base_delay:
        Retry budget and exponential backoff base for 429/5xx/transport errors.
    proxy_list:
        Explicit proxy URLs. Defaults to env ``PROXY_LIST`` or HTTP(S)_PROXY.
    transport:
        Optional httpx transport (used by tests with ``httpx.MockTransport``).
    clock / sleep_func:
        Injectable for deterministic rate-limiter tests.
    """

    def __init__(
        self,
        min_interval: float | None = None,
        jitter: float = DEFAULT_JITTER,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY,
        timeout: float = 30.0,
        user_agent: str | None = None,
        proxy_list: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.min_interval = (
            min_interval if min_interval is not None
            else _env_float("CRAWL_DELAY_SECONDS", DEFAULT_MIN_INTERVAL)
        )
        self.jitter = jitter
        self.max_attempts = max(1, max_attempts)
        self.base_delay = base_delay
        self.proxy_list = proxy_list if proxy_list is not None else _default_proxy_list()
        self._clock = clock
        self._sleep = sleep_func

        self._user_agent = user_agent or os.environ.get(
            "CRAWLER_USER_AGENT",
            "RealEstateMagnet/0.1 (research; contact: admin@example.com)",
        )
        self._timeout = httpx.Timeout(timeout)
        self._transport = transport
        # one httpx client per proxy (httpx 0.28 binds the proxy at client level);
        # key None = no proxy
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        self._hosts: dict[str, _HostState] = {}

        # tenacity retry policy for the internal request attempt
        self._retry = retry(
            stop=stop_after_attempt(self.max_attempts),
            wait=self._wait_fn,
            retry=retry_if_exception_type((RetryableHTTPError, ProxyRotatedError)),
            reraise=True,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET ``url`` with per-host throttling, retries and proxy rotation."""
        host = urllib.parse.urlparse(url).netloc.lower()
        state = self._host_state(host)
        await self._throttle(state)
        return await self._retry(self._do_request)(url, state, kwargs)

    async def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        """GET and return the raw body (for file downloads)."""
        resp = await self.get(url, **kwargs)
        return resp.content

    async def check_proxies(self, ping_url: str = PROXY_PING_URL) -> list[str]:
        """Ping each configured proxy; return the healthy subset."""
        healthy: list[str] = []
        for proxy in self.proxy_list:
            try:
                await asyncio.wait_for(
                    self._client_for(proxy).get(ping_url, timeout=10),
                    timeout=15,
                )
                healthy.append(proxy)
            except Exception as exc:
                logger.warning("Proxy %s failed health check: %s", proxy, exc)
        return healthy

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ------------------------------------------------------------------
    # Client pool (one httpx client per proxy)
    # ------------------------------------------------------------------

    def _client_for(self, proxy: str | None) -> httpx.AsyncClient:
        key = proxy or None
        if key not in self._clients:
            self._clients[key] = httpx.AsyncClient(
                headers={
                    "User-Agent": self._user_agent,
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=self._timeout,
                follow_redirects=True,
                transport=self._transport,
                proxy=proxy,
            )
        return self._clients[key]

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _host_state(self, host: str) -> _HostState:
        if host not in self._hosts:
            self._hosts[host] = _HostState()
        return self._hosts[host]

    async def _throttle(self, state: _HostState) -> None:
        elapsed = self._clock() - state.last_request
        if elapsed < self.min_interval:
            delay = (self.min_interval - elapsed) + random.uniform(0, self.jitter)
            logger.debug("Throttling %.2fs for host (last=%.3f)", delay, state.last_request)
            await self._sleep(delay)
        state.last_request = self._clock()

    # ------------------------------------------------------------------
    # Retries / proxy rotation
    # ------------------------------------------------------------------

    def _wait_fn(self, retry_state: Any) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        attempt = retry_state.attempt_number
        if isinstance(exc, RetryableHTTPError) and exc.retry_after is not None:
            return min(exc.retry_after, MAX_RETRY_AFTER) + random.uniform(0, 0.5)
        if isinstance(exc, ProxyRotatedError):
            return 0.0   # proxy already rotated; retry immediately
        return min(self.base_delay * (2 ** (attempt - 1)), 60.0) + random.uniform(0, 0.5)

    def _proxy_for(self, state: _HostState) -> str | None:
        if not self.proxy_list:
            return None
        return self.proxy_list[state.proxy_index % len(self.proxy_list)]

    async def _do_request(
        self, url: str, state: _HostState, kwargs: dict[str, Any]
    ) -> httpx.Response:
        proxy = self._proxy_for(state)
        try:
            resp = await self._client_for(proxy).get(url, **kwargs)
        except httpx.TransportError as exc:
            state.failures += 1
            if len(self.proxy_list) > 1:
                state.proxy_index += 1
                logger.info(
                    "Connection failed via %s; rotating to %s",
                    proxy, self._proxy_for(state),
                )
                raise ProxyRotatedError(str(exc), original=exc) from exc
            raise

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            logger.warning(
                "HTTP %d from %s (retry-after=%s)", resp.status_code, url, retry_after
            )
            raise RetryableHTTPError(resp.status_code, url, retry_after)

        state.failures = 0
        return resp


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds form only)."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
