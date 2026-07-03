"""Mainnet LIVE trading runner for Predict.Fun.

THIS SENDS REAL ORDERS WITH REAL USDT. Use only after paper trading succeeds
and all safety checks pass.
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
from on_chain import OnChainChecker
from order_signer import OrderSigner
from predict_client import PredictFunClient
from safety import GuardrailConfig, SafetyGuard
from shadow_engine import ShadowEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("predict_fun")


class LiveTrader:
    def __init__(
        self,
        max_markets: int = 2,
        orders_per_market: int = 1,
        max_notional_per_order: float = 5.0,
    ):
        self.config = get_config()
        if self.config.mode != "LIVE":
            raise RuntimeError(f"run_live.py requires mode=LIVE, got {self.config.mode}")
        if not self.config.private_key:
            raise RuntimeError("PREDICT_FUN_PRIVATE_KEY is required for live trading")

        self.client = PredictFunClient()
        self.auth = PredictFunAuth(client=self.client, private_key=self.config.private_key)
        self.client.auth = self.auth
        self.discovery = MarketDiscovery(self.client)
        self.signer = OrderSigner(private_key=self.config.private_key)
        self.engine = ShadowEngine()
        self.guard = SafetyGuard(
            GuardrailConfig(
                max_notional_per_order_usdt=max_notional_per_order,
                max_total_notional_usdt=max(100.0, max_notional_per_order * 2),
                require_confirmation_for_live=True,
            )
        )
        self.chain = OnChainChecker()
        self.max_markets = max_markets
        self.orders_per_market = orders_per_market
        self.report: dict[str, Any] = {
            "started_at": time.time(),
            "mode": "LIVE",
            "address": self.auth.address,
            "orders": [],
        }

    async def run(self, confirmed_live: bool = False) -> dict[str, Any]:
        log.info("=== Predict.Fun mainnet LIVE trading starting ===")
        log.info(f"Address: {self.auth.address}")
        log.warning("REAL ORDERS WILL BE SENT. THIS IS NOT A DRILL.")

        killed, reason = self.guard.is_killed()
        if killed:
            log.error(f"Kill switch active: {reason}")
            sys.exit(1)

        jwt = await self.auth.get_jwt()
        if not jwt:
            log.error("JWT auth failed. Cannot place live orders.")
            sys.exit(1)

        chain_summary = self.chain.summary()
        log.info(f"On-chain summary: {json.dumps(chain_summary, indent=2)}")
        if chain_summary["usdt_balance"] <= 0:
            log.error("No USDT balance detected. Aborting live run.")
            sys.exit(1)

        markets = await self._select_markets()
        log.info(f"Live trading on {len(markets)} markets")

        for ms in markets:
            for _ in range(self.orders_per_market):
                price = self.engine.compute_buy_price(ms, "yes")
                if price is None:
                    continue
                target_shares = self.guard.cfg.max_notional_per_order_usdt / max(price, 0.001)
                shares = max(ms.min_size, 1.0)
                shares = min(shares, target_shares)

                ok, reason = self.guard.validate_order(
                    "LIVE", ms, "buy", price, shares, confirmed_live=confirmed_live
                )
                if not ok:
                    log.warning(f"Safety blocked order for {ms.market_id}: {reason}")
                    continue

                preflight = self.chain.preflight_buy_check(
                    ms.is_neg_risk, ms.is_yield_bearing, price, shares
                )
                if not preflight["can_place"]:
                    log.error(f"On-chain preflight failed: {preflight['reason']}")
                    continue

                payload = self.signer.build_signed_order(ms, "buy", price, shares)
                if not payload:
                    continue
                try:
                    resp = await self.client.create_order(payload)
                    self.guard.record_intended_order("LIVE", ms, "buy", price, shares)
                    self.report["orders"].append({
                        "market_id": ms.market_id,
                        "question": ms.question,
                        "side": "BUY",
                        "price": price,
                        "shares": shares,
                        "order_id": resp.get("data", {}).get("id"),
                        "preflight": preflight,
                    })
                    log.info(f"LIVE order placed: {resp}")
                except Exception as e:
                    log.exception(f"Live order failed for {ms.market_id}: {e}")

        self.report["ended_at"] = time.time()
        self.report["rate_limits"] = self.client.rate_limit_summary()
        self.report["safety_summary"] = self.guard.summary()
        log.info("=== Live trading summary ===")
        log.info(json.dumps({
            "orders": len(self.report["orders"]),
            "safety": self.report["safety_summary"],
        }, indent=2, default=str))
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
                allowed_spread = max(ms.max_spread, 0.05)
                if spread > allowed_spread:
                    continue
                selected.append(ms)
            except Exception as e:
                log.debug(f"Skipping {ms.market_id}: {e}")
        return selected

    async def close(self):
        await self.client.close()


def save_report(report: dict[str, Any], path: str = "live_report.json") -> None:
    Path(path).write_text(json.dumps(report, indent=2, default=str))
    log.info(f"Report saved to {path}")


async def main():
    parser = argparse.ArgumentParser(description="Predict.Fun mainnet LIVE trading")
    parser.add_argument("--markets", type=int, default=2, help="Max markets")
    parser.add_argument("--orders-per-market", type=int, default=1, help="Orders per market")
    parser.add_argument("--max-notional", type=float, default=5.0, help="Max USDT per order")
    parser.add_argument("--confirm-live", action="store_true", help="Required to send real orders")
    parser.add_argument("--report", type=str, default="live_report.json")
    args = parser.parse_args()

    cfg = get_config()
    if cfg.mode != "LIVE":
        log.error("Set PREDICT_FUN_MODE=LIVE in .env to run live trading")
        sys.exit(1)
    if not cfg.api_key or not cfg.private_key:
        log.error("PREDICT_FUN_API_KEY and PREDICT_FUN_PRIVATE_KEY are required")
        sys.exit(1)
    if not args.confirm_live:
        log.error("You must pass --confirm-live to send real orders")
        sys.exit(1)

    trader = LiveTrader(
        max_markets=args.markets,
        orders_per_market=args.orders_per_market,
        max_notional_per_order=args.max_notional,
    )
    try:
        report = await trader.run(confirmed_live=args.confirm_live)
        save_report(report, args.report)
    finally:
        await trader.close()


if __name__ == "__main__":
    asyncio.run(main())
