"""Predict.Fun REST and WebSocket clients.

Public-endpoint focused for the shadow dry run. JWT auth is stubbed but not
required until we move past shadow mode.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets

from config import get_config

log = logging.getLogger("predict_fun")

GENERAL_ENDPOINTS = {
    "/v1/markets",
    "/v1/categories",
    "/v1/tags",
    "/v1/search",
    "/v1/auth",
    "/v1/auth/message",
}
TRADING_ENDPOINTS = {
    "/v1/orders",
    "/v1/orders/remove",
    "/v1/orders/remove-by-hash",
    "/v1/orders/matches",
    "/v1/positions",
    "/v1/account/activity",
}


def _bucket_for_path(path: str) -> str:
    """Classify a URL path as 'general' or 'trading' for rate-limit tracking."""
    base = path.split("?")[0]
    # Orderbook/statistics endpoints are classified as trading per user instructions.
    if "/orderbook" in base or "/statistics" in base or "/last-sale" in base:
        return "trading"
    if base in TRADING_ENDPOINTS or any(base.startswith(p) for p in TRADING_ENDPOINTS):
        return "trading"
    return "general"


class RateLimitState:
    """Track one rate-limit bucket from response headers."""

    def __init__(self, default_limit: int, default_window: int = 60):
        self.limit = default_limit
        self.remaining = default_limit
        self.reset_at = 0.0
        self.window = default_window
        self.burst_remaining: int | None = None
        self.burst_limit: int | None = None
        self.lock = asyncio.Lock()

    def update_from_headers(self, headers: httpx.Headers) -> None:
        try:
            self.limit = int(headers.get("ratelimit-limit", self.limit))
            self.remaining = int(headers.get("ratelimit-remaining", self.remaining))
            reset_sec = headers.get("ratelimit-reset")
            if reset_sec is not None:
                self.reset_at = time.time() + float(reset_sec)
            policy = headers.get("ratelimit-policy", "")
            if ";w=" in policy:
                self.window = int(policy.split(";w=")[1].split(";")[0])
            self.burst_remaining = headers.get("ratelimit-burst-remaining")
            if self.burst_remaining is not None:
                self.burst_remaining = int(self.burst_remaining)
            self.burst_limit = headers.get("ratelimit-burst-limit")
            if self.burst_limit is not None:
                self.burst_limit = int(self.burst_limit)
        except Exception as e:
            log.debug(f"Failed to parse rate-limit headers: {e}")

    async def acquire(self) -> None:
        async with self.lock:
            if self.remaining <= 0 and time.time() < self.reset_at:
                wait = max(0.0, self.reset_at - time.time())
                log.warning(f"Rate-limit bucket exhausted; waiting {wait:.1f}s")
                await asyncio.sleep(wait)

    def __repr__(self) -> str:
        return f"RateLimitState(limit={self.limit}, remaining={self.remaining})"


class PredictFunClient:
    """REST client for Predict.Fun public endpoints."""

    def __init__(self, config=None, client: httpx.AsyncClient | None = None, auth=None):
        self.config = config or get_config()
        self.api_key = self.config.require_api_key()
        self.base_url = self.config.base_url.rstrip("/")
        self._client = client
        self._owned_client = client is None
        self.auth = auth
        self._last_call = 0.0
        self._call_lock = asyncio.Lock()
        self.general_limit = RateLimitState(self.config.general_rpm)
        self.trading_limit = RateLimitState(self.config.trading_rpm)
        self._bucket_totals: dict[str, int] = {"general": 0, "trading": 0}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Accept": "application/json",
        }

    async def _auth_headers(self) -> dict[str, str]:
        headers = self._headers()
        if self.auth:
            token = await self.auth.get_jwt()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _throttle(self) -> None:
        async with self._call_lock:
            elapsed = time.time() - self._last_call
            if elapsed < self.config.min_call_interval:
                await asyncio.sleep(self.config.min_call_interval - elapsed)
            self._last_call = time.time()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        """Make a rate-limited request and return JSON."""
        bucket = _bucket_for_path(path.split("?")[0])
        limit_state = self.general_limit if bucket == "general" else self.trading_limit
        await limit_state.acquire()
        await self._throttle()

        headers = await self._auth_headers() if authenticated else self._headers()
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        url = f"{self.base_url}{path}"
        client = await self._get_client()

        for attempt in range(self.config.max_retries + 1):
            try:
                resp = await client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
                limit_state.update_from_headers(resp.headers)
                self._bucket_totals[bucket] += 1

                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", self.config.base_backoff * (2 ** attempt)))
                    log.warning(f"429 on {path}; retry after {retry_after}s")
                    await asyncio.sleep(retry_after)
                    continue

                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    try:
                        body = resp.json()
                        log.error(f"HTTP {resp.status_code} response: {body}")
                    except Exception:
                        log.error(f"HTTP {resp.status_code} response text: {resp.text[:500]}")
                    raise
                return resp.json()
            except httpx.HTTPStatusError as e:
                if 500 <= e.response.status_code < 600 and attempt < self.config.max_retries:
                    await asyncio.sleep(self.config.base_backoff * (2 ** attempt))
                    continue
                raise
            except (httpx.NetworkError, httpx.TimeoutException) as e:
                if attempt < self.config.max_retries:
                    await asyncio.sleep(self.config.base_backoff * (2 ** attempt))
                    continue
                raise

        raise RuntimeError(f"Max retries exceeded for {method} {path}")

    # --- Public market endpoints ---

    async def get_markets(
        self,
        *,
        first: int | None = None,
        after: str | None = None,
        status: str | None = None,
        tag_ids: list[int] | None = None,
        sort: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if first is not None:
            params["first"] = first
        if after is not None:
            params["after"] = after
        if status is not None:
            params["status"] = status
        if tag_ids:
            params["tagIds"] = ",".join(str(t) for t in tag_ids)
        if sort is not None:
            params["sort"] = sort
        return await self._request("GET", "/v1/markets", params=params or None)

    async def get_market(self, market_id: int | str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/markets/{market_id}")

    async def get_orderbook(self, market_id: int | str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/markets/{market_id}/orderbook")

    async def get_statistics(self, market_id: int | str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/markets/{market_id}/statistics")

    async def get_categories(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/categories")

    async def get_tags(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/tags")

    # --- Account / trading endpoints (JWT required) ---

    async def get_auth_message(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/auth/message")

    async def post_auth(self, signer: str, message: str, signature: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/auth",
            json_body={"signer": signer, "message": message, "signature": signature},
        )

    async def get_account(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/account", authenticated=True)

    async def get_account_activity(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/account/activity", authenticated=True)

    async def get_orders(self, market_id: int | str | None = None) -> dict[str, Any]:
        params = {"marketId": market_id} if market_id is not None else None
        return await self._request("GET", "/v1/orders", params=params, authenticated=True)

    async def get_order(self, order_hash: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/orders/{order_hash}", authenticated=True)

    async def get_positions(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/positions", authenticated=True)

    async def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/orders. In PAPER mode, logs and returns a fake ID."""
        if self.config.mode == "PAPER":
            data = payload.get("data", {})
            order = data.get("order", {})
            fake_id = f"paper-{order.get('salt', '0')[:16]}"
            log.info(f"[PAPER] intercepted POST /v1/orders: market={order.get('tokenId')} strategy={data.get('strategy')} price={data.get('pricePerShare')}")
            log.debug(f"[PAPER] full payload keys: {list(payload.keys())}")
            return {"data": {"id": fake_id, "status": "PAPER"}, "success": True}
        if self.config.mode == "SHADOW":
            raise RuntimeError("create_order called in SHADOW mode")
        return await self._request("POST", "/v1/orders", json_body=payload, authenticated=True)

    async def cancel_order(self, order_hash: str) -> dict[str, Any]:
        """POST /v1/orders/remove. In PAPER mode, logs and returns success."""
        if self.config.mode == "PAPER":
            log.info(f"[PAPER] intercepted POST /v1/orders/remove: hash={order_hash}")
            return {"data": {"orderHash": order_hash, "status": "PAPER_CANCELLED"}, "success": True}
        if self.config.mode == "SHADOW":
            raise RuntimeError("cancel_order called in SHADOW mode")
        return await self._request(
            "POST",
            "/v1/orders/remove",
            json_body={"orderHash": order_hash},
            authenticated=True,
        )

    def rate_limit_summary(self) -> dict[str, Any]:
        return {
            "general": {
                "limit": self.general_limit.limit,
                "remaining": self.general_limit.remaining,
                "calls": self._bucket_totals["general"],
            },
            "trading": {
                "limit": self.trading_limit.limit,
                "remaining": self.trading_limit.remaining,
                "calls": self._bucket_totals["trading"],
            },
        }


class PredictFunWebSocket:
    """WebSocket client for Predict.Fun market data."""

    def __init__(self, config=None):
        self.config = config or get_config()
        self.api_key = self.config.require_api_key()
        self.url = self.config.ws_url
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._subscriptions: set[str] = set()
        self._running = False
        self._latest: dict[str, dict[str, Any]] = {}
        self._req_id = 0
        self._reconnect_delay = self.config.ws_reconnect_delay
        self._heartbeat_task: asyncio.Task | None = None
        self._receive_task: asyncio.Task | None = None

    def _next_req_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def connect(self) -> None:
        """Establish the WebSocket connection and start the receive loop."""
        log.info(f"Connecting to Predict.Fun WS: {self.url}")
        extra_headers = {"x-api-key": self.api_key}
        self.ws = await websockets.connect(self.url, additional_headers=extra_headers)
        self._running = True
        if self._receive_task is None or self._receive_task.done():
            self._receive_task = asyncio.create_task(self._receive_loop())
        log.info("Predict.Fun WS connected")
        for topic in list(self._subscriptions):
            await self.subscribe(topic)

    async def close(self) -> None:
        self._running = False
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def subscribe(self, topic: str) -> int:
        self._subscriptions.add(topic)
        req_id = self._next_req_id()
        if self.ws and self._running:
            try:
                await self.ws.send(json.dumps({"method": "subscribe", "requestId": req_id, "params": [topic]}))
            except websockets.ConnectionClosed:
                log.warning(f"WS closed while subscribing to {topic}; will retry on reconnect")
        return req_id

    async def unsubscribe(self, topic: str) -> int:
        self._subscriptions.discard(topic)
        req_id = self._next_req_id()
        if self.ws and self._running:
            try:
                await self.ws.send(json.dumps({"method": "unsubscribe", "requestId": req_id, "params": [topic]}))
            except websockets.ConnectionClosed:
                pass
        return req_id

    async def _receive_loop(self) -> None:
        """Single receive loop. Handles connection loss internally."""
        while self._running:
            try:
                if self.ws is None:
                    await self._reconnect()
                    if self.ws is None:
                        continue
                msg = await asyncio.wait_for(self.ws.recv(), timeout=self.config.ws_heartbeat_interval + 10)
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8")
                data = json.loads(msg)
                await self._handle_message(data)
            except asyncio.TimeoutError:
                log.warning("Predict.Fun WS receive timeout; reconnecting")
                await self._safe_close_ws()
            except websockets.ConnectionClosed:
                log.warning("Predict.Fun WS closed; reconnecting")
                await self._safe_close_ws()
            except Exception as e:
                log.exception(f"Predict.Fun WS error: {e}")
                await self._safe_close_ws()

    async def _handle_message(self, data: dict[str, Any]) -> None:
        msg_type = data.get("type")
        topic = data.get("topic")
        if msg_type == "M" and topic == "heartbeat":
            ts = data.get("data")
            if ts is not None and self.ws:
                try:
                    await self.ws.send(json.dumps({"method": "heartbeat", "data": ts}))
                except websockets.ConnectionClosed:
                    pass
            return
        if msg_type == "M" and topic:
            self._latest[topic] = data.get("data", {})

    async def _safe_close_ws(self) -> None:
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        await self._reconnect()

    async def _reconnect(self) -> None:
        """Reconnect without spawning nested receive loops."""
        delay = min(self._reconnect_delay, self.config.ws_max_reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self.config.ws_max_reconnect_delay)
        log.info(f"Reconnecting Predict.Fun WS in {delay}s")
        await asyncio.sleep(delay)
        try:
            extra_headers = {"x-api-key": self.api_key}
            self.ws = await websockets.connect(self.url, additional_headers=extra_headers)
            log.info("Predict.Fun WS reconnected")
            for topic in list(self._subscriptions):
                await self.subscribe(topic)
        except Exception as e:
            log.warning(f"Predict.Fun WS reconnect failed: {e}")
            self.ws = None

    def get_snapshot(self, topic: str) -> dict[str, Any] | None:
        return self._latest.get(topic)

    async def iter_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Placeholder iterator; real consumers use get_snapshot."""
        while self._running:
            await asyncio.sleep(0.1)
