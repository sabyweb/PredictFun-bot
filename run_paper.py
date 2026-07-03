"""Mainnet paper-trading runner for Predict.Fun.

Builds and signs real orders using the configured private key, but intercepts
POST /v1/orders and only logs the payload. No real orders are sent.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from auth import PredictFunAuth
from config import Mode, get_config
from market_discovery import MarketDiscovery
from models import MarketState
from order_signer import OrderSigner
from predict_client import PredictFunClient
from shadow_engine import ShadowEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("predict_fun")


class PaperTrader:
    def __init__(self, max_markets: int = 5, orders_per_market: int = 1):
        self.config = get_config()
        if self.config.mode != "PAPER":
            raise RuntimeError(f"run_paper.py requires mode=PAPER, got {self.config.mode}")
        if not self.config.private_key:
            raise RuntimeError("PREDICT_FUN_PRIVATE_KEY is required for paper trading")

        self.client = PredictFunClient()
        self.auth = PredictFunAuth(client=self.client, private_key=self.config.private_key)
        self.client.auth = self.auth
        self.discovery = MarketDiscovery(self.client)
        self.signer = OrderSigner(private_key=self.config.private_key)
        self.engine = ShadowEngine()
        self.max_markets = max_markets
        self.orders_per_market = orders_per_market
        self.report: dict[str, Any] = {
            "started_at": time.time(),
            "mode": "PAPER",
            "address": self.auth.address,
            "signed_orders": [],
        }

    async def run(self) -> dict[str, Any]:
        log.info("=== Predict.Fun mainnet PAPER trading starting ===")
        log.info(f"Address: {self.auth.address}")

        # Authenticate if possible. In paper mode this is optional because we
        # intercept the order POST anyway, but validating auth proves the key
        # matches a Predict.Fun-registered address.
        jwt = await self.auth.get_jwt()
        if jwt:
            try:
                account = await self.client.get_account()
                log.info(f"Account data keys: {list(account.get('data', account).keys())}")
            except Exception as e:
                log.warning(f"Could not fetch account (non-fatal for paper): {e}")
        else:
            log.warning("JWT auth failed. Continuing in paper mode without account verification.")
            log.warning("For live trading, the EOA address must be registered/deposited on Predict.Fun.")

        markets = await self._select_markets()
        log.info(f"Paper trading on {len(markets)} markets")

        for ms in markets:
            for _ in range(self.orders_per_market):
                # YES buy at one tick inside best bid.
                price = self.engine.compute_buy_price(ms, "yes")
                if price is None:
                    continue
                shares = max(ms.min_size, 1.0)
                payload = self.signer.build_signed_order(ms, "buy", price, shares)
                if not payload:
                    continue
                try:
                    resp = await self.client.create_order(payload)
                    self.report["signed_orders"].append({
                        "market_id": ms.market_id,
                        "question": ms.question,
                        "side": "BUY",
                        "price": price,
                        "shares": shares,
                        "paper_id": resp.get("data", {}).get("id"),
                    })
                except Exception as e:
                    log.exception(f"Paper order failed for {ms.market_id}: {e}")

        self.report["ended_at"] = time.time()
        self.report["rate_limits"] = self.client.rate_limit_summary()
        log.info("=== Paper trading summary ===")
        log.info(json.dumps({"signed_orders": len(self.report["signed_orders"])}, indent=2))
        return self.report

    async def _select_markets(self) -> list[MarketState]:
        candidates = await self.discovery.fetch_candidate_markets(max_pages=10)
        selected: list[MarketState] = []
        for ms in candidates:
            if len(selected) >= self.max_markets:
                break
            try:
                if not await self.discovery.enrich_with_book(ms):
                    continue
                book = ms.cached_book
                if not (book and book["bids"] and book["asks"]):
                    continue
                spread = round(book["asks"][0]["price"] - book["bids"][0]["price"], ms.decimal_precision)
                allowed_spread = max(ms.max_spread, 0.10)
                if spread > allowed_spread:
                    continue
                selected.append(ms)
            except Exception as e:
                log.debug(f"Skipping {ms.market_id}: {e}")
        return selected

    async def close(self):
        await self.client.close()


def save_report(report: dict[str, Any], path: str = "paper_report.json") -> None:
    Path(path).write_text(json.dumps(report, indent=2, default=str))
    log.info(f"Report saved to {path}")


async def main():
    parser = argparse.ArgumentParser(description="Predict.Fun mainnet paper trading")
    parser.add_argument("--markets", type=int, default=5, help="Max markets")
    parser.add_argument("--orders-per-market", type=int, default=1, help="Orders per market")
    parser.add_argument("--report", type=str, default="paper_report.json")
    args = parser.parse_args()

    cfg = get_config()
    if cfg.mode != "PAPER":
        log.error("Set PREDICT_FUN_MODE=PAPER in .env to run paper trading")
        sys.exit(1)
    if not cfg.api_key or not cfg.private_key:
        log.error("PREDICT_FUN_API_KEY and PREDICT_FUN_PRIVATE_KEY are required")
        sys.exit(1)

    trader = PaperTrader(max_markets=args.markets, orders_per_market=args.orders_per_market)
    try:
        report = await trader.run()
        save_report(report, args.report)
    finally:
        await trader.close()


if __name__ == "__main__":
    asyncio.run(main())
