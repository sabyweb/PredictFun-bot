"""Configuration loader for PredictFun bot.

Mirrors the Polymarket BotConfig pattern: environment variables override
module-level defaults.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Literal

from dotenv import load_dotenv

load_dotenv()


DEFAULT_BASE_URL = "https://api.predict.fun"
DEFAULT_WS_URL = "wss://ws.predict.fun/ws"
Mode = Literal["SHADOW", "PAPER", "LIVE"]


def _parse_mode(value: str) -> Mode:
    v = value.upper()
    if v in {"SHADOW", "PAPER", "LIVE"}:
        return v  # type: ignore[return-value]
    return "SHADOW"


@dataclass
class BotConfig:
    """Runtime configuration."""

    api_key: str = field(default_factory=lambda: os.environ.get("PREDICT_FUN_API_KEY", ""))
    private_key: str | None = field(default_factory=lambda: os.environ.get("PREDICT_FUN_PRIVATE_KEY") or None)
    predict_account: str | None = field(default_factory=lambda: os.environ.get("PREDICT_FUN_PREDICT_ACCOUNT") or None)
    base_url: str = DEFAULT_BASE_URL
    ws_url: str = DEFAULT_WS_URL
    mode: Mode = field(default_factory=lambda: _parse_mode(os.environ.get("PREDICT_FUN_MODE", "SHADOW")))

    # Rate limits (per user instructions). Separate buckets for General and Trading.
    general_rpm: int = 240
    trading_rpm: int = 500
    min_call_interval: float = 0.12  # seconds between calls (conservative)
    max_retries: int = 3
    base_backoff: float = 1.0

    # WS
    ws_heartbeat_interval: float = 15.0
    ws_reconnect_delay: float = 5.0
    ws_max_reconnect_delay: float = 60.0

    # Markets
    default_fee_rate_bps: int = 0
    markets_page_size: int = 100

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError("PREDICT_FUN_API_KEY is required")
        return self.api_key


_cfg: BotConfig | None = None


def get_config() -> BotConfig:
    global _cfg
    if _cfg is None:
        _cfg = BotConfig()
    return _cfg


def cfg(name: str, default: Any = None) -> Any:
    """Dot-access helper mirroring Polymarket cfg()."""
    c = get_config()
    return getattr(c, name, default)
