//! Spike (plan step 1a): can a restricted function-call key sign a NEP-366 `DelegateAction` that calls the vault's
//! `pay_direct` with the vault as sender and a different account as relayer, and does `only_agent` see the delegate key?
//! The transaction is built with nearcore's own types (`near-primitives`) so this is independent of our Python encoder.
//!
//! FINDING (recorded in docs/SPIKE-DELEGATE.md): the protocol accepts the delegate action and the call reaches the
//! contract with `predecessor == vault`, but `signer_account_id` / `signer_account_pk` are the RELAYER'S, not the
//! delegate key's. So the v1 `only_agent` key check can never pass on this path. These tests pin that behaviour for the
//! v1 contract; contract v2 (plan step 3) changes `only_agent` and extends them to a successful payment.
//! Run: cargo build --target wasm32-unknown-unknown --release && cargo test --test delegate
use near_crypto::{InMemorySigner, SecretKey as NearSecretKey};
use near_primitives::action::delegate::{DelegateAction, NonDelegateAction, SignedDelegateAction};
use near_primitives::action::{Action, FunctionCallAction};
use near_primitives::hash::CryptoHash;
use near_primitives::transaction::{SignedTransaction, Transaction, TransactionV0};
use near_workspaces::types::NearToken;
use near_workspaces::{Account, Contract};
use serde_json::{json, Value};
use std::str::FromStr;

mod common;
use common::setup;

async fn rpc(url: &str, method: &str, params: Value) -> anyhow::Result<Value> {
    let body = json!({"jsonrpc": "2.0", "id": "t", "method": method, "params": params});
    let reply: Value = reqwest::Client::new()
        .post(url)
        .json(&body)
        .send()
        .await?
        .json()
        .await?;
    Ok(reply)
}

async fn access_key_nonce(url: &str, account: &str, public_key: &str) -> anyhow::Result<u64> {
    let r = rpc(
        url,
        "query",
        json!({"request_type": "view_access_key", "finality": "optimistic", "account_id": account, "public_key": public_key}),
    )
    .await?;
    r["result"]["nonce"]
        .as_u64()
        .ok_or_else(|| anyhow::anyhow!("no nonce for {account} {public_key}: {r}"))
}

async fn head(url: &str) -> anyhow::Result<(CryptoHash, u64)> {
    let r = rpc(url, "block", json!({"finality": "optimistic"})).await?;
    let hash = CryptoHash::from_str(r["result"]["header"]["hash"].as_str().unwrap())
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    Ok((hash, r["result"]["header"]["height"].as_u64().unwrap()))
}

fn payment(method: &str, intent: &str, to: &Account, near: u128) -> NonDelegateAction {
    let args = json!({
        "intent_id": intent, "token_id": "near", "to": to.id(),
        "amount": NearToken::from_near(near).as_yoctonear().to_string(), "receipt_hash": "wf1:delegate"
    });
    let call = FunctionCallAction {
        method_name: method.to_string(),
        args: args.to_string().into_bytes(),
        gas: near_primitives::types::Gas::from_teragas(100),
        deposit: near_primitives::types::Balance::ZERO,
    };
    NonDelegateAction::try_from(Action::FunctionCall(Box::new(call))).expect("not a delegate")
}

struct Outcome {
    /// True when the outer transaction and every receipt succeeded.
    ok: bool,
    raw: Value,
}

/// The relayer signs and pays for a transaction `relayer -> vault` carrying the signed delegate action.
async fn relay(
    url: &str,
    relayer: &Account,
    vault: &Contract,
    signed: SignedDelegateAction,
) -> anyhow::Result<Outcome> {
    let sk = NearSecretKey::from_str(&relayer.secret_key().to_string())?;
    let signer = InMemorySigner::from_secret_key(relayer.id().as_str().parse()?, sk);
    let (block_hash, _) = head(url).await?;
    let nonce =
        access_key_nonce(url, relayer.id().as_str(), &signer.public_key().to_string()).await? + 1;
    let tx = Transaction::V0(TransactionV0 {
        signer_id: relayer.id().as_str().parse()?,
        public_key: signer.public_key(),
        nonce,
        receiver_id: vault.id().as_str().parse()?,
        block_hash,
        actions: vec![Action::Delegate(Box::new(signed))],
    });
    let (hash, _) = tx.get_hash_and_size();
    let stx = SignedTransaction::new(signer.sign(hash.as_ref()), tx);
    let b64 = near_primitives::serialize::to_base64(&borsh::to_vec(&stx)?);
    let raw = rpc(
        url,
        "broadcast_tx_commit",
        json!({"signed_tx_base64": b64, "wait_until": "FINAL"}),
    )
    .await?;
    let result = &raw["result"];
    let mut ok = result["status"].get("SuccessValue").is_some();
    for r in result["receipts_outcome"].as_array().into_iter().flatten() {
        let o = &r["outcome"];
        ok &= o["status"].get("Failure").is_none();
    }
    Ok(Outcome { ok, raw })
}

/// Build a delegate action for the vault signed with `key`, valid until `max_block_height`.
async fn delegate(
    url: &str,
    vault: &Contract,
    key: &NearSecretKey,
    action: NonDelegateAction,
    nonce_offset: u64,
    max_block_height: u64,
) -> anyhow::Result<SignedDelegateAction> {
    let signer = InMemorySigner::from_secret_key(vault.id().as_str().parse()?, key.clone());
    let nonce = access_key_nonce(url, vault.id().as_str(), &key.public_key().to_string()).await?
        + nonce_offset;
    let action = DelegateAction {
        sender_id: vault.id().as_str().parse()?,
        receiver_id: vault.id().as_str().parse()?,
        actions: vec![action],
        nonce,
        max_block_height,
        public_key: key.public_key(),
    };
    Ok(SignedDelegateAction::sign(&signer, action))
}

/// A delegate action for a method the key is NOT allowed to call must be refused by the runtime itself, before the
/// contract runs: the function-call restriction survives inside NEP-366.
#[tokio::test]
async fn function_call_restriction_holds_inside_a_delegate_action() -> anyhow::Result<()> {
    let (worker, _owner, vault, agent_sk, ada) = setup().await?;
    let url = worker.rpc_addr();
    let relayer = worker.dev_create_account().await?;
    let agent_key = NearSecretKey::from_str(&agent_sk.to_string())?;
    let (_, h) = head(&url).await?;
    let evil = delegate(
        &url,
        &vault,
        &agent_key,
        payment("set_policy", "x", &ada, 1),
        1,
        h + 200,
    )
    .await?;
    assert!(evil.verify());
    let out = relay(&url, &relayer, &vault, evil).await?;
    assert!(
        !out.ok,
        "restricted key must not call set_policy through a delegate action"
    );
    let text = out.raw.to_string();
    println!("RESTRICTION-RESULT {text}");
    assert!(
        text.contains("MethodNameMismatch") && !text.contains("Smart contract panicked"),
        "must be refused by the runtime's access-key check, not by contract code: {text}"
    );
    Ok(())
}

/// v1 FINDING: a correctly signed, in-policy delegate payment is accepted by the protocol and reaches `pay_direct`
/// with the vault as predecessor, but v1 `only_agent` refuses it because the signer key is the relayer's.
#[tokio::test]
async fn v1_refuses_delegate_payment_because_the_signer_key_is_the_relayers() -> anyhow::Result<()>
{
    let (worker, _owner, vault, agent_sk, ada) = setup().await?;
    let url = worker.rpc_addr();
    let relayer = worker.dev_create_account().await?;
    let agent_key = NearSecretKey::from_str(&agent_sk.to_string())?;
    let (_, h) = head(&url).await?;
    let ada_before = ada.view_account().await?.balance;
    let vault_before = vault.view_account().await?.balance;

    let good = delegate(
        &url,
        &vault,
        &agent_key,
        payment("pay_direct", "d1", &ada, 1),
        1,
        h + 200,
    )
    .await?;
    assert!(good.verify(), "delegate signature must verify locally");
    let out = relay(&url, &relayer, &vault, good).await?;
    let text = out.raw.to_string();
    // The outer transaction and the delegate wrapper succeeded; the inner function call reached the contract...
    assert!(
        text.contains("Smart contract panicked: Only a registered agent key can call this"),
        "{text}"
    );
    // ...which refused it, so nothing moved.
    assert!(!out.ok);
    assert_eq!(ada.view_account().await?.balance, ada_before);
    // The vault did not lose funds. (It gains a few micro-NEAR: contracts earn a share of the gas the relayer burns.)
    assert!(vault.view_account().await?.balance >= vault_before);
    Ok(())
}
