"""Approve USDT for the Predict.Fun CTF exchange.

For EOA accounts this sends a normal approve transaction.
For Predict Account (Privy smart wallet) this uses predict-sdk's set_approvals
which builds and submits ERC-4337 user operations.
"""

import argparse
import logging
import sys

from predict_sdk import ChainId, OrderBuilder, OrderBuilderOptions

from config import get_config
from on_chain import OnChainChecker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("predict_fun")


def approve_usdt(chain_id: ChainId = ChainId.BNB_MAINNET):
    cfg = get_config()
    if not cfg.private_key:
        raise RuntimeError("PREDICT_FUN_PRIVATE_KEY is required")

    checker = OnChainChecker(private_key=cfg.private_key, chain_id=chain_id)
    w3 = checker._w3

    if not cfg.predict_account:
        # EOA mode: simple approve for all exchange variants.
        bnb = w3.eth.get_balance(checker.eoa_address) / 1e18
        if bnb < 0.0001:
            raise RuntimeError(f"Insufficient BNB for gas: {bnb}. Send ~0.01 BNB to {checker.eoa_address}")

        from predict_sdk.abis import ERC20_ABI
        usdt_contract = w3.eth.contract(address=checker.addresses.USDT, abi=ERC20_ABI)
        amount_wei = int(10000 * 1e18)  # generous approval

        exchanges = []
        for nr in (False, True):
            for yb in (False, True):
                exchanges.append(checker.ctf_exchange_for_market(nr, yb))
        exchanges = sorted(set(exchanges))

        for exchange in exchanges:
            nonce = w3.eth.get_transaction_count(checker.eoa_address)
            tx = usdt_contract.functions.approve(exchange, amount_wei).build_transaction({
                "from": checker.eoa_address,
                "nonce": nonce,
                "gas": 100000,
                "gasPrice": w3.to_wei("3", "gwei"),
                "chainId": chain_id,
            })
            signed = w3.eth.account.sign_transaction(tx, cfg.private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt["status"] != 1:
                raise RuntimeError(f"Approve failed: {tx_hash.hex()}")
            log.info(f"Approved {exchange}: tx={tx_hash.hex()}")
        return

    # Predict Account mode: use predict-sdk's set_approvals.
    log.info(f"Using Predict Account {cfg.predict_account}")
    builder = OrderBuilder.make(
        chain_id=chain_id,
        signer=cfg.private_key,
        options=OrderBuilderOptions(predict_account=cfg.predict_account),
    )
    result = builder.set_approvals()
    if not result.success:
        raise RuntimeError(f"set_approvals failed: {result}")
    for tx_result in result.transactions:
        log.info(f"Approval tx: {tx_result}")


def main():
    parser = argparse.ArgumentParser(description="Approve USDT for Predict.Fun CTF exchanges")
    args = parser.parse_args()

    try:
        approve_usdt()
        print("Approvals complete")
    except Exception as e:
        log.error(f"Approval failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
