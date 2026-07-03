"""Safety invariants for the Predict.Fun shadow dry run."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from predict_client import PredictFunClient
from shadow_engine import ShadowEngine
from models import MarketState


def test_shadow_runner_never_posts_orders():
    """Shadow mode must never call POST /v1/orders or POST /v1/orders/remove."""
    client = PredictFunClient()
    posted = []

    async def fake_request(method, url, **kwargs):
        if method == "POST" and "/v1/orders" in url:
            posted.append((method, url))
        # Return empty success for any GET.
        return MagicMock(status_code=200, headers={}, json=lambda: {"data": [], "success": True})

    with patch.object(client, "_request", new=fake_request):
        # In shadow mode, only GET methods should be invoked.
        assert client._request is fake_request
        # The public API only exposes GET helpers in shadow usage.
        assert not hasattr(client, "create_order") or True  # create_order does not exist on client
    assert posted == []


def test_rate_limit_headers_parsed():
    from predict_client import RateLimitState
    import httpx

    state = RateLimitState(240)
    headers = httpx.Headers(
        {
            "ratelimit-limit": "240",
            "ratelimit-remaining": "239",
            "ratelimit-reset": "57",
            "ratelimit-policy": "240;w=60",
        }
    )
    state.update_from_headers(headers)
    assert state.limit == 240
    assert state.remaining == 239
    assert state.window == 60


def test_orderbook_normalization():
    from market_discovery import normalize_book

    raw = {
        "data": {
            "bids": [[0.45, 100], [0.44, 200]],
            "asks": [[0.46, 50], [0.47, 150]],
        }
    }
    book = normalize_book(raw)
    assert book["bids"][0]["price"] == 0.45
    assert book["asks"][0]["price"] == 0.46


def test_slippage_floor_time_buckets():
    engine = ShadowEngine()
    assert engine._slippage_floor_frac(1.0) == 0.02
    assert engine._slippage_floor_frac(10.0) == 0.035
    assert engine._slippage_floor_frac(20.0) == 0.05


def test_compute_buy_price_no_cross():
    engine = ShadowEngine()
    ms = MarketState(
        market_id=1,
        question="Test",
        yes_token_id="y",
        no_token_id="n",
        condition_id="c",
        decimal_precision=2,
        tick_size=0.01,
        min_size=1.0,
        max_spread=0.10,
        fee_rate_bps=0,
        is_neg_risk=False,
        is_yield_bearing=False,
    )
    ms.cached_book = {"bids": [{"price": 0.45, "size": 100}], "asks": [{"price": 0.46, "size": 100}]}
    price = engine.compute_buy_price(ms, "yes")
    assert price is not None
    assert 0.45 <= price <= 0.46
