"""Market discovery for Predict.Fun.

Fetches markets, enriches them, verifies orderbooks, and normalizes to a
YES-equivalent book.
"""

import logging
import time
from typing import Any

from predict_client import PredictFunClient
from models import MarketState

log = logging.getLogger("predict_fun")


def _outcome_token_ids(outcomes: list[dict[str, Any]]) -> tuple[str, str]:
    """Return (yes_token_id, no_token_id) from outcome list.

    Predict.Fun orderbooks are priced for the first outcome (often called Yes,
    Up, Long, etc.). We map the first outcome to our YES side and the second to
    NO for binary markets.
    """
    yes = outcomes[0]["onChainId"]
    no = outcomes[1]["onChainId"]
    return yes, no


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def _tick_size(decimal_precision: int) -> float:
    """Smallest price increment given decimal precision."""
    return 10 ** (-decimal_precision)


def normalize_book(raw_book: dict[str, Any]) -> dict[str, list[dict[str, float]]]:
    """Convert Predict.Fun orderbook to YES-equivalent {bids, asks} floats.

    The API nests the book under `data` and returns each level as
    `[price, size]` lists.
    """
    bids: list[dict[str, float]] = []
    asks: list[dict[str, float]] = []
    data = raw_book.get("data", raw_book)
    for side, entries in [("bids", data.get("bids", [])), ("asks", data.get("asks", []))]:
        for entry in entries:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price = _to_float(entry[0])
                size = _to_float(entry[1])
            elif isinstance(entry, dict):
                price = _to_float(entry.get("price"))
                size = _to_float(entry.get("size"))
            else:
                continue
            if price <= 0 or size <= 0:
                continue
            if side == "bids":
                bids.append({"price": price, "size": size})
            else:
                asks.append({"price": price, "size": size})
    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])
    return {"bids": bids, "asks": asks}


def midpoint_from_book(book: dict[str, list[dict[str, float]]]) -> float:
    best_bid = book["bids"][0]["price"] if book["bids"] else 0.0
    best_ask = book["asks"][0]["price"] if book["asks"] else 0.0
    if best_bid > 0 and best_ask > 0:
        return round((best_bid + best_ask) / 2.0, 6)
    if best_bid > 0:
        return best_bid
    if best_ask > 0:
        return best_ask
    return 0.0


class MarketDiscovery:
    def __init__(self, client: PredictFunClient):
        self.client = client

    async def fetch_candidate_markets(self, max_pages: int = 5) -> list[MarketState]:
        """Fetch open binary markets with basic filters."""
        markets: list[MarketState] = []
        after: str | None = None
        page = 0
        while page < max_pages:
            resp = await self.client.get_markets(
                first=self.client.config.markets_page_size,
                after=after,
                status="OPEN",
            )
            data = resp.get("data", [])
            for m in data:
                try:
                    ms = self._to_market_state(m)
                    if ms:
                        markets.append(ms)
                except Exception as e:
                    log.debug(f"Skipping market {m.get('id')}: {e}")
            after = resp.get("cursor")
            if not after or not data:
                break
            page += 1
        log.info(f"Fetched {len(markets)} candidate markets")
        return markets

    def _to_market_state(self, raw: dict[str, Any]) -> MarketState | None:
        trading_status = raw.get("tradingStatus", "")
        if trading_status != "OPEN":
            return None
        outcomes = raw.get("outcomes", [])
        if len(outcomes) < 2:
            return None
        yes_tid, no_tid = _outcome_token_ids(outcomes)
        decimal_precision = int(raw.get("decimalPrecision", 2))
        tick = _tick_size(decimal_precision)

        # Spread / min size from market config if present.
        spread_threshold = _to_float(raw.get("spreadThreshold"))
        share_threshold = _to_float(raw.get("shareThreshold"))

        rewards = raw.get("rewards") or {}
        daily_rate = _to_float(rewards.get("daily") if isinstance(rewards, dict) else 0)

        stats = raw.get("stats") or {}
        yes_price = _to_float(stats.get("yesPrice")) if isinstance(stats, dict) else None
        if yes_price is None or yes_price <= 0:
            yes_price = None

        return MarketState(
            market_id=int(raw["id"]),
            question=raw.get("question") or raw.get("title", ""),
            yes_token_id=yes_tid,
            no_token_id=no_tid,
            condition_id=raw.get("conditionId", ""),
            decimal_precision=decimal_precision,
            tick_size=tick,
            min_size=share_threshold if share_threshold > 0 else 1.0,
            max_spread=spread_threshold if spread_threshold > 0 else 0.05,
            fee_rate_bps=int(raw.get("feeRateBps", 0)),
            is_neg_risk=bool(raw.get("isNegRisk", False)),
            is_yield_bearing=bool(raw.get("isYieldBearing", False)),
            status=raw.get("status", ""),
            trading_status=trading_status,
            end_date_iso=raw.get("resolution"),
            game_start_time=raw.get("createdAt"),
            yes_price=yes_price,
        )

    async def enrich_with_book(self, ms: MarketState) -> bool:
        """Fetch orderbook and compute midpoint. Returns True if book is usable."""
        try:
            raw_book = await self.client.get_orderbook(ms.market_id)
            book = normalize_book(raw_book)
            ms.cached_book = book
            ms.midpoint = midpoint_from_book(book)
            if ms.yes_price is None and ms.midpoint > 0:
                ms.yes_price = ms.midpoint
            return bool(book["bids"] and book["asks"])
        except Exception as e:
            log.debug(f"Book fetch failed for {ms.market_id}: {e}")
            return False
