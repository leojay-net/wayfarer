# NEAR Intents in Wayfarer: what was checked, what is built, what is missing

Checked 13 Nov 2026 with live calls to `https://1click.chaindefuser.com` (no funds moved) and the public docs/SDK summaries.

## Verified
- Flow: quote -> (non-dry) deposit address -> someone transfers to it -> optional `POST /v0/deposit/submit` -> poll `GET /v0/status`. Terminal states: `SUCCESS`, `REFUNDED`, `FAILED`.
- A dry quote needs no key. A non-dry quote returns `depositAddress`; it moves nothing and expires at its deadline.
- **A NEAR-origin quote works**: 1 wNEAR (`nep141:wrap.near`) -> USDC on Base priced at 3.400306 USDC, ~24 s (one sample). Its deposit address was a 64-hex implicit NEAR account, which is exactly what the vault's `pay_via_intents(deposit_address: AccountId)` accepts.
- **`refundTo` must be valid on the origin chain's real network**: the testnet vault account was rejected (HTTP 400 "Internal server error"). So real settlement needs a mainnet NEAR account. There is no testnet path.

## Built
- `src/wayfarer/oneclick.py`: `OneClick.quote(dry=True|False)`, `submit_deposit`, `status`; `wayfarer quote`. It never signs or sends a deposit.
- `src/wayfarer/swap.py`: `IntentsSettlement.plan()` (real quote -> read-back text), `execute()` (vault `pay_via_intents` -> `submit_deposit` -> poll status). Outcomes: delivered / refunded / failed / not_sent / pending. "Delivered" needs 1Click `SUCCESS`; a timeout is `pending`, never success. Unit-tested with fakes.
- Contract: `pay_via_intents` now registers the deposit address on the token (`storage_deposit`, registration only, fixed 0.00125 NEAR from the vault balance, outside the policy) before `ft_transfer`. `pay_direct` does not. Sandbox test imports the **real `wrap.near` code** (needs network): `pay_direct` to an unregistered account fails and the budget is restored (`payment_failed`); `pay_via_intents` to a fresh 64-hex address succeeds and the balance arrives. This is a local sandbox, not testnet or mainnet. The deployed testnet vault still runs the **old** wasm until redeployed.

## Still missing before a real Intents payment (do not assume these work)
1. **Mainnet vault** with tiny limits and a funded owner. Needs the user's explicit go-ahead and a stated amount.
2. **Bot wiring:** the intent extractor only understands NEAR-account recipients. Cross-chain ("send 20 USDC to 0x... on Base") needs a destination field, validation per chain, and the swap read-back in the Confirm step. `IntentsSettlement` is not called by the bot yet.
3. **Real run:** nothing here has moved real funds. One sample quote only.
4. **Final recipient is not enforced on-chain** (the deposit address is opaque); it is only recorded in the event.
