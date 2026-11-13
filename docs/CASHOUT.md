# Merchant cash-out: NEAR -> USDC -> bank (13 Nov 2026)

**Status: the orchestration is built and tested with fakes; the sandbox path runs for anyone; the real NEAR Intents leg and any real bank rail have NOT been run.** Nothing here has moved real money.

## Flow
`CashOutService.quote()` reads back one sentence (NEAR in, estimated naira out, fee, the USDC floor the estimate is based on, which rail, "estimate", "nothing happens until you confirm"). After the merchant confirms, `execute()` runs the swap leg, and only if it was **delivered** does it ask the rail to pay, sized on the swap's guaranteed minimum (`min_usdc_out`), never on the optimistic estimate.

| Result | Meaning |
|---|---|
| `paid` | Swap delivered and the rail paid |
| `swap_pending` | The swap has no final state yet. Do not retry blindly; no payout was made |
| `swap_failed` | Refunded, failed, or never sent. No payout was made |
| `payout_pending` | Swap delivered; the rail is still processing |
| `usdc_held` | Swap delivered but the payout failed. The USDC is at the destination; follow up with the reference |

The payout is idempotent on a reference (derived from the sale), so a retry cannot pay twice.

## Implementations
| Piece | Real? | Notes |
|---|---|---|
| `SandboxSwapLeg` | Fake | Demo `usd_per_near` price and fee. Nothing is swapped. Labelled in every read-back |
| `SandboxRail` | Fake | Validates a 10-digit account number and bank code, applies a demo NGN/USD rate and fee, writes a local JSON ledger (`no real money moved`, only a masked account). Runs for anyone, no keys |
| `IntentsSwapLeg` + `NearWrapFunder` | Real code, **never run** | Quotes NEAR (as `wrap.near`) -> USDC through 1Click, wraps and funds the deposit address in one transaction (storage registration, `near_deposit`, `ft_transfer`), follows the swap status. **Mainnet only** (1Click has no testnet; `refundTo` must be a mainnet account). Unit-tested with fakes; the transaction layout is compared byte for byte with the same action builders |
| A real bank rail | **Not built** | Needs a licensed partner. Wayfarer does not custody or convert fiat |

## Honest limits
- The swap leg sizes the payout on 1Click's `minAmountOut`; the real amount delivered may be higher. The surplus is not paid out by this code.
- The rail's FX rate and fee are whatever the rail says; the sandbox's are made-up demo numbers (NGN 1,500 per USD, 1% fee).
- If the funding transaction's result cannot be confirmed (network error), the leg reports `PENDING`, never "nothing happened".
- A real run needs: a mainnet merchant account holding NEAR, explicit approval of the amount, a real rail. Any mainnet spend needs the owner's go-ahead.
