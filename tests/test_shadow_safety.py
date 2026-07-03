"""Safety invariants for the Predict.Fun shadow dry run."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from predict_client import PredictFunClient
from shadow_engine import ShadowEngine
from models import MarketState


def test_no_create_or_cancel_methods_on_client():
    """The REST client must not expose order-placement methods in shadow phase."""
    client = PredictFunClient()
    assert not hasattr(client, "create_order")
    assert not hasattr(client, "cancel_order")


def test_run_shadow_only_uses_get_requests():
    """run_shadow.py must only issue GET requests to public endpoints."""
    import asyncio
    from run_shadow import ShadowRunner

    async def run():
        methods = []
        runner = ShadowRunner(max_markets=2, cycles=1, cycle_interval=0)

        async def capture(method, path, **kwargs):
            methods.append((method, path))
            if "/orderbook" in path:
                return {"data": {"bids": [[0.45, 100]], "asks": [[0.46, 100]]}}
            if "/v1/markets/" in path:
                return {
                    "data": {
                        "id": 1,
                        "question": "Test",
                        "decimalPrecision": 2,
                        "feeRateBps": 0,
                        "isNegRisk": False,
                        "isYieldBearing": False,
                        "status": "REGISTERED",
                        "tradingStatus": "OPEN",
                        "outcomes": [
                            {"name": "Yes", "onChainId": "1"},
                            {"name": "No", "onChainId": "2"},
                        ],
                    },
                    "success": True,
                }
            return {
                "data": [
                    {
                        "id": 1,
                        "question": "Test",
                        "decimalPrecision": 2,
                        "feeRateBps": 0,
                        "isNegRisk": False,
                        "isYieldBearing": False,
                        "status": "REGISTERED",
                        "tradingStatus": "OPEN",
                        "outcomes": [
                            {"name": "Yes", "onChainId": "1"},
                            {"name": "No", "onChainId": "2"},
                        ],
                    }
                ],
                "cursor": None,
                "success": True,
            }

        with patch.object(runner.client, "_request", new=capture):
            try:
                await runner.run()
            except Exception:
                pass  # empty data is fine for this invariant test
        return methods

    methods = asyncio.run(run())
    assert all(m == "GET" for m, _ in methods), f"Non-GET methods detected: {methods}"
    assert any("/v1/markets" in p for _, p in methods)
    assert any("/orderbook" in p for _, p in methods)


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
