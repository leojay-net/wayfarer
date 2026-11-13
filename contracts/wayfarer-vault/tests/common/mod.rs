//! Shared sandbox fixture: a funded vault with a registered agent key and a 2 / 3 NEAR policy.
#![allow(dead_code)]
use near_workspaces::types::{NearToken, SecretKey};
use near_workspaces::{Account, Contract};
use serde_json::json;

pub const WASM: &str = "target/wasm32-unknown-unknown/release/wayfarer_vault.wasm";

pub async fn setup() -> anyhow::Result<(
    near_workspaces::Worker<near_workspaces::network::Sandbox>,
    Account,
    Contract,
    SecretKey,
    Account,
)> {
    let worker = near_workspaces::sandbox().await?;
    let owner = worker.dev_create_account().await?;
    let ada = worker.dev_create_account().await?;
    let vault = worker.dev_deploy(&std::fs::read(WASM)?).await?;
    vault
        .call("new")
        .args_json(json!({"owner_id": owner.id()}))
        .transact()
        .await?
        .into_result()?;
    // fund the vault
    owner
        .transfer_near(vault.id(), NearToken::from_near(20))
        .await?
        .into_result()?;
    let agent_sk = SecretKey::from_random(near_workspaces::types::KeyType::ED25519);
    owner.call(vault.id(), "add_agent")
        .args_json(json!({"public_key": agent_sk.public_key(), "gas_allowance_near": NearToken::from_near(10).as_yoctonear().to_string()}))
        .transact().await?.into_result()?;
    owner.call(vault.id(), "set_policy")
        .args_json(json!({"token_id": "near", "per_tx_max": NearToken::from_near(2).as_yoctonear().to_string(), "daily_max": NearToken::from_near(3).as_yoctonear().to_string()}))
        .transact().await?.into_result()?;
    Ok((worker, owner, vault, agent_sk, ada))
}
