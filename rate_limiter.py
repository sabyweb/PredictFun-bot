"""Rate-limited wrapper around PredictFunClient.

Mirrors the Polymarket RateLimitedClient pattern: intercept selected methods,
apply retries/backoff, and expose everything else transparently.
"""

import asyncio
import logging
import time
from typing import Any

from predict_client import PredictFunClient, PredictFunWebSocket

log = logging.getLogger("predict_fun")

_RATE_LIMITED_METHODS = {
    "get_markets",
    "get_market",
    "get_orderbook",
    "get_statistics",
    "get_categories",
    "get_tags",
    "get_auth_message",
    "post_auth",
    # Trading endpoints (gated in shadow mode)
    "get_orders",
    "create_order",
    "cancel_order",
}


class RateLimitedClient:
    """Thin wrapper that adds logging and retries around PredictFunClient."""

    def __init__(self, client: PredictFunClient | None = None):
        self._client = client or PredictFunClient()
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self):
        await self._client.close()

    async def _throttle(self):
        async with self._lock:
            elapsed = time.time() - self._last_call
            min_interval = self._client.config.min_call_interval
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
            self._last_call = time.time()

    async def _call(self, name: str, *args, **kwargs) -> Any:
        await self._throttle()
        method = getattr(self._client, name)
        log.debug(f"Predict.Fun API: {name}({args}, {kwargs})")
        return await method(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name in _RATE_LIMITED_METHODS:
            return lambda *args, **kwargs: self._call(name, *args, **kwargs)
        return getattr(self._client, name)

    def rate_limit_summary(self) -> dict[str, Any]:
        return self._client.rate_limit_summary()
