"""Validate the private key format without exposing it."""

import re
from config import get_config


def validate():
    cfg = get_config()
    key = (cfg.private_key or "").strip()
    if not key:
        print("ERROR: PREDICT_FUN_PRIVATE_KEY is not set in .env")
        return

    print(f"Key length before cleanup: {len(key)} chars")

    # Check for common mistakes without printing the key.
    if key.startswith("'") or key.endswith("'"):
        print("ERROR: Key has single quotes around it. Remove the quotes.")
    if key.startswith('"') or key.endswith('"'):
        print("ERROR: Key has double quotes around it. Remove the quotes.")
    if " " in key:
        print("ERROR: Key contains spaces. It must be one continuous hex string.")
    if len(key.replace("0x", "")) not in (64, 66):
        print(f"ERROR: Key has wrong length ({len(key.replace('0x', ''))} hex chars). Expected 64 hex chars (or 66 with 0x prefix).")

    hex_only = key[2:] if key.startswith("0x") else key
    if not re.fullmatch(r"[0-9a-fA-F]{64}", hex_only):
        print("ERROR: Key contains non-hexadecimal characters.")
        # Show what kind of characters without revealing the key.
        bad = {c for c in hex_only if not (c.isdigit() or c.lower() in "abcdef")}
        print(f"Invalid characters found: {sorted(bad)}")
    else:
        print("OK: Key format looks correct.")


if __name__ == "__main__":
    validate()
