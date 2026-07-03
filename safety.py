"""Safety guardrails for Predict.Fun live trading.

Mirrors the Polymarket bot kill switch, notional caps, spread checks, and
pre-flight order validation.
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Mode, get_config
from models import MarketState

log = logging.getLogger("predict_fun")

DEFAULT_KILL_FILE = "kill_state.json"


@dataclass
class GuardrailConfig:
    max_notional_per_order_usdt: float = 10.0
    max_total_notional_usdt: float = 100.0
    max_open_orders: int = 20
    max_spread: float = 0.10
    min_price: float = 0.01
    max_price: float = 0.99
    require_confirmation_for_live: bool = True
    max_slippage_for_market: float = 0.05


class SafetyGuard:
    """Central safety controller."""

    def __init__(self, guard_config: GuardrailConfig | None = None, kill_file: str = DEFAULT_KILL_FILE):
        self.cfg = guard_config or GuardrailConfig()
        self.kill_file = Path(kill_file)
        self._open_orders: list[dict[str, Any]] = []
        self._total_notional: float = 0.0

    def is_killed(self) -> tuple[bool, str | None]:
        if not self.kill_file.exists():
            return False, None
        try:
            data = json.loads(self.kill_file.read_text())
            if data.get("killed"):
                return True, data.get("reason", "manual kill")
        except Exception as e:
            log.warning(f"Failed to read kill file: {e}")
        return False, None

    def kill(self, reason: str = "manual") -> None:
        self.kill_file.write_text(json.dumps({"killed": True, "reason": reason, "at": time.time()}))
        log.warning(f"KILL SWITCH ACTIVATED: {reason}")

    def clear_kill(self) -> None:
        if self.kill_file.exists():
            self.kill_file.write_text(json.dumps({"killed": False, "at": time.time()}))

    def check_mode(self, mode: Mode) -> bool:
        if mode not in {"SHADOW", "PAPER", "LIVE"}:
            log.error(f"Invalid mode: {mode}")
            return False
        return True

    def validate_order(
        self,
        mode: Mode,
        ms: MarketState,
        side: str,
        price: float,
        shares: float,
        *,
        confirmed_live: bool = False,
    ) -> tuple[bool, str | None]:
        """Return (ok, reason) after running all safety checks."""
        killed, reason = self.is_killed()
        if killed:
            return False, f"kill switch active: {reason}"

        if mode == "LIVE" and self.cfg.require_confirmation_for_live and not confirmed_live:
            return False, "LIVE mode requires explicit --confirm-live flag"

        if side not in {"buy", "sell"}:
            return False, f"invalid side: {side}"

        if price < self.cfg.min_price or price > self.cfg.max_price:
            return False, f"price {price:.4f} outside allowed range [{self.cfg.min_price}, {self.cfg.max_price}]"

        notional = price * shares
        if notional > self.cfg.max_notional_per_order_usdt:
            return False, f"order notional {notional:.4f} > max {self.cfg.max_notional_per_order_usdt}"

        if self._total_notional + notional > self.cfg.max_total_notional_usdt:
            return False, f"total notional would exceed {self.cfg.max_total_notional_usdt}"

        if len(self._open_orders) >= self.cfg.max_open_orders:
            return False, f"max open orders reached: {self.cfg.max_open_orders}"

        book = ms.cached_book
        if book and book["bids"] and book["asks"]:
            spread = book["asks"][0]["price"] - book["bids"][0]["price"]
            if spread > self.cfg.max_spread:
                return False, f"spread {spread:.4f} > max {self.cfg.max_spread}"

        return True, None

    def record_intended_order(self, mode: Mode, ms: MarketState, side: str, price: float, shares: float) -> None:
        if mode == "LIVE":
            self._open_orders.append({
                "market_id": ms.market_id,
                "side": side,
                "price": price,
                "shares": shares,
                "at": time.time(),
            })
            self._total_notional += price * shares

    def summary(self) -> dict[str, Any]:
        killed, reason = self.is_killed()
        return {
            "killed": killed,
            "kill_reason": reason,
            "open_orders": len(self._open_orders),
            "total_notional": self._total_notional,
        }
