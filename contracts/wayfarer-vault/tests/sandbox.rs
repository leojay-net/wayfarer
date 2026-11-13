//! End-to-end tests on a local NEAR sandbox: real runtime, real access keys, real balances.
//! Run: cargo build --target wasm32-unknown-unknown --release && cargo test --test sandbox
use near_workspaces::types::{AccessKeyPermission, NearToken};
use near_workspaces::{Account, Contract};
use serde_json::json;

mod common;
use common::setup;

fn pay(
    agent: &Account,
    vault: &Contract,
    id: &str,
    to: &Account,
    near: u128,
) -> near_workspaces::operations::CallTransaction {
    agent.call(vault.id(), "pay_direct").args_json(json!({
        "intent_id": id, "token_id": "near", "to": to.id(),
        "amount": NearToken::from_near(near).as_yoctonear().to_string(), "receipt_hash": "sha256:test"
    })).gas(near_workspaces::types::Gas::from_tgas(100))
}

#[tokio::test]
async fn agent_key_is_function_call_limited_and_enforces_policy() -> anyhow::Result<()> {
    let (worker, owner, vault, agent_sk, ada) = setup().await?;
    // The agent key on the vault account is a function-call key restricted to the payment methods.
    let keys = vault.as_account().view_access_keys().await?;
    let k = keys
        .iter()
        .find(|k| k.public_key == agent_sk.public_key())
        .expect("agent key present");
    match &k.access_key.permission {
        AccessKeyPermission::FunctionCall(fc) => {
            assert_eq!(
                fc.method_names,
                vec!["pay_direct".to_string(), "pay_via_intents".to_string()]
            );
            assert_eq!(fc.receiver_id, vault.id().to_string());
        }
        other => panic!("agent key must be function-call, got {other:?}"),
    }
    let agent = Account::from_secret_key(vault.id().clone(), agent_sk, &worker);

    // Within limits: pays 1 NEAR.
    let before = ada.view_account().await?.balance;
    pay(&agent, &vault, "t1", &ada, 1)
        .transact()
        .await?
        .into_result()?;
    let after = ada.view_account().await?.balance;
    assert_eq!(
        after.as_yoctonear() - before.as_yoctonear(),
        NearToken::from_near(1).as_yoctonear()
    );

    // Over the per-tx limit (3 > 2): refused, nothing moves.
    let r = pay(&agent, &vault, "t2", &ada, 3).transact().await?;
    assert!(r.is_failure(), "over-limit payment must fail");
    assert!(format!("{:?}", r.clone().into_result().err()).contains("per-transaction limit"));
    assert_eq!(ada.view_account().await?.balance, after);

    // 1 + 2 = 3 spends the daily cap exactly: allowed.
    pay(&agent, &vault, "t3", &ada, 2)
        .transact()
        .await?
        .into_result()?;
    // Anything more today is refused (3 + 1 > 3).
    let r = pay(&agent, &vault, "t5", &ada, 1).transact().await?;
    assert!(
        r.is_failure() && format!("{:?}", r.clone().into_result().err()).contains("daily limit")
    );

    // Replay of a used intent id is refused.
    let r = pay(&agent, &vault, "t1", &ada, 1).transact().await?;
    assert!(r.is_failure());

    // The agent key cannot change policy: the runtime itself rejects non-whitelisted methods.
    let r = agent.call(vault.id(), "set_policy").args_json(json!({"token_id":"near","per_tx_max":"1000000000000000000000000000","daily_max":"1000000000000000000000000000"})).transact().await;
    assert!(r.is_err() || r.unwrap().is_failure());

    // Owner revokes: key disappears and further payments fail.
    owner
        .call(vault.id(), "revoke_agent")
        .args_json(json!({"public_key": agent.secret_key().public_key()}))
        .transact()
        .await?
        .into_result()?;
    assert!(vault
        .as_account()
        .view_access_keys()
        .await?
        .iter()
        .all(|k| k.public_key != agent.secret_key().public_key()));
    let r = pay(&agent, &vault, "t4", &ada, 1).transact().await;
    assert!(
        r.is_err() || r.unwrap().is_failure(),
        "revoked key must not pay"
    );
    Ok(())
}

#[tokio::test]
async fn panic_messages_reach_the_client() -> anyhow::Result<()> {
    let (_w, owner, vault, _sk, _ada) = setup().await?;
    let r = owner
        .call(vault.id(), "set_policy")
        .args_json(json!({"token_id":"near","per_tx_max":"500","daily_max":"100"}))
        .transact()
        .await?;
    let e = r
        .into_result()
        .err()
        .map(|e| e.to_string())
        .unwrap_or_default();
    assert!(e.contains("per_tx_max"), "message lost: {e}");
    Ok(())
}

/// Needs network: imports the real `wrap.near` contract code into the sandbox.
/// 1Click deposit addresses are fresh implicit accounts. A real NEP-141 token rejects `ft_transfer` to an unregistered
/// receiver, so `pay_via_intents` must register the receiver first, while `pay_direct` must not (and must stay safe).
#[tokio::test]
async fn pay_via_intents_registers_a_fresh_deposit_address_on_a_real_nep141_token(
) -> anyhow::Result<()> {
    use near_workspaces::types::Gas;
    let (worker, owner, vault, agent_sk, _ada) = setup().await?;
    let mainnet = near_workspaces::mainnet().await?;
    let wrap_id: near_workspaces::AccountId = "wrap.near".parse()?;
    let wrap = worker
        .import_contract(&wrap_id, &mainnet)
        .initial_balance(NearToken::from_near(2000))
        .transact()
        .await?;

    wrap.call("new").transact().await?.into_result()?;
    // Fund the vault with 5 wNEAR (owner wraps NEAR, registers the vault, transfers).
    let one = NearToken::from_near(1).as_yoctonear().to_string();
    let five = NearToken::from_near(5).as_yoctonear().to_string();
    for who in [owner.id(), vault.id()] {
        owner
            .call(wrap.id(), "storage_deposit")
            .args_json(json!({"account_id": who}))
            .deposit(NearToken::from_millinear(10))
            .transact()
            .await?
            .into_result()?;
    }
    owner
        .call(wrap.id(), "near_deposit")
        .deposit(NearToken::from_near(5))
        .transact()
        .await?
        .into_result()?;
    owner
        .call(wrap.id(), "ft_transfer")
        .args_json(json!({"receiver_id": vault.id(), "amount": five}))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await?
        .into_result()?;

    owner
        .call(vault.id(), "set_policy")
        .args_json(json!({"token_id": "wrap.near", "per_tx_max": five, "daily_max": five}))
        .transact()
        .await?
        .into_result()?;
    let agent = Account::from_secret_key(vault.id().clone(), agent_sk, &worker);
    let deposit_address = "ab".repeat(32);
    let balance_of = |who: String| {
        let wrap = wrap.clone();
        async move {
            wrap.view("ft_balance_of")
                .args_json(json!({"account_id": who}))
                .await
                .map(|r| r.json::<String>().unwrap())
        }
    };

    // pay_direct to an unregistered account fails inside the token; the vault gives the budget back (no funds lost).
    let direct = agent.call(vault.id(), "pay_direct").args_json(json!({"intent_id":"d1","token_id":"wrap.near","to":deposit_address,"amount":one,"receipt_hash":"wf1:x"})).gas(Gas::from_tgas(100)).transact().await?;
    assert_eq!(balance_of(deposit_address.clone()).await?, "0");
    assert!(
        direct.logs().iter().any(|l| l.contains("payment_failed")),
        "failed transfer must be reported: {:?}",
        direct.logs()
    );
    let (spent, _): (String, String) = vault
        .view("get_budget")
        .args_json(json!({"token_id": "wrap.near"}))
        .await?
        .json()?;
    assert_eq!(
        spent, "0",
        "budget must be restored after a failed transfer"
    );

    // pay_via_intents registers the receiver first, then the transfer succeeds.
    let via = agent.call(vault.id(), "pay_via_intents").args_json(json!({"intent_id":"i1","token_id":"wrap.near","deposit_address":deposit_address,"amount":one,"declared_recipient":"0xabc","receipt_hash":"wf1:y"})).gas(Gas::from_tgas(150)).transact().await?;
    assert!(via.is_success(), "{:?}", via.failures());
    assert_eq!(balance_of(deposit_address.clone()).await?, one);
    let (spent, remaining): (String, String) = vault
        .view("get_budget")
        .args_json(json!({"token_id": "wrap.near"}))
        .await?
        .json()?;
    assert_eq!(
        (spent.as_str(), remaining.as_str()),
        (
            one.as_str(),
            NearToken::from_near(4).as_yoctonear().to_string().as_str()
        )
    );
    Ok(())
}

// ---------------------------------------------------------------- v2: ML-DSA-65 cheques in the real runtime

use near_crypto::SecretKey as PqSecretKey;

/// A fresh ML-DSA-65 key from nearcore's own implementation, as (secret key, raw 1,952-byte public key).
fn pq_key() -> (PqSecretKey, Vec<u8>) {
    let mut seed = [0u8; 32];
    for (i, b) in seed.iter_mut().enumerate() {
        *b = (i as u8).wrapping_mul(7).wrapping_add(rand_byte());
    }
    let sk = near_crypto::ml_dsa_65_from_seed(&seed).expect("seed");
    let wire = borsh::to_vec(&sk.public_key()).expect("borsh");
    (sk, wire[1..].to_vec()) // drop the 1-byte key-type tag
}

fn rand_byte() -> u8 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos() as u8)
        .unwrap_or(1)
}

async fn cheque_message(
    vault: &Contract,
    to: &Account,
    near: u128,
    intent: &str,
    expires_at_ms: u64,
) -> anyhow::Result<Vec<u8>> {
    let r = vault
        .view("cheque_message")
        .args_json(json!({
            "intent_id": intent, "token_id": "near", "to": to.id(),
            "amount": NearToken::from_near(near).as_yoctonear().to_string(),
            "expires_at_ms": expires_at_ms.to_string(), "receipt_hash": "wf1:sandbox"
        }))
        .await?;
    let b64: String = r.json()?;
    Ok(base64_decode(&b64))
}

fn base64_decode(s: &str) -> Vec<u8> {
    near_primitives::serialize::from_base64(s).expect("base64")
}

fn pq_sign(sk: &PqSecretKey, message: &[u8]) -> Vec<u8> {
    let sig = sk.sign(message);
    borsh::to_vec(&sig).expect("borsh")[1..].to_vec()
}

#[tokio::test]
async fn pq_cheque_pays_through_any_relayer_and_the_policy_still_binds() -> anyhow::Result<()> {
    use near_workspaces::types::Gas;
    let (worker, owner, vault, _agent_sk, ada) = setup().await?;
    let relayer = worker.dev_create_account().await?;
    let (sk, public) = pq_key();
    let key_id: String = owner
        .call(vault.id(), "add_pq_agent")
        .args_json(json!({"public_key": near_primitives::serialize::to_base64(&public), "expires_at_ms": "0", "min_interval_ms": "0"}))
        .transact()
        .await?
        .into_result()?
        .json()?;
    let far = 4_000_000_000_000u64;
    let call = |intent: &'static str, near: u128, sig: Vec<u8>| {
        let (relayer, vault, ada, key_id) =
            (relayer.clone(), vault.clone(), ada.clone(), key_id.clone());
        async move {
            relayer
                .call(vault.id(), "pay_with_cheque")
                .args_json(json!({
                    "intent_id": intent, "token_id": "near", "to": ada.id(),
                    "amount": NearToken::from_near(near).as_yoctonear().to_string(),
                    "expires_at_ms": far.to_string(), "receipt_hash": "wf1:sandbox",
                    "key_id": key_id, "signature": near_primitives::serialize::to_base64(&sig)
                }))
                .gas(Gas::from_tgas(100))
                .transact()
                .await
        }
    };

    // 1. A correctly signed 1 NEAR cheque, submitted by an account that is NOT an agent, pays.
    let before = ada.view_account().await?.balance;
    let msg = cheque_message(&vault, &ada, 1, "q1", far).await?;
    let r = call("q1", 1, pq_sign(&sk, &msg)).await?;
    assert!(r.is_success(), "{:?}", r.failures());
    assert_eq!(
        ada.view_account().await?.balance.as_yoctonear() - before.as_yoctonear(),
        NearToken::from_near(1).as_yoctonear()
    );
    let logs = r.logs().join("\n");
    assert!(
        logs.contains("payment_authorized") && logs.contains(&format!("ml-dsa-65:{key_id}")),
        "{logs}"
    );
    assert!(logs.contains("payment_settled"), "{logs}");
    println!("CHEQUE-GAS total_gas_burnt={}", r.total_gas_burnt);

    // 2. Replaying the same cheque is refused (one-time intent id).
    let r = call("q1", 1, pq_sign(&sk, &msg)).await?;
    assert!(r.is_failure() && format!("{:?}", r.failures()).contains("intent_id already used"));

    // 3. A signature by a different ML-DSA key is refused.
    let (other_sk, _) = pq_key();
    let msg2 = cheque_message(&vault, &ada, 1, "q2", far).await?;
    let r = call("q2", 1, pq_sign(&other_sk, &msg2)).await?;
    assert!(r.is_failure() && format!("{:?}", r.failures()).contains("Invalid cheque signature"));

    // 4. A genuine signature for 1 NEAR cannot be presented for 2 NEAR.
    let r = call("q2", 2, pq_sign(&sk, &msg2)).await?;
    assert!(r.is_failure() && format!("{:?}", r.failures()).contains("Invalid cheque signature"));

    // 5. The per-transaction cap still binds a validly signed cheque (3 > 2).
    let msg3 = cheque_message(&vault, &ada, 3, "q3", far).await?;
    let r = call("q3", 3, pq_sign(&sk, &msg3)).await?;
    assert!(r.is_failure() && format!("{:?}", r.failures()).contains("per-transaction limit"));

    // 6. After the owner revokes the key, a valid cheque is refused.
    owner
        .call(vault.id(), "revoke_pq_agent")
        .args_json(json!({"key_id": key_id}))
        .transact()
        .await?
        .into_result()?;
    let r = call("q2", 1, pq_sign(&sk, &msg2)).await?;
    assert!(r.is_failure() && format!("{:?}", r.failures()).contains("Unknown agent key"));
    Ok(())
}
