# Day 0 findings (Mon 9 Nov 2026)

Method: NEAR docs pages (fetched 9 Nov), the 1Click OpenAPI spec, and live calls. Status labels: **VERIFIED** = I called it or read it in the official docs/spec; **UNVERIFIED** = not confirmed; **FALSE** = docs contradict the plan.

## 1. NEAR AI Cloud
| Question | Answer | Status |
|---|---|---|
| Base URL | `https://cloud-api.near.ai/v1` (OpenAI-compatible) | VERIFIED (docs quickstart) |
| Auth | `Authorization: Bearer <API key>`, key created in the cloud.near.ai dashboard, "API Keys" | VERIFIED (docs) |
| Speech endpoint | `POST /v1/audio/transcriptions`, multipart, model `openai/whisper-large-v3` | VERIFIED (docs); not yet called, needs a key |
| **Language hint** | **Yes.** Optional `language` field (ISO code such as `en`). No `prompt` or `temperature` field. Also `response_format` (`json`/`text`/`verbose_json`), `timestamp_granularities`. Response includes detected `language` and `duration`. | VERIFIED (API reference). The quickstart-style guide omits it; the API reference has it. |
| Audio formats / size | MP3, WAV, WEBM, FLAC, OGG, M4A; max 25 MB. Telegram voice notes are OGG/Opus, so **ffmpeg conversion is probably not needed**, but this is unconfirmed until a real call. | Docs VERIFIED; OGG/Opus acceptance UNVERIFIED |
| Chat model ids | Only `zai-org/GLM-5.1-FP8` appeared in the quickstart (stale, see section 10). `/v1/models` is the real catalog but returned HTTP 500 (database pool error) when I called it unauthenticated, so I could not list models. Other ids in the docs are non-chat: `Qwen/Qwen3-VL-30B-A3B-Instruct` (vision via chat), `Qwen/Qwen3-Embedding-0.6B`, `openai/privacy-filter`, FLUX image model. | Partly VERIFIED |
| Tool calling on the default model | Models advertise `tools` / `structured_outputs` features via `/v1/models`; I could not read it. | UNVERIFIED |
| Hausa / Yoruba / Igbo quality | Unknown. Whisper large-v3 lists Hausa, Yoruba and others as supported languages in general, but quality on short phone audio must be measured on our own test set. | UNVERIFIED, must be measured |
| TEE signatures | `GET /v1/signature/<chat_id>?signing_algo=ecdsa\|ed25519` documented for chat completions and the Responses API. The docs I read do not mention audio, so **a signed transcript for the receipt is not assured**; the LLM intent-extraction step can be signed. | VERIFIED for chat; UNVERIFIED for audio |

## 2. NEAR Intents 1Click
| Question | Answer | Status |
|---|---|---|
| Base URL | `https://1click.chaindefuser.com` | VERIFIED (docs + live) |
| Endpoints | `GET /v0/tokens`, `POST /v0/quote`, `POST /v0/deposit/submit`, `GET /v0/status?depositAddress=...`. Status values: `PENDING_DEPOSIT, KNOWN_DEPOSIT_TX, PROCESSING, SUCCESS, INCOMPLETE_DEPOSIT, REFUNDED, FAILED`. | VERIFIED (docs; tokens and quote called live, status returns 404 for an unknown address as expected) |
| Auth | **Not required for quotes.** A `dry:true` quote returned HTTP 201 with no key. Optional partner JWT from partners.near-intents.org, sent as `X-API-Key` (Bearer also accepted, marked legacy). Without it an extra **0.25%** fee applies. Authenticated is "typically 20 bps, or 1 bp on stablecoin / same-asset routes". | VERIFIED |
| **Testnet** | **None documented**; docs never mention one, the quote hit mainnet, and token ids are mainnet assets. Treat as mainnet-only. | VERIFIED as "not documented"; plan for tiny real amounts |
| Quote shape | Returns `amountIn/Out(+Usd)`, `minAmountOut`, `timeEstimate` (s), `refundFee`, `withdrawFee`, `signature`. A live 5 USDC (Base) -> NEAR-chain USDC dry quote gave 4.994876 out and ~35 s. That is one sample, not a benchmark. | VERIFIED (one call) |
| Gotchas | `refundTo` must be a valid address for the origin chain (a lowercase non-checksummed EVM address was rejected with `refundTo is not valid`). Python's default User-Agent got a non-JSON response; the script sets its own. A non-dry quote returns a `depositAddress` the user must fund; the agent cannot "pull" funds. | VERIFIED |
| Intent-signing flow | Spec also lists `generate-intent` / `submit-intent` routes for signed-intent settlement from an Intents balance (`depositType: INTENTS`). Not yet read in detail. | UNVERIFIED, read on Day 2 |

## 3. Relayer / gasless
NEAR protocol supports meta-transactions (NEP-366 delegate actions, NEP-461 signing prefix): user signs off-chain, a relayer submits and pays gas, and relayers can whitelist which contracts they sponsor. Official docs point to an open-source Rust relayer. So gasless for **our contract's calls only** is feasible, but we would run our own relayer. There is no hosted relayer I verified. Status: protocol support VERIFIED (docs), hosted service UNVERIFIED; keep this "Should", cut first.

## 4. Telegram format
Telegram voice notes are OGG/Opus. NEAR AI Cloud lists OGG as supported. Test file `testdata/send-20-usdc-en.ogg` (macOS `say` + ffmpeg, **synthetic speech, not a real speaker**) is in the repo for the script. ffmpeg is on this machine if conversion turns out to be needed.

## 5. Plan corrections
- Plan section 5.1 asked whether Whisper takes a language hint: **yes**.
- Plan section 5.2 "testnet": **none**; mainnet with tiny amounts, label stub paths.
- Receipts: do not promise TEE-signed audio transcripts until confirmed.

## 7. Open items needing the user
1. `NEAR_AI_API_KEY` (cloud.near.ai) to run the ASR step and check tool calling on a chat model.
2. Optional `ONECLICK_JWT` (partners.near-intents.org) to avoid the 0.25% fee.
3. A NEAR account name (testnet for the vault contract, mainnet for Intents tests) and a small mainnet budget.
4. Real Hausa / Yoruba / Igbo / Pidgin voice samples (the synthetic test file is English only).

## 8. How to run the Day 0 script
```
export NEAR_AI_API_KEY=...            # never commit
python3 scripts/day0_check.py --audio testdata/send-20-usdc-en.ogg --lang en
python3 scripts/day0_check.py --quote-only    # no key needed; always dry=true
```
Result so far: the quote step ran live (HTTP 201). The transcription step has **not** been run (no key yet).

## 9. Day 1 progress (10 Nov)
Written: `webapp/wayfarer.py`, `webapp/telegram_bot.py`, `webapp/tests/test_wayfarer.py` (6 tests, mocked network, passing), `scripts/intent_eval.py` + `testdata/intents.jsonl` (12 text cases).
Not done: nothing has run against the live NEAR AI Cloud or Telegram (no keys). **No pass rate exists yet.** (An early accidental run without a key printed "4/12"; that was every case failing on the missing key, not a result. The script now refuses to run without a key and does not count upstream errors.)

## 10. Live checks with a real key (12 Nov)
- NEAR AI Cloud key is valid; `GET /v1/models` (authenticated) returns 64 models including `openai/whisper-large-v3`. **The quickstart's example model `zai-org/GLM-5.1-FP8` is not in the live catalog**; all defaults changed to `z-ai/glm-5.3-flash` (catalog lists tools, structured_outputs, json_mode, reasoning). Docs examples can be stale; trust `/v1/models`.
- (Superseded by section 11: a second key with credits works.) **Both transcription and chat returned HTTP 402 `no_limit_configured` ("No spending limit configured. Please contact support to set up credits.")**: the account needs credits / a spending limit before any call works. So ASR, tool calling and the intent pass rate are still unmeasured.
- Telegram bot token is valid (`getMe` ok). The bot loop itself has not been run.
- A second 1Click dry quote worked again (HTTP 201).
- The key and bot token were pasted in chat; they are stored only in the gitignored `.env`. Rotate both after the hackathon.

## 11. Live results with a funded key (12 Nov)
- Chat works on `z-ai/glm-5.3-flash`. It is a reasoning model: with a 20-token limit it returned `content: null` (all tokens spent on reasoning). NEAR docs say it has no off switch; use `"reasoning_effort": "low"` (not `enable_thinking:false`, which moves reasoning into `content`). Code now sends `reasoning_effort: low`, `max_tokens: 800`, and errors cleanly on an empty answer.
- Whisper (`openai/whisper-large-v3`) with `language=en`: HTTP 200; OGG/Opus accepted directly, no ffmpeg. Output for the synthetic note: `Send 20 USDC to ada.near.` (detected `en`, 2.67 s audio). One clean synthetic sample only.
- Tool calling: one request with a `now` tool returned a correct `tool_calls` response. Single sample.
- `scripts/intent_eval.py`: **12/12** on our 12 hand-written text cases (8 send, 4 must-refuse), including 3 AI-written non-English lines. Small, self-authored set; do not generalise.
- Not yet done: Telegram end-to-end with a human voice, any non-synthetic audio, TEE signature fetch for audio (docs do not list audio), settlement.

## 12. Vault contract (12 Nov)
`contracts/wayfarer-vault`: 20 unit tests + 2 NEAR-sandbox tests pass (sandbox: real function-call key, real balances; agent paid within limits, was refused over per-tx and daily caps, could not replay an intent id or call `set_policy`; after `revoke_agent` its key was gone and payments failed). Gotcha: without `--cfg near` the wasm hides panic messages (bare `unreachable`); fixed in `.cargo/config.toml`. Not yet done: testnet deploy, real FT/USDC path, `pay_via_intents` against a real 1Click deposit address, Python signer. See docs/VAULT.md.

## 13. Testnet deploy (12 Nov)
Vault deployed to `vault.wayfarer-vault-11121649.testnet`; agent paid 0.3 NEAR, a 0.6 NEAR payment was refused by the contract, and `set_policy` was rejected by the access key. Details and transaction ids: docs/TESTNET-DEPLOYMENT.md. An earlier deploy attempt failed for insufficient storage balance (fixed by funding the account with 7 NEAR total).

## 14. Whisper hang (12 Nov, ~18:50-19:05 WAT)
Real Telegram voice notes (OGG/Opus, 5 s) downloaded fine (23 KB, 1.4 s) but `/v1/audio/transcriptions` never answered within 120 s. The same endpoint answered the synthetic clip in about a second earlier that day, then also hung on it (2 runs x 121 s; WAV 16 kHz mono and OGG both hung at 45-60 s with curl). Chat completions answered in 4.8 s throughout. `whisper-large-v3` is the only audio-input model in the catalog. Fixes on our side: 45 s ASR timeout with a clear error, and a typed-instruction fallback. Root cause on NEAR's side is unknown.
