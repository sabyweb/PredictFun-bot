# Predict.Fun Bot Hardening Audit

**Date:** 2026-07-03  
**Scope:** `predict_fun_bot` mainnet shadow dry run phase  
**Commit:** `96dd30e` (before fixes in this audit)  
**Auditor:** Kimi Code CLI

## Executive Summary

The Predict.Fun bot was built for a **mainnet shadow / read-only dry run**.
No real orders are placed. The audit focused on:

1. Preventing accidental live orders in shadow mode.
2. Rate-limit handling.
3. WebSocket reconnect safety.
4. Data parsing robustness.
5. Test coverage of safety invariants.

**Verdict:** After the fixes below, the bot is safe for continued shadow
operation. It must **not** be run in live mode without adding JWT auth, order
signing via `predict-sdk`, on-chain approvals, and additional kill switches.

## Findings & Fixes

### 1. WebSocket reconnect could spawn nested receive loops (FIXED)

**Risk:** `PredictFunWebSocket._reconnect()` called `self.connect()`, which
created a new `_receive_loop()` task every reconnect. Over time this could
spawn an unbounded number of receive tasks and amplify reconnect/load.

**Fix:** Refactored `_reconnect()` to reuse the existing receive loop. It now
only re-establishes the underlying `websockets.connect()` and re-subscribes.
`connect()` only starts a new receive task if the current one is absent or
already done.

**Test added:** `tests/test_shadow_safety.py` now covers the absence of
placement/cancel methods; WS reconnect unit test deferred to live-phase wiring.

### 2. No order-placement methods exposed yet (OK for shadow)

**Observation:** `PredictFunClient` only has GET helpers (`get_markets`,
`get_orderbook`, etc.) and a JWT auth stub. There is no `create_order` or
`cancel_order` method. This makes accidental live order placement impossible
from the current REST client.

**Action:** Added an explicit test asserting these methods do not exist.
When live mode is implemented, these methods must be gated by mode checks
and reviewed in a follow-up audit.

### 3. run_shadow.py only issues GET requests (VERIFIED)

**Observation:** The shadow runner calls `get_markets()` and `get_orderbook()`
only. The safety test mocks `_request` and confirms all recorded requests are
GETs.

**Evidence from dry run:**
- General bucket: 10 calls
- Trading bucket: 346 calls (all `GET /v1/markets/{id}/orderbook`)
- Zero POST/DELETE/PATCH requests.

### 4. Rate-limit tracking is bucket-aware (OK)

**Observation:** The client classifies endpoints into General and Trading
buckets per Predict.Fun's documented limits (240 vs 500 RPM). It parses
`ratelimit-*` headers and backs off on 429. The dry run never hit a limit.

**Recommendation for live phase:** Enable `x-burst-enabled: true` only after
measuring sustained load; burst is not for normal operation.

### 5. Orderbook parsing initially rejected valid responses (FIXED)

**Risk:** `normalize_book()` expected `{price, size}` dicts, but Predict.Fun
returns `[[price, size], ...]` arrays. This caused all books to appear empty.

**Fix:** Updated `normalize_book()` to handle both list-tuple and dict
formats, and to unwrap the `data` wrapper.

### 6. Market status filter used wrong field (FIXED)

**Risk:** The discovery code filtered on `status == "OPEN"`, but Predict.Fun
uses `tradingStatus == "OPEN"` and `status == "REGISTERED"`. This filtered out
all tradeable markets.

**Fix:** Filter on `tradingStatus == "OPEN"`.

### 7. Outcome name mapping was brittle (FIXED)

**Risk:** The code looked for outcome names "Yes"/"No", but Predict.Fun uses
"Up"/"Down", "Long"/"Short", etc.

**Fix:** For binary markets, map `outcomes[0]` to the YES side and
`outcomes[1]` to the NO side, since Predict.Fun books are always priced in the
first outcome.

### 8. No kill switch / circuit breaker yet (ACCEPTABLE for shadow)

**Risk:** The shadow runner has no persistent kill switch. In shadow mode this
only affects data fetching, not real funds.

**Recommendation:** Add a `kill_state` file and a mode check before any live
order methods are introduced.

### 9. Private key / JWT not required yet (OK)

**Observation:** Shadow mode uses only public endpoints. The `.env` file
contains the API key but no private key. This is correct for the current phase.

**Recommendation:** When adding live trading, store `PREDICT_FUN_PRIVATE_KEY`
in `.env`, implement the `/v1/auth` JWT flow, and never log the private key or
JWT.

## Test Results

```
$ python -m pytest tests/ -q
6 passed in 0.05s
```

Tests cover:
- No order-placement methods on the client.
- `run_shadow.py` only issues GET requests.
- Rate-limit header parsing.
- Orderbook normalization.
- Time-bucketed slippage floor.
- Buy-price computation.

## Dry Run Results

```
30 markets evaluated
10 cycles
600 simulated BUY placements
0 real orders sent
0 fills / 0 dumps (market did not cross simulated bid)
Average spread: 0.004
Max spread: 0.034
Rate-limit usage: 10 general / 346 trading (well below limits)
```

Full report: `shadow_report.json`

## Remaining Work Before Live Mode

1. Implement JWT auth (`GET /v1/auth/message`, `POST /v1/auth`).
2. Add `create_order` / `cancel_order` methods using `predict-sdk` for EIP-712
   signing.
3. Add on-chain approval checks and USDT/ERC-1155 allowance management.
4. Confirm Predict.Fun reward-share API or formula; the current planner is a
   placeholder.
5. Add persistent kill switch, notional caps, and drawdown guards.
6. Add WebSocket wallet-event subscription for real-time fill detection.
7. Add integration tests against testnet before mainnet live trading.
