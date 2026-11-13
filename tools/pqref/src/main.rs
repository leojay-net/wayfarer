//! Reference ML-DSA-65 tool on nearcore's `near-crypto` (aws-lc-rs underneath). Testnet/sandbox use only.
//!
//!   pqref keygen <seed-hex32>                       -> JSON with public key (string + hex)
//!   pqref sign <seed-hex32> <message-hex>           -> signature hex
//!   pqref verify <public-hex> <message-hex> <sig>   -> "true" / "false"
//!   pqref handle <public-hex>                       -> the ml-dsa-65-hash:<base58> handle NEAR stores on-chain
//!   pqref delegate-vector <ed25519:<secret>|ml:<seed-hex>> <sender> <receiver> <method> <args-json> <gas> <nonce> <max-height>
//!       -> JSON: borsh DelegateAction, its NEP-461 hash and the SignedDelegateAction, built with near-primitives
//!   pqref check-delegate <signed-delegate-hex>      -> JSON: decoded fields and whether the signature verifies
//!   pqref check-tx <signed-transaction-hex>         -> JSON: decoded fields and whether the signature verifies
//!   pqref testnet-spike <ed25519-key-file> <account> <mldsa-seed-hex32> <receiver> <method>
//!       adds an ML-DSA-65 function-call key to <account> (signed by its ed25519 key), then sends one
//!       transaction signed by the ML-DSA key, then deletes the key. Prints every step and the RPC replies.
use anyhow::{bail, Context, Result};
use near_crypto::{
    ml_dsa_65_from_seed, InMemorySigner, KeyType, PublicKey, SecretKey, Signature, Signer,
};
use near_primitives::account::{AccessKey, AccessKeyPermission, FunctionCallPermission};
use near_primitives::action::delegate::{DelegateAction, NonDelegateAction, SignedDelegateAction};
use near_primitives::action::{Action, AddKeyAction, DeleteKeyAction, FunctionCallAction};
use near_primitives::hash::CryptoHash;
use near_primitives::transaction::{SignedTransaction, Transaction, TransactionV0};
use serde_json::{json, Value};
use std::str::FromStr;

fn seed32(hex_seed: &str) -> Result<[u8; 32]> {
    hex::decode(hex_seed)?
        .try_into()
        .map_err(|_| anyhow::anyhow!("seed must be 32 bytes"))
}

fn mldsa_key(hex_seed: &str) -> Result<SecretKey> {
    ml_dsa_65_from_seed(&seed32(hex_seed)?).map_err(|e| anyhow::anyhow!("{e:?}"))
}

fn pk_bytes(pk: &PublicKey) -> Vec<u8> {
    borsh::to_vec(pk).expect("borsh")[1..].to_vec() // strip the 1-byte key-type tag
}

fn rpc(url: &str, method: &str, params: Value) -> Result<Value> {
    let body = json!({"jsonrpc":"2.0","id":"pqref","method":method,"params":params});
    Ok(reqwest::blocking::Client::new()
        .post(url)
        .json(&body)
        .send()?
        .json()?)
}

fn nonce_and_hash(url: &str, account: &str, pk: &PublicKey) -> Result<(u64, CryptoHash)> {
    let r = rpc(
        url,
        "query",
        json!({"request_type":"view_access_key","finality":"final","account_id":account,"public_key":pk.to_string()}),
    )?;
    let nonce = r["result"]["nonce"]
        .as_u64()
        .with_context(|| format!("no access key: {r}"))?;
    let hash = CryptoHash::from_str(r["result"]["block_hash"].as_str().context("block_hash")?)
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    Ok((nonce, hash))
}

fn send(
    url: &str,
    account: &str,
    signer: &Signer,
    nonce: u64,
    receiver: &str,
    block_hash: CryptoHash,
    actions: Vec<Action>,
) -> Result<Value> {
    let tx = Transaction::V0(TransactionV0 {
        signer_id: account.parse()?,
        public_key: signer.public_key(),
        nonce,
        receiver_id: receiver.parse()?,
        block_hash,
        actions,
    });
    let (hash, _) = tx.get_hash_and_size();
    let signed = SignedTransaction::new(signer.sign(hash.as_ref()), tx);
    let wire = borsh::to_vec(&signed)?;
    println!(
        "  wire size {} bytes, signature type {:?}",
        wire.len(),
        signed.signature.key_type()
    );
    let b64 = near_primitives::serialize::to_base64(&wire);
    rpc(
        url,
        "broadcast_tx_commit",
        json!({"signed_tx_base64": b64, "wait_until":"FINAL"}),
    )
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["keygen", seed] => {
            let sk = mldsa_key(seed)?;
            let pk = sk.public_key();
            println!(
                "{}",
                json!({"public_key": pk.to_string(), "public_hex": hex::encode(pk_bytes(&pk))})
            );
        }
        ["sign", seed, msg] => {
            let sig = mldsa_key(seed)?.sign(&hex::decode(msg)?);
            let raw = borsh::to_vec(&sig)?;
            println!("{}", hex::encode(&raw[1..]));
        }
        ["verify", pk, msg, sig] => {
            let mut pk_wire = vec![2u8];
            pk_wire.extend(hex::decode(pk)?);
            let mut sig_wire = vec![2u8];
            sig_wire.extend(hex::decode(sig)?);
            let pk: PublicKey = borsh::from_slice(&pk_wire)?;
            let sig: Signature = borsh::from_slice(&sig_wire)?;
            println!("{}", sig.verify(&hex::decode(msg)?, &pk));
        }
        ["handle", public_hex] => {
            let mut wire = vec![2u8];
            wire.extend(hex::decode(public_hex)?);
            let pk: PublicKey = borsh::from_slice(&wire)?;
            match pk {
                PublicKey::MLDSA65(inner) => println!(
                    "{}",
                    near_crypto::PublicKeyHandle::MlDsa65(inner.to_public_key_handle())
                ),
                _ => bail!("not an ML-DSA-65 key"),
            }
        }
        ["delegate-vector", secret, sender, receiver, method, args, gas, nonce, max_height] => {
            let sk = match secret.strip_prefix("ml:") {
                Some(seed) => mldsa_key(seed)?,
                None => SecretKey::from_str(secret)?,
            };
            let signer = InMemorySigner::from_secret_key(sender.parse()?, sk.clone());
            let call = FunctionCallAction {
                method_name: method.to_string(),
                args: args.as_bytes().to_vec(),
                gas: near_primitives::types::Gas::from_gas(gas.parse()?),
                deposit: near_primitives::types::Balance::ZERO,
            };
            let delegate = DelegateAction {
                sender_id: sender.parse()?,
                receiver_id: receiver.parse()?,
                actions: vec![
                    NonDelegateAction::try_from(Action::FunctionCall(Box::new(call)))
                        .map_err(|_| anyhow::anyhow!("delegate"))?,
                ],
                nonce: nonce.parse()?,
                max_block_height: max_height.parse()?,
                public_key: sk.public_key(),
            };
            let hash = delegate.get_nep461_hash();
            let unsigned = borsh::to_vec(&delegate)?;
            let signed = SignedDelegateAction::sign(&signer, delegate);
            println!(
                "{}",
                json!({
                    "unsigned_hex": hex::encode(unsigned),
                    "hash_hex": hex::encode(hash.as_ref()),
                    "signed_hex": hex::encode(borsh::to_vec(&signed)?),
                    "verifies": signed.verify(),
                })
            );
        }
        ["check-delegate", signed_hex] => {
            let signed: SignedDelegateAction = borsh::from_slice(&hex::decode(signed_hex)?)?;
            let d = &signed.delegate_action;
            println!(
                "{}",
                json!({
                    "verifies": signed.verify(),
                    "sender": d.sender_id.to_string(),
                    "receiver": d.receiver_id.to_string(),
                    "nonce": d.nonce,
                    "max_block_height": d.max_block_height,
                    "actions": d.actions.len(),
                    "public_key_prefix": d.public_key.to_string().chars().take(12).collect::<String>(),
                })
            );
        }
        ["check-tx", signed_hex] => {
            let stx: SignedTransaction = borsh::from_slice(&hex::decode(signed_hex)?)?;
            let (hash, _) = stx.transaction.get_hash_and_size();
            let public_key = stx.transaction.public_key().clone();
            println!(
                "{}",
                json!({
                    "verifies": stx.signature.verify(hash.as_ref(), &public_key),
                    "signer": stx.transaction.signer_id().to_string(),
                    "receiver": stx.transaction.receiver_id().to_string(),
                    "nonce": stx.transaction.nonce().nonce(),
                    "actions": stx.transaction.actions().len(),
                    "action_debug": stx.transaction.actions().iter().map(|a| format!("{a:?}")).collect::<Vec<_>>(),
                })
            );
        }
        ["testnet-spike", key_file, account, seed, receiver, method] => {
            let url = "https://rpc.testnet.near.org";
            let status = rpc(url, "status", json!([]))?;
            let proto = status["result"]["protocol_version"].as_u64().unwrap_or(0);
            println!("protocol_version {proto} (ML-DSA-65 needs >= 85)");
            if proto < 85 {
                bail!("protocol too old");
            }
            let file: Value = serde_json::from_str(&std::fs::read_to_string(key_file)?)?;
            let classic =
                SecretKey::from_str(file["private_key"].as_str().context("private_key")?)?;
            let classic_signer = InMemorySigner::from_secret_key(account.parse()?, classic.clone());
            let pq = mldsa_key(seed)?;
            let pq_signer = InMemorySigner::from_secret_key(account.parse()?, pq.clone());
            println!("ML-DSA key {}...", &pq.public_key().to_string()[..40]);

            println!("1. AddKey (function-call, receiver {receiver}, method {method}) signed by the ed25519 key");
            let (n, h) = nonce_and_hash(url, account, &classic.public_key())?;
            let add = Action::AddKey(Box::new(AddKeyAction {
                public_key: pq.public_key(),
                access_key: AccessKey {
                    nonce: 0,
                    permission: AccessKeyPermission::FunctionCall(FunctionCallPermission {
                        allowance: Some(near_primitives::types::Balance::from_millinear(100)),
                        receiver_id: receiver.to_string(),
                        method_names: vec![method.to_string()],
                    }),
                },
            }));
            let r = send(url, account, &classic_signer, n + 1, account, h, vec![add])?;
            println!(
                "  tx {} status {}",
                r["result"]["transaction"]["hash"], r["result"]["status"]
            );
            if r["result"]["status"].get("SuccessValue").is_none() {
                bail!("AddKey failed: {r}");
            }

            println!("2. Transaction signed by the ML-DSA-65 key (FunctionCall {method})");
            let (n, h) = nonce_and_hash(url, account, &pq.public_key())?;
            let call = Action::FunctionCall(Box::new(FunctionCallAction {
                method_name: method.to_string(),
                args: br#"{"intent_id":"pq-spike","token_id":"near","to":"wayfarer-vault-11121649.testnet","amount":"1","receipt_hash":"wf1:pq-spike"}"#.to_vec(),
                gas: near_primitives::types::Gas::from_teragas(30),
                deposit: near_primitives::types::Balance::ZERO,
            }));
            let r = send(url, account, &pq_signer, n + 1, receiver, h, vec![call])?;
            println!(
                "  tx {} signer key {}",
                r["result"]["transaction"]["hash"],
                r["result"]["transaction"]["public_key"]
                    .as_str()
                    .map(|s| &s[..30])
                    .unwrap_or("?")
            );
            println!("  status {}", r["result"]["status"]);
            println!(
                "  gas burnt (tx) {}",
                r["result"]["transaction_outcome"]["outcome"]["gas_burnt"]
            );
            if r["result"].is_null() {
                println!("  raw {r}");
            }

            println!("3. Cleanup: DeleteKey, signed by the ed25519 key");
            let (n, h) = nonce_and_hash(url, account, &classic.public_key())?;
            let del = Action::DeleteKey(Box::new(DeleteKeyAction {
                public_key: pq.public_key(),
            }));
            let r = send(url, account, &classic_signer, n + 1, account, h, vec![del])?;
            println!(
                "  tx {} status {}",
                r["result"]["transaction"]["hash"], r["result"]["status"]
            );
        }
        _ => bail!("usage: see source header"),
    }
    let _ = KeyType::MLDSA65;
    Ok(())
}
