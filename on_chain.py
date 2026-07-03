"""On-chain balance and allowance checks for Predict.Fun.

Uses BNB Chain RPC and the contract addresses bundled with predict-sdk.
"""

import logging
from decimal import Decimal
from typing import Any

from eth_account import Account
from predict_sdk import ADDRESSES_BY_CHAIN_ID, ChainId
from predict_sdk.abis import CONDITIONAL_TOKENS_ABI, ERC20_ABI
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from config import get_config

log = logging.getLogger("predict_fun")


class OnChainChecker:
    """Read-only on-chain state for the trading address."""

    def __init__(
        self,
        private_key: str | None = None,
        predict_account: str | None = None,
        chain_id: ChainId = ChainId.BNB_MAINNET,
    ):
        self.config = get_config()
        self.private_key = (private_key or self.config.private_key or "").strip().strip("'\"")
        self.predict_account = (predict_account or self.config.predict_account or "").strip()
        if not self.private_key:
            raise RuntimeError("PREDICT_FUN_PRIVATE_KEY is required for on-chain checks")
        self.account = Account.from_key(self.private_key)
        # The smart wallet is what holds funds and trades; EOA just signs.
        self.address = self.predict_account or self.account.address
        self.eoa_address = self.account.address
        self.chain_id = chain_id
        self.addresses = ADDRESSES_BY_CHAIN_ID[chain_id]
        self._w3 = self._make_w3()
        self._usdt = self._w3.eth.contract(address=self.addresses.USDT, abi=ERC20_ABI)

    def _make_w3(self) -> Web3:
        from predict_sdk.constants import RPC_URLS_BY_CHAIN_ID

        rpc_url = RPC_URLS_BY_CHAIN_ID[self.chain_id]
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        if self.chain_id in (ChainId.BNB_MAINNET, ChainId.BNB_TESTNET):
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        return w3

    def usdt_balance(self) -> float:
        """Return USDT balance in human units (18 decimals on BNB)."""
        raw = self._usdt.functions.balanceOf(self.address).call()
        return float(Decimal(raw) / Decimal(10**18))

    def usdt_allowance(self, spender: str | None = None) -> float:
        """Return USDT allowance for the CTF exchange in human units."""
        spender = spender or self.addresses.CTF_EXCHANGE
        raw = self._usdt.functions.allowance(self.address, spender).call()
        return float(Decimal(raw) / Decimal(10**18))

    def conditional_token_balance(self, token_id: str, token_contract: str | None = None) -> float:
        """Return balance of a conditional outcome token (ERC-1155)."""
        contract_address = token_contract or self.addresses.CONDITIONAL_TOKENS
        contract = self._w3.eth.contract(address=contract_address, abi=CONDITIONAL_TOKENS_ABI)
        raw = contract.functions.balanceOf(self.address, int(token_id)).call()
        return float(Decimal(raw) / Decimal(10**18))

    def ctf_exchange_for_market(self, is_neg_risk: bool, is_yield_bearing: bool) -> str:
        if is_neg_risk:
            if is_yield_bearing:
                return self.addresses.YIELD_BEARING_NEG_RISK_CTF_EXCHANGE
            return self.addresses.NEG_RISK_CTF_EXCHANGE
        if is_yield_bearing:
            return self.addresses.YIELD_BEARING_CTF_EXCHANGE
        return self.addresses.CTF_EXCHANGE

    def preflight_buy_check(self, is_neg_risk: bool, is_yield_bearing: bool, price: float, shares: float) -> dict[str, Any]:
        """Check if a BUY order can be placed safely."""
        exchange = self.ctf_exchange_for_market(is_neg_risk, is_yield_bearing)
        usdt = self.usdt_balance()
        allowance = self.usdt_allowance(exchange)
        required = price * shares
        ok = usdt >= required and allowance >= required
        reason = None
        if usdt < required:
            reason = f"insufficient USDT: {usdt:.4f} < {required:.4f}"
        elif allowance < required:
            reason = f"insufficient allowance: {allowance:.4f} < {required:.4f}"
        return {
            "address": self.address,
            "usdt_balance": usdt,
            "usdt_allowance": allowance,
            "required": required,
            "can_place": ok,
            "reason": reason,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "chain_id": self.chain_id,
            "usdt_balance": self.usdt_balance(),
            "usdt_allowance_ctf": self.usdt_allowance(self.addresses.CTF_EXCHANGE),
        }
