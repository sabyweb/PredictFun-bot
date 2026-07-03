"""Order builder and signer for Predict.Fun using predict-sdk.

This module converts our internal price/shares into the CLOB order format,
signs EIP-712 typed data, and returns a payload ready for POST /v1/orders.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from predict_sdk import (
    BuildOrderInput,
    ChainId,
    LimitHelperInput,
    OrderBuilder,
    OrderBuilderOptions,
    Side,
    SignatureType,
)

from config import get_config
from models import MarketState

log = logging.getLogger("predict_fun")


def _price_to_wei(price: float, precision: int = 18) -> int:
    """Convert human price to wei-like integer using the configured precision."""
    return int(Decimal(str(price)) * (10 ** precision))


def _shares_to_wei(shares: float, precision: int = 18) -> int:
    """Convert human shares to wei-like integer."""
    return int(Decimal(str(shares)) * (10 ** precision))


class OrderSigner:
    """Wraps predict-sdk OrderBuilder for EOA signing."""

    def __init__(self, private_key: str | None = None, chain_id: ChainId = ChainId.BNB_MAINNET):
        self.config = get_config()
        self.private_key = (private_key or self.config.private_key or "").strip().strip("'\"")
        if not self.private_key:
            raise RuntimeError("PREDICT_FUN_PRIVATE_KEY is required for signing")
        self.builder = OrderBuilder.make(
            chain_id=chain_id,
            signer=self.private_key,
            options=OrderBuilderOptions(precision=18),
        )
        self.address = self.builder._signer.address if self.builder._signer else None
        log.info(f"OrderSigner initialized for {self.address}")

    def build_signed_order(
        self,
        ms: MarketState,
        side: str,
        price: float,
        shares: float,
        *,
        expires_minutes: int = 60,
    ) -> dict | None:
        """Build and sign a LIMIT order. Returns the POST /v1/orders payload.

        For the paper-trading phase we only trade the YES outcome. The Predict.Fun
        book is priced in YES, so YES buy/sell maps directly to BUY/SELL of the
        YES token.
        """
        try:
            sdk_side = Side.BUY if side == "buy" else Side.SELL
            token_id = ms.yes_token_id

            price_wei = _price_to_wei(price)
            qty_wei = _shares_to_wei(shares)

            if qty_wei < int(1e16):
                log.warning(f"Quantity too small: {shares} shares")
                return None

            amounts = self.builder.get_limit_order_amounts(
                LimitHelperInput(
                    side=sdk_side,
                    price_per_share_wei=price_wei,
                    quantity_wei=qty_wei,
                )
            )

            expires_at = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
            input_data = BuildOrderInput(
                side=sdk_side,
                token_id=token_id,
                maker_amount=amounts.maker_amount,
                taker_amount=amounts.taker_amount,
                fee_rate_bps=ms.fee_rate_bps,
                signer=self.address,
                maker=self.address,
                expires_at=expires_at,
                signature_type=SignatureType.EOA,
            )

            order = self.builder.build_order("LIMIT", input_data)
            typed_data = self.builder.build_typed_data(
                order,
                is_neg_risk=ms.is_neg_risk,
                is_yield_bearing=ms.is_yield_bearing,
            )
            signed = self.builder.sign_typed_data_order(typed_data)

            return {
                "marketId": ms.market_id,
                "side": "BUY" if sdk_side == Side.BUY else "SELL",
                "type": "LIMIT",
                "pricePerShare": str(price_wei),
                "shares": str(qty_wei),
                "salt": signed.salt,
                "maker": signed.maker,
                "signer": signed.signer,
                "taker": signed.taker,
                "tokenId": signed.token_id,
                "makerAmount": signed.maker_amount,
                "takerAmount": signed.taker_amount,
                "expiration": signed.expiration,
                "nonce": signed.nonce,
                "feeRateBps": signed.fee_rate_bps,
                "signatureType": int(signed.signature_type),
                "signature": signed.signature,
            }
        except Exception as e:
            log.exception(f"Failed to build signed order: {e}")
            return None
