#!/usr/bin/env bash
# Deploys wayfarer-vault to NEAR TESTNET. Prototype, unaudited. Usage: scripts/deploy_vault.sh <owner.testnet> <agent_public_key>
# Needs: `near` CLI, the owner's key in the legacy keychain (~/.near-credentials), and a built wasm.
set -euo pipefail
OWNER=${1:?owner account, e.g. you.testnet}; AGENT_PK=${2:?agent public key ed25519:...}; V=vault.$OWNER
cd "$(dirname "$0")/../contracts/wayfarer-vault"
cargo build --target wasm32-unknown-unknown --release
NET="network-config testnet sign-with-legacy-keychain send"
# the contract needs ~4.5 NEAR of storage for its 450 KB wasm, plus a little spare; fund generously
near account create-account fund-myself "$V" '8 NEAR' autogenerate-new-keypair save-to-legacy-keychain sign-as "$OWNER" $NET
near contract deploy "$V" use-file target/wasm32-unknown-unknown/release/wayfarer_vault.wasm with-init-call new json-args "{\"owner_id\":\"$OWNER\"}" prepaid-gas '30 Tgas' attached-deposit '0 NEAR' $NET
near contract call-function as-transaction "$V" set_policy json-args '{"token_id":"near","per_tx_max":"500000000000000000000000","daily_max":"1000000000000000000000000"}' prepaid-gas '30 Tgas' attached-deposit '0 NEAR' sign-as "$OWNER" $NET
near contract call-function as-transaction "$V" add_agent json-args "{\"public_key\":\"$AGENT_PK\",\"gas_allowance_near\":\"500000000000000000000000\"}" prepaid-gas '30 Tgas' attached-deposit '0 NEAR' sign-as "$OWNER" $NET
echo "Deployed $V. Keep the agent private key OUT of the repo."
