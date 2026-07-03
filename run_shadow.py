"""Mainnet shadow dry run for Predict.Fun.

Fetches markets, evaluates orderbooks, simulates BUY placements/dumps, and
reports market quality. Never places real orders.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from config import get_config
from market_discovery import MarketDiscovery
from models import MarketState
from predict_client import PredictFunClient
from shadow_engine import ShadowEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("predict_fun")


class ShadowRunner:
    def __init__(self, max_markets: int = 50, cycles: int = 10, cycle_interval: float = 30.0):
        self.config = get_config()
        self.client = PredictFunClient()
        self.discovery = MarketDiscovery(self.client)
        self.engine = ShadowEngine()
        self.max_markets = max_markets
        self.cycles = cycles
        self.cycle_interval = cycle_interval
        self.report: dict[str, Any] = {
            "started_at": time.time(),
            "cycles": [],
            "summary": {},
        }

    async def run(self) -> dict[str, Any]:
        log.info("=== Predict.Fun mainnet shadow dry run starting ===")
        log.info(f"Base URL: {self.config.base_url}")
        log.info(f"Mode: SHADOW (no real orders)")

        candidates = await self.discovery.fetch_candidate_markets(max_pages=10)
        # Select the first N markets with a tight, two-sided book.
        enriched: list[MarketState] = []
        for ms in candidates:
            if len(enriched) >= self.max_markets:
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
                    log.debug(f"Skipping {ms.market_id}: spread {spread:.4f} > allowed {allowed_spread:.4f}")
                    continue
                enriched.append(ms)
            except Exception as e:
                log.debug(f"Book enrichment failed for {ms.market_id}: {e}")

        log.info(f"Selected {len(enriched)} markets with tight two-sided books")

        for cycle in range(1, self.cycles + 1):
            cycle_start = time.time()
            log.info(f"--- Cycle {cycle}/{self.cycles} ---")
            cycle_report = await self._cycle(enriched)
            self.report["cycles"].append(cycle_report)
            elapsed = time.time() - cycle_start
            if cycle < self.cycles:
                sleep_for = max(0.0, self.cycle_interval - elapsed)
                log.info(f"Sleeping {sleep_for:.1f}s until next cycle")
                await asyncio.sleep(sleep_for)

        self.report["ended_at"] = time.time()
        self.report["rate_limits"] = self.client.rate_limit_summary()
        self._summarize()
        return self.report

    async def _cycle(self, markets: list[MarketState]) -> dict[str, Any]:
        placements = 0
        simulated_fills = 0
        simulated_dumps = 0
        spreads: list[float] = []

        # Refresh books.
        for ms in markets:
            try:
                await self.discovery.enrich_with_book(ms)
            except Exception as e:
                log.debug(f"Book refresh failed for {ms.market_id}: {e}")
                continue

            book = ms.cached_book
            if book and book["bids"] and book["asks"]:
                spread = round(book["asks"][0]["price"] - book["bids"][0]["price"], ms.decimal_precision)
                spreads.append(spread)

            # Try to place on both sides (shadow only).
            for side in ("yes", "no"):
                order = self.engine.place_shadow_order(ms, side, shares=ms.min_size)
                if order:
                    placements += 1

            # Simulate fills.
            fills = self.engine.simulate_fill(ms)
            simulated_fills += len(fills)

            # Simulate dumps for any filled positions.
            for side in ("yes", "no"):
                if self.engine.simulate_dump(ms, side):
                    simulated_dumps += 1

        return {
            "timestamp": time.time(),
            "markets": len(markets),
            "placements": placements,
            "fills": simulated_fills,
            "dumps": simulated_dumps,
            "avg_spread": round(sum(spreads) / len(spreads), 4) if spreads else None,
            "max_spread": round(max(spreads), 4) if spreads else None,
        }

    def _summarize(self) -> None:
        totals = {"markets": 0, "placements": 0, "fills": 0, "dumps": 0}
        for c in self.report["cycles"]:
            for k in totals:
                totals[k] += c.get(k, 0)
        totals["cycles"] = len(self.report["cycles"])
        self.report["summary"] = totals
        log.info("=== Shadow dry run summary ===")
        log.info(json.dumps(totals, indent=2))
        log.info(f"Rate-limit usage: {json.dumps(self.report['rate_limits'], indent=2)}")

    async def close(self):
        await self.client.close()


def save_report(report: dict[str, Any], path: str = "shadow_report.json") -> None:
    # Convert any non-serializable values.
    out = json.dumps(report, indent=2, default=str)
    Path(path).write_text(out)
    log.info(f"Report saved to {path}")


async def main():
    parser = argparse.ArgumentParser(description="Predict.Fun mainnet shadow dry run")
    parser.add_argument("--markets", type=int, default=30, help="Max markets to evaluate")
    parser.add_argument("--cycles", type=int, default=10, help="Number of cycles")
    parser.add_argument("--interval", type=float, default=30.0, help="Seconds between cycles")
    parser.add_argument("--report", type=str, default="shadow_report.json", help="Report output path")
    args = parser.parse_args()

    cfg = get_config()
    if not cfg.api_key:
        log.error("PREDICT_FUN_API_KEY is required")
        sys.exit(1)
    if cfg.mode != "SHADOW":
        log.warning(f"Mode is {cfg.mode}; run_shadow.py requires SHADOW mode")
        sys.exit(1)

    runner = ShadowRunner(max_markets=args.markets, cycles=args.cycles, cycle_interval=args.interval)
    try:
        report = await runner.run()
        save_report(report, args.report)
    finally:
        await runner.close()


if __name__ == "__main__":
    asyncio.run(main())
