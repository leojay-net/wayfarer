# How Wayfarer uses NEAR

Pitch: **a NEAR-native spending-policy layer for agents.** Whatever the agent is (a Telegram voice bot today, anything speaking to the contract tomorrow), money only moves through an on-chain policy that the user owns. The voice bot is the first client.

| NEAR piece | Role in Wayfarer | State today |
|---|---|---|
| **NEAR AI Cloud** (TEE-hosted inference) | Whisper transcribes the voice note; an LLM turns the transcript into a structured payment intent. Code: `src/wayfarer/nearai.py`, `src/wayfarer/intent.py`. | **Runs live** (transcription of one synthetic note; 12/12 on a small self-written intent set; one tool-call sample). Note: Telegram itself sees the voice note before it reaches the TEE. |
| **Smart contract** (`wayfarer-vault`) | Per-token caps, allow-list, pause, replay protection, receipts as events. | **Deployed to NEAR testnet** (`vault.wayfarer-vault-11121649.testnet`; native-NEAR path exercised, with an accepted and a refused payment); 20 unit + 2 sandbox tests. Not on mainnet, not audited. |
| **Function-call access keys** | The agent's key is restricted by the NEAR runtime to two payment methods; revocable instantly. | **Verified on sandbox and testnet** (the runtime rejected the agent's attempt to call `set_policy`). |
| **NEAR Intents 1Click** | Cross-chain, any-token settlement via a deposit address. | **Dry quote only** (`wayfarer quote`, `src/wayfarer/oneclick.py`; nothing executed; gaps in INTENTS.md). No payment has been made through it; the contract path `pay_via_intents` exists but has not been exercised against a real deposit address. |
| **NEP-297 events** | Machine-readable receipts (`payment_authorized`, `payment_settled`, `payment_failed`, ...). | Emitted and unit-tested. |

## Not used (do not claim)
Chain Signatures, NEAR names/social, a gasless relayer (meta-transactions), an MCP server, TEE signatures over audio, mainnet. These are possible extensions, not features.

## Why this is infrastructure and not a demo
1. **Separation of intelligence and authority.** The model proposes; the chain disposes. Any agent framework can sit on top of the same vault.
2. **Auditable by default.** Every authorisation is an on-chain event with a hash that binds it to the user's instruction.
3. **Revocable in one transaction.** Deleting the key stops the agent immediately, with no cooperation from the agent or the bot operator.
4. **Private by construction where it matters.** Inference runs inside NEAR AI Cloud's TEE rather than on a third-party model API.

## What it would take to be production grade (honest roadmap, none of it built)
Third-party audit; real FT-path and failure-path tests on testnet; verifying the 1Click quote signature on-chain to bind the final recipient; multisig / timelocked owner; per-recipient and per-period policies; an MCP server and SDK so other agents can integrate; a relayer for gasless onboarding.
