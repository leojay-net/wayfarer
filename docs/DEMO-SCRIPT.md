# Demo script (target about 100 s, under the 2-minute limit)

Rule: show only what runs. Say "testnet" and "simulated phones" out loud. Do not mention mainnet, a real bank payout, or live FX rates. If you have two Android phones and have run the two-phone test yourself (not done yet), use Option B; otherwise use Option A and say the phones are simulated.

**Before recording:** the testnet accounts of `docs/TESTNET-PQ.md` exist and the ML-DSA seeds are in `~/.near-credentials/wayfarer-pq/`; the explorer tab and `verifier/` page (`python3 -m http.server -d verifier 8000`) are open; terminal font large.

## Option A: terminal + verifier (everything here has been run)
| Time | On screen | Say |
|---|---|---|
| 0:00 | Title card | "Wayfarer: spend naira, hold NEAR. A NEAR testnet prototype with a quantum-safe, mandate-bounded payment." |
| 0:08 | `python3 scripts/tap_demo.py --naira 500 --testnet 9bda7c --save /tmp/receipt`, step 1-2 | "The merchant prices NGN 500 at a demo rate and locks it. Both phones are simulated in this script." |
| 0:25 | Steps 3-4: confirm prompt, `5359 bytes in 6 QR frames; frames [1, 4] missed` | "The buyer confirms; the phone signs a cheque with ML-DSA-65. It crosses as QR frames, two were lost and re-scanned. The merchant checks it offline: accepted, pending settlement, not paid." |
| 0:45 | Step 5: strong check ok, `settlement: PAID` + explorer link; open the link, show `payment_authorized` / `payment_settled` | "The vault contract verified the post-quantum signature on-chain and enforced the mandate. Only now is it paid." |
| 1:05 | Step 6: the 20 `[ok]` lines, then paste `/tmp/receipt/receipt.json` into the verifier page, **Check against the chain** | "The receipt links the request, the signature, the owner's signed mandate and the chain events. This page checks them in the browser." |
| 1:25 | Step 7 (sandbox cash-out line) | "Cash-out to a bank is a sandbox: no swap, no money moves." |
| 1:35 | `docs/TESTNET-PQ.md` refusals table | "The contract refused replays, over-cap, forged, expired and revoked cheques." End card: "Built at NEAR HACKATHON. Testnet, unaudited." |

## Option B: two phones (only after you have run this yourself)
Follow `docs/TWO-PHONE-TEST.md`. Merchant app: enter amount, **Create payment request**. Buyer app: hold phones together, **Approve**, hold again. Merchant: "ACCEPTED, PENDING SETTLEMENT", **Share the accepted cheque**, then `python3 scripts/settle_cheque.py accepted.json --suffix 9bda7c --save out/` on screen, then the verifier page. Register the buyer's key first with `scripts/add_phone_key.py`.

Honest caveats for the video description or X post: testnet only; phones simulated unless you ran Option B; rate typed in by hand; cash-out is a sandbox; offline acceptance is not final; unaudited.
