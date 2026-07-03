"""JWT authentication for Predict.Fun.

Predict.Fun trading/account endpoints require a JWT obtained by signing an
auth message. In EOA mode the EOA signs directly. In Predict Account mode
(Privy smart wallet) the EOA owner signs on behalf of the smart wallet.
"""

import logging
import time
from typing import Any

from eth_account import Account
from predict_sdk import ChainId, OrderBuilder, OrderBuilderOptions

from config import get_config
from predict_client import PredictFunClient

log = logging.getLogger("predict_fun")


class PredictFunAuth:
    """Manages signed JWT for Predict.Fun private endpoints."""

    def __init__(
        self,
        client: PredictFunClient | None = None,
        private_key: str | None = None,
        predict_account: str | None = None,
    ):
        self.config = get_config()
        self.client = client or PredictFunClient()
        self.private_key = (private_key or self.config.private_key or "").strip().strip("'\"")
        self.predict_account = (predict_account or self.config.predict_account or "").strip()
        self._account: Account | None = None
        self._jwt: str | None = None
        self._expires_at: float = 0.0
        self._order_builder: OrderBuilder | None = None
        if self.private_key:
            try:
                self._account = Account.from_key(self.private_key)
            except Exception as e:
                raise RuntimeError(f"Invalid PREDICT_FUN_PRIVATE_KEY format: {e}") from e
        if self.predict_account:
            log.info(f"Predict Account mode: smart wallet={self.predict_account}, EOA={self._account.address if self._account else None}")
            self._order_builder = OrderBuilder.make(
                chain_id=ChainId.BNB_MAINNET,
                signer=self.private_key,
                options=OrderBuilderOptions(predict_account=self.predict_account),
            )
        elif self._account:
            log.info(f"PredictFunAuth initialized for EOA {self._account.address}")

    @property
    def address(self) -> str | None:
        return self.predict_account or (self._account.address if self._account else None)

    @property
    def eoa_address(self) -> str | None:
        return self._account.address if self._account else None

    async def get_jwt(self, force_refresh: bool = False) -> str | None:
        """Return a valid JWT, refreshing if needed."""
        if self._jwt and not force_refresh and time.time() < self._expires_at - 60:
            return self._jwt
        if not self._account:
            log.warning("No private key configured; cannot authenticate")
            return None
        return await self._refresh_jwt()

    async def _refresh_jwt(self) -> str | None:
        log.info("Refreshing Predict.Fun JWT")
        try:
            msg_resp = await self.client.get_auth_message()
            message = msg_resp.get("data", {}).get("message") if "data" in msg_resp else msg_resp.get("message")
            if not message:
                log.error("Auth message missing from /v1/auth/message")
                return None

            signer = self.predict_account or self._account.address
            signature = self._sign_message(message)
            auth_resp = await self.client.post_auth(
                signer=signer,
                message=message,
                signature=signature,
            )
            token = auth_resp.get("data", {}).get("token") if "data" in auth_resp else auth_resp.get("token")
            expires_in = auth_resp.get("data", {}).get("expiresIn") if "data" in auth_resp else auth_resp.get("expiresIn")
            if not token:
                log.error("JWT token missing from /v1/auth response")
                return None

            self._jwt = token
            self._expires_at = time.time() + (expires_in if isinstance(expires_in, (int, float)) else 86400)
            log.info("Predict.Fun JWT refreshed")
            return token
        except Exception as e:
            log.exception(f"Failed to refresh JWT: {e}")
            return None

    def _sign_message(self, message: str) -> str:
        if self._order_builder:
            return self._order_builder.sign_predict_account_message(message)
        # Fallback to EOA EIP-191 signing.
        from eth_account.messages import encode_defunct
        signable = encode_defunct(text=message)
        signed = self._account.sign_message(signable)
        return signed.signature.hex()
