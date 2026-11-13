//! wayfarer-vault: an on-chain spending policy for AI agents.
//!
//! STATUS: testnet prototype, UNAUDITED. Do not hold more than you can lose.
//!
//! Idea: the owner funds this account and registers an *agent key*. The key is added to this very
//! account as a NEAR function-call access key limited to the two payment methods, so the agent
//! can never withdraw, change policy or add keys. Every payment must pass the policy below
//! (per-token per-transaction cap, per-token daily cap, optional recipient allow-list, pause switch,
//! one-time intent ids). Over-limit calls panic and nothing moves. Each payment emits a NEP-297
//! event carrying a caller-supplied receipt hash, which is how a user can later prove what was authorised.
//!
//! Two payment paths:
//!  * `pay_direct`: send to a NEAR account. The allow-list (if enabled) is enforced on-chain.
//!  * `pay_via_intents`: send to a NEAR Intents 1Click *deposit address* so the swap/bridge settles
//!    to any chain. The caps are enforced on-chain. The final recipient is only *declared* by the
//!    agent (the deposit address hides it), so it is logged, not enforced. See docs/VAULT.md.
//!
//! v2 adds a third path, `pay_with_cheque`, for quantum-safe agents. The owner registers an ML-DSA-65 (FIPS 204)
//! agent key *in contract state* (`add_pq_agent`); the agent signs a canonical payment message (a "cheque") on a
//! phone, and ANY account may submit it. The contract verifies the signature with the `ml_dsa_verify` host function
//! (nearcore protocol 87+), then applies the same policy as the other paths plus the mandate fields: per-agent
//! expiry, minimum interval between payments, cheque expiry and the one-time intent id. No access key is involved,
//! so the agent key cannot do anything outside this contract.
use near_sdk::borsh::{self, BorshSerialize};
use near_sdk::json_types::{Base64VecU8, U128, U64};
use near_sdk::store::{IterableMap, IterableSet, LookupMap, LookupSet};
use near_sdk::{env, near, require, AccountId, Gas, NearToken, PanicOnDefault, Promise, PublicKey};

const NANOS_PER_DAY: u64 = 86_400_000_000_000;
const FT_TRANSFER_GAS: Gas = Gas::from_tgas(30);
const CALLBACK_GAS: Gas = Gas::from_tgas(15);
const STORAGE_DEPOSIT_GAS: Gas = Gas::from_tgas(10);
/// Standard NEP-145 minimum for registering an account on a fungible token (0.00125 NEAR).
const STORAGE_REGISTRATION: NearToken = NearToken::from_yoctonear(1_250_000_000_000_000_000_000);
const NATIVE: &str = "near";
const MAX_ID_LEN: usize = 64;
const ML_DSA_65_PUBLIC_KEY_LEN: usize = 1952;
const ML_DSA_65_SIGNATURE_LEN: usize = 3309;
/// Domain separation for the cheque message. Bump the version if the layout of `ChequeBody` ever changes.
const CHEQUE_DOMAIN: &[u8] = b"wayfarer-cheque-v1";

#[near(serializers = [borsh, json])]
#[derive(Clone, Copy)]
pub struct Policy {
    pub per_tx_max: U128,
    pub daily_max: U128,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Copy, Default)]
pub struct Tally {
    pub day: u64,
    pub spent: U128,
}

/// A registered ML-DSA-65 agent key and its mandate terms. `0` means "no limit" for the two time fields.
#[near(serializers = [borsh])]
#[derive(Clone)]
pub struct PqAgent {
    public_key: Vec<u8>,
    expires_at_ms: u64,
    min_interval_ms: u64,
    last_payment_ms: u64,
}

/// Public view of a PQ agent (the 1,952-byte key is not repeated; `key_id` is the hex SHA-256 of it).
#[near(serializers = [json])]
pub struct PqAgentView {
    pub key_id: String,
    pub expires_at_ms: U64,
    pub min_interval_ms: U64,
    pub last_payment_ms: U64,
}

/// Everything a bank, processor or auditor needs to know about what one agent key may do right now.
#[near(serializers = [json])]
pub struct Mandate {
    pub vault: AccountId,
    pub owner: AccountId,
    pub key_id: String,
    pub paused: bool,
    pub expires_at_ms: U64,
    pub min_interval_ms: U64,
    pub last_payment_ms: U64,
    pub token_id: String,
    pub per_tx_max: U128,
    pub daily_max: U128,
    pub allowlist_enabled: bool,
    pub allowlist: Vec<AccountId>,
}

/// The bytes an agent signs. Borsh encoding (u32 length-prefixed strings, little-endian integers) after `CHEQUE_DOMAIN`.
#[derive(BorshSerialize)]
#[borsh(crate = "near_sdk::borsh")]
struct ChequeBody {
    vault: String,
    token_id: String,
    to: String,
    amount: u128,
    intent_id: String,
    expires_at_ms: u64,
    receipt_hash: String,
}

/// What a failed cheque transfer must undo besides the daily budget.
#[near(serializers = [borsh, json])]
#[derive(Clone)]
pub struct PqRollback {
    pub key_id: String,
    pub previous_last_payment_ms: U64,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Vault {
    owner_id: AccountId,
    agents: IterableSet<PublicKey>,
    policies: IterableMap<String, Policy>,
    tallies: LookupMap<String, Tally>,
    allowlist: IterableSet<AccountId>,
    allowlist_enabled: bool,
    used_intents: LookupSet<String>,
    paused: bool,
    pq_agents: IterableMap<String, PqAgent>,
}

fn emit(event: &str, data: &str) {
    env::log_str(&format!(
        r#"EVENT_JSON:{{"standard":"wayfarer_vault","version":"1.0.0","event":"{}","data":{}}}"#,
        event, data
    ));
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn key_id_of(public_key: &[u8]) -> String {
    hex(&env::sha256(public_key))
}

fn view_of(key_id: &str, agent: &PqAgent) -> PqAgentView {
    PqAgentView {
        key_id: key_id.to_string(),
        expires_at_ms: U64(agent.expires_at_ms),
        min_interval_ms: U64(agent.min_interval_ms),
        last_payment_ms: U64(agent.last_payment_ms),
    }
}

fn js(s: &str) -> String {
    // JSON string literal for log output (escapes quotes/backslashes/control chars).
    near_sdk::serde_json::to_string(s).unwrap()
}

#[near]
impl Vault {
    #[init]
    pub fn new(owner_id: AccountId) -> Self {
        Self {
            owner_id,
            agents: IterableSet::new(b"a"),
            policies: IterableMap::new(b"p"),
            tallies: LookupMap::new(b"t"),
            allowlist: IterableSet::new(b"l"),
            allowlist_enabled: false,
            used_intents: LookupSet::new(b"u"),
            paused: false,
            pq_agents: IterableMap::new(b"q"),
        }
    }

    // ---------------------------------------------------------------- owner

    fn only_owner(&self) {
        require!(
            env::predecessor_account_id() == self.owner_id,
            "Only the owner can call this"
        );
        // An agent key signs with predecessor == this account, never the owner, so this also blocks agents.
    }

    /// Set caps for a token. `token_id` is "near" for native NEAR (yoctoNEAR) or an FT contract account id (smallest units).
    pub fn set_policy(&mut self, token_id: String, per_tx_max: U128, daily_max: U128) {
        self.only_owner();
        require!(token_id.len() <= MAX_ID_LEN, "token_id too long");
        require!(
            token_id == NATIVE || token_id.parse::<AccountId>().is_ok(),
            "token_id must be 'near' or a valid account id"
        );
        require!(
            per_tx_max.0 > 0 && daily_max.0 >= per_tx_max.0,
            "need 0 < per_tx_max <= daily_max"
        );
        self.policies.insert(
            token_id.clone(),
            Policy {
                per_tx_max,
                daily_max,
            },
        );
        emit(
            "policy_set",
            &format!(
                r#"{{"token":{},"per_tx_max":"{}","daily_max":"{}"}}"#,
                js(&token_id),
                per_tx_max.0,
                daily_max.0
            ),
        );
    }

    pub fn remove_policy(&mut self, token_id: String) {
        self.only_owner();
        self.policies.remove(&token_id);
        emit(
            "policy_removed",
            &format!(r#"{{"token":{}}}"#, js(&token_id)),
        );
    }

    /// Register an agent key and add it to this account as a function-call key limited to the payment methods.
    /// `gas_allowance_near` bounds the gas the key may burn. It is NOT a spending limit; the policy is.
    #[payable]
    pub fn add_agent(&mut self, public_key: PublicKey, gas_allowance_near: U128) -> Promise {
        self.only_owner();
        require!(
            self.agents.insert(public_key.clone()),
            "Agent already registered"
        );
        emit(
            "agent_added",
            &format!(r#"{{"public_key":{}}}"#, js(&String::from(&public_key))),
        );
        Promise::new(env::current_account_id()).add_access_key_allowance(
            public_key,
            near_sdk::Allowance::limited(NearToken::from_yoctonear(gas_allowance_near.0))
                .expect("allowance must be above zero"),
            env::current_account_id(),
            "pay_direct,pay_via_intents".to_string(),
        )
    }

    /// Instantly revoke an agent: removes the registration and deletes the access key.
    pub fn revoke_agent(&mut self, public_key: PublicKey) -> Promise {
        self.only_owner();
        require!(self.agents.remove(&public_key), "Unknown agent key");
        emit(
            "agent_revoked",
            &format!(r#"{{"public_key":{}}}"#, js(&String::from(&public_key))),
        );
        Promise::new(env::current_account_id()).delete_key(public_key)
    }

    /// Register an ML-DSA-65 agent key (raw 1,952-byte public key) for `pay_with_cheque`. Returns its `key_id`.
    /// `expires_at_ms` (unix milliseconds, 0 = never) ends the mandate; `min_interval_ms` (0 = none) is the minimum
    /// time between two payments by this key.
    pub fn add_pq_agent(
        &mut self,
        public_key: Base64VecU8,
        expires_at_ms: U64,
        min_interval_ms: U64,
    ) -> String {
        self.only_owner();
        require!(
            public_key.0.len() == ML_DSA_65_PUBLIC_KEY_LEN,
            "ML-DSA-65 public key must be 1952 bytes"
        );
        require!(
            expires_at_ms.0 == 0 || expires_at_ms.0 > env::block_timestamp_ms(),
            "expires_at_ms must be in the future (or 0 for no expiry)"
        );
        let key_id = key_id_of(&public_key.0);
        require!(
            !self.pq_agents.contains_key(&key_id),
            "Agent already registered"
        );
        self.pq_agents.insert(
            key_id.clone(),
            PqAgent {
                public_key: public_key.0,
                expires_at_ms: expires_at_ms.0,
                min_interval_ms: min_interval_ms.0,
                last_payment_ms: 0,
            },
        );
        emit(
            "agent_added",
            &format!(
                r#"{{"kind":"ml-dsa-65","key_id":{},"expires_at_ms":"{}","min_interval_ms":"{}"}}"#,
                js(&key_id),
                expires_at_ms.0,
                min_interval_ms.0
            ),
        );
        key_id
    }

    /// Instantly revoke a PQ agent. Cheques it already signed can no longer be settled.
    pub fn revoke_pq_agent(&mut self, key_id: String) {
        self.only_owner();
        require!(
            self.pq_agents.remove(&key_id).is_some(),
            "Unknown agent key"
        );
        emit(
            "agent_revoked",
            &format!(r#"{{"kind":"ml-dsa-65","key_id":{}}}"#, js(&key_id)),
        );
    }

    pub fn set_allowlist_enabled(&mut self, enabled: bool) {
        self.only_owner();
        self.allowlist_enabled = enabled;
        emit(
            "allowlist_enabled",
            &format!(r#"{{"enabled":{}}}"#, enabled),
        );
    }

    pub fn allow_recipient(&mut self, account_id: AccountId) {
        self.only_owner();
        self.allowlist.insert(account_id.clone());
        emit(
            "recipient_allowed",
            &format!(r#"{{"account":{}}}"#, js(account_id.as_str())),
        );
    }

    pub fn disallow_recipient(&mut self, account_id: AccountId) {
        self.only_owner();
        self.allowlist.remove(&account_id);
        emit(
            "recipient_removed",
            &format!(r#"{{"account":{}}}"#, js(account_id.as_str())),
        );
    }

    pub fn set_paused(&mut self, paused: bool) {
        self.only_owner();
        self.paused = paused;
        emit("paused", &format!(r#"{{"paused":{}}}"#, paused));
    }

    /// Owner escape hatch: not subject to agent caps.
    pub fn owner_withdraw(&mut self, token_id: String, to: AccountId, amount: U128) -> Promise {
        self.only_owner();
        require!(amount.0 > 0, "amount must be above zero");
        emit(
            "owner_withdraw",
            &format!(
                r#"{{"token":{},"to":{},"amount":"{}"}}"#,
                js(&token_id),
                js(to.as_str()),
                amount.0
            ),
        );
        self.transfer(&token_id, to, amount.0, false)
    }

    // ---------------------------------------------------------------- agent

    fn only_agent(&self) {
        require!(
            env::predecessor_account_id() == env::current_account_id()
                && self.agents.contains(&env::signer_account_pk()),
            "Only a registered agent key can call this"
        );
    }

    /// Reserve `amount` of `token_id` against the policy or panic. Mutates the daily tally.
    fn reserve(&mut self, intent_id: &str, token_id: &str, amount: u128) {
        require!(!self.paused, "Vault is paused");
        require!(
            !intent_id.is_empty() && intent_id.len() <= MAX_ID_LEN,
            "intent_id must be 1-64 chars"
        );
        require!(amount > 0, "amount must be above zero");
        let policy = *self
            .policies
            .get(token_id)
            .unwrap_or_else(|| env::panic_str("No policy for this token: not allowed"));
        require!(
            amount <= policy.per_tx_max.0,
            "Over the per-transaction limit"
        );
        let day = env::block_timestamp() / NANOS_PER_DAY;
        let mut tally = self.tallies.get(token_id).copied().unwrap_or_default();
        if tally.day != day {
            tally = Tally {
                day,
                spent: U128(0),
            };
        }
        let new_spent = tally
            .spent
            .0
            .checked_add(amount)
            .unwrap_or_else(|| env::panic_str("overflow"));
        require!(new_spent <= policy.daily_max.0, "Over the daily limit");
        require!(
            self.used_intents.insert(intent_id.to_string()),
            "intent_id already used"
        );
        tally.spent = U128(new_spent);
        self.tallies.insert(token_id.to_string(), tally);
    }

    /// `register_receiver` first registers `to` on the token contract (NEP-145 `storage_deposit`, registration only).
    /// 1Click deposit addresses are fresh accounts, so without this their first `ft_transfer` would fail.
    /// The registration fee is paid from the vault balance, is a fixed small constant, and is not part of the policy.
    fn transfer(
        &self,
        token_id: &str,
        to: AccountId,
        amount: u128,
        register_receiver: bool,
    ) -> Promise {
        if token_id == NATIVE {
            let reserved = env::storage_byte_cost().saturating_mul(env::storage_usage() as u128);
            require!(
                env::account_balance()
                    .saturating_sub(reserved)
                    .as_yoctonear()
                    >= amount,
                "Insufficient NEAR balance"
            );
            Promise::new(to).transfer(NearToken::from_yoctonear(amount))
        } else {
            let ft: AccountId = token_id
                .parse()
                .unwrap_or_else(|_| env::panic_str("bad token id"));
            let args = format!(r#"{{"receiver_id":"{}","amount":"{}"}}"#, to, amount);
            let transfer = Promise::new(ft.clone()).function_call(
                "ft_transfer".to_string(),
                args.into_bytes(),
                NearToken::from_yoctonear(1),
                FT_TRANSFER_GAS,
            );
            if !register_receiver {
                return transfer;
            }
            require!(
                env::account_balance()
                    >= STORAGE_REGISTRATION.saturating_add(NearToken::from_yoctonear(1)),
                "Insufficient NEAR balance to register the receiver"
            );
            let register_args = format!(r#"{{"account_id":"{}","registration_only":true}}"#, to);
            Promise::new(ft)
                .function_call(
                    "storage_deposit".to_string(),
                    register_args.into_bytes(),
                    STORAGE_REGISTRATION,
                    STORAGE_DEPOSIT_GAS,
                )
                .then(transfer)
        }
    }

    /// Reserve against the policy, emit `payment_authorized` and start the transfer. The caller has already
    /// authenticated the payer (`only_agent` or a verified cheque). `agent_key` identifies the authorising key
    /// in the event; `relayer` is whoever submitted the call.
    #[allow(clippy::too_many_arguments)]
    fn settle(
        &mut self,
        kind: &str,
        intent_id: String,
        token_id: String,
        to: AccountId,
        amount: U128,
        declared: String,
        receipt_hash: String,
        agent_key: String,
        rollback: Option<PqRollback>,
    ) -> Promise {
        require!(receipt_hash.len() <= 128, "receipt_hash too long");
        self.reserve(&intent_id, &token_id, amount.0);
        emit(
            "payment_authorized",
            &format!(
                r#"{{"kind":"{}","intent_id":{},"token":{},"to":{},"amount":"{}","declared_recipient":{},"receipt_hash":{},"agent_key":{},"relayer":{}}}"#,
                kind,
                js(&intent_id),
                js(&token_id),
                js(to.as_str()),
                amount.0,
                js(&declared),
                js(&receipt_hash),
                js(&agent_key),
                js(env::predecessor_account_id().as_str())
            ),
        );
        self.transfer(&token_id, to, amount.0, kind == "intents")
            .then(
                Self::ext(env::current_account_id())
                    .with_static_gas(CALLBACK_GAS)
                    .on_payment_done(intent_id, token_id, amount, rollback),
            )
    }

    /// Pay a NEAR account directly. If the allow-list is enabled the recipient must be on it.
    pub fn pay_direct(
        &mut self,
        intent_id: String,
        token_id: String,
        to: AccountId,
        amount: U128,
        receipt_hash: String,
    ) -> Promise {
        self.only_agent();
        if self.allowlist_enabled {
            require!(
                self.allowlist.contains(&to),
                "Recipient is not on the allow-list"
            );
        }
        let declared = to.to_string();
        let agent_key = String::from(&env::signer_account_pk());
        self.settle(
            "direct",
            intent_id,
            token_id,
            to,
            amount,
            declared,
            receipt_hash,
            agent_key,
            None,
        )
    }

    /// Bytes an agent signs for a cheque: `CHEQUE_DOMAIN` then the borsh encoding of the cheque fields. Pure.
    fn cheque_bytes(
        &self,
        intent_id: &str,
        token_id: &str,
        to: &AccountId,
        amount: u128,
        expires_at_ms: u64,
        receipt_hash: &str,
    ) -> Vec<u8> {
        let body = ChequeBody {
            vault: env::current_account_id().to_string(),
            token_id: token_id.to_string(),
            to: to.to_string(),
            amount,
            intent_id: intent_id.to_string(),
            expires_at_ms,
            receipt_hash: receipt_hash.to_string(),
        };
        let mut out = CHEQUE_DOMAIN.to_vec();
        out.extend(borsh::to_vec(&body).unwrap_or_else(|_| env::panic_str("encode")));
        out
    }

    /// The exact message a PQ agent must sign for these cheque fields (view helper for wallets and tests).
    pub fn cheque_message(
        &self,
        intent_id: String,
        token_id: String,
        to: AccountId,
        amount: U128,
        expires_at_ms: U64,
        receipt_hash: String,
    ) -> Base64VecU8 {
        self.cheque_bytes(
            &intent_id,
            &token_id,
            &to,
            amount.0,
            expires_at_ms.0,
            &receipt_hash,
        )
        .into()
    }

    /// Settle a signed cheque. ANY account may call this (the relayer pays gas); the ML-DSA-65 signature is the
    /// authority. The cheque binds this vault, token, payee, amount, intent id, expiry and receipt hash.
    #[allow(clippy::too_many_arguments)]
    pub fn pay_with_cheque(
        &mut self,
        intent_id: String,
        token_id: String,
        to: AccountId,
        amount: U128,
        expires_at_ms: U64,
        receipt_hash: String,
        key_id: String,
        signature: Base64VecU8,
    ) -> Promise {
        let now_ms = env::block_timestamp_ms();
        require!(now_ms <= expires_at_ms.0, "Cheque has expired");
        let agent = self
            .pq_agents
            .get(&key_id)
            .cloned()
            .unwrap_or_else(|| env::panic_str("Unknown agent key"));
        require!(
            agent.expires_at_ms == 0 || now_ms <= agent.expires_at_ms,
            "Agent mandate has expired"
        );
        require!(
            agent.min_interval_ms == 0
                || agent.last_payment_ms == 0
                || now_ms >= agent.last_payment_ms.saturating_add(agent.min_interval_ms),
            "Minimum interval between payments has not elapsed"
        );
        if self.allowlist_enabled {
            require!(
                self.allowlist.contains(&to),
                "Recipient is not on the allow-list"
            );
        }
        let message = self.cheque_bytes(
            &intent_id,
            &token_id,
            &to,
            amount.0,
            expires_at_ms.0,
            &receipt_hash,
        );
        let signature: [u8; ML_DSA_65_SIGNATURE_LEN] = signature
            .0
            .try_into()
            .unwrap_or_else(|_| env::panic_str("ML-DSA-65 signature must be 3309 bytes"));
        let public_key: [u8; ML_DSA_65_PUBLIC_KEY_LEN] = agent
            .public_key
            .clone()
            .try_into()
            .unwrap_or_else(|_| env::panic_str("stored key has the wrong size"));
        require!(
            env::ml_dsa_verify(&signature, &message, &public_key),
            "Invalid cheque signature"
        );
        let rollback = PqRollback {
            key_id: key_id.clone(),
            previous_last_payment_ms: U64(agent.last_payment_ms),
        };
        if let Some(stored) = self.pq_agents.get_mut(&key_id) {
            stored.last_payment_ms = now_ms;
        }
        let declared = to.to_string();
        self.settle(
            "cheque",
            intent_id,
            token_id,
            to,
            amount,
            declared,
            receipt_hash,
            format!("ml-dsa-65:{key_id}"),
            Some(rollback),
        )
    }

    /// Pay a NEAR Intents 1Click deposit address (on this chain) so it settles to `declared_recipient` anywhere.
    /// The deposit address is opaque, so `declared_recipient` is recorded in the event but NOT enforced.
    pub fn pay_via_intents(
        &mut self,
        intent_id: String,
        token_id: String,
        deposit_address: AccountId,
        amount: U128,
        declared_recipient: String,
        receipt_hash: String,
    ) -> Promise {
        self.only_agent();
        require!(
            declared_recipient.len() <= 128,
            "declared_recipient too long"
        );
        let agent_key = String::from(&env::signer_account_pk());
        self.settle(
            "intents",
            intent_id,
            token_id,
            deposit_address,
            amount,
            declared_recipient,
            receipt_hash,
            agent_key,
            None,
        )
    }

    #[private]
    pub fn on_payment_done(
        &mut self,
        intent_id: String,
        token_id: String,
        amount: U128,
        rollback: Option<PqRollback>,
    ) -> bool {
        let ok = env::promise_result_checked(0, 1024).is_ok();
        if !ok {
            // A failed cheque payment must not consume the agent's minimum-interval slot either.
            if let Some(r) = rollback {
                if let Some(agent) = self.pq_agents.get_mut(&r.key_id) {
                    agent.last_payment_ms = r.previous_last_payment_ms.0;
                }
            }
            // Give the budget back and allow a retry with the same intent id.
            let day = env::block_timestamp() / NANOS_PER_DAY;
            if let Some(mut t) = self.tallies.get(&token_id).copied() {
                if t.day == day {
                    t.spent = U128(t.spent.0.saturating_sub(amount.0));
                    self.tallies.insert(token_id.clone(), t);
                }
            }
            self.used_intents.remove(&intent_id);
            emit(
                "payment_failed",
                &format!(
                    r#"{{"intent_id":{},"token":{},"amount":"{}"}}"#,
                    js(&intent_id),
                    js(&token_id),
                    amount.0
                ),
            );
        } else {
            emit(
                "payment_settled",
                &format!(
                    r#"{{"intent_id":{},"token":{},"amount":"{}"}}"#,
                    js(&intent_id),
                    js(&token_id),
                    amount.0
                ),
            );
        }
        ok
    }

    // ---------------------------------------------------------------- views

    pub fn get_owner(&self) -> AccountId {
        self.owner_id.clone()
    }
    pub fn is_paused(&self) -> bool {
        self.paused
    }
    pub fn is_agent(&self, public_key: PublicKey) -> bool {
        self.agents.contains(&public_key)
    }
    pub fn list_agents(&self) -> Vec<PublicKey> {
        self.agents.iter().cloned().collect()
    }
    pub fn get_pq_agent(&self, key_id: String) -> Option<PqAgentView> {
        self.pq_agents.get(&key_id).map(|a| view_of(&key_id, a))
    }
    pub fn list_pq_agents(&self) -> Vec<PqAgentView> {
        self.pq_agents
            .iter()
            .map(|(id, a)| view_of(id, a))
            .collect()
    }

    /// The full mandate of one agent key for one token, in a form an outside reader can check.
    pub fn get_mandate(&self, key_id: String, token_id: String) -> Option<Mandate> {
        let agent = self.pq_agents.get(&key_id)?;
        let policy = *self.policies.get(&token_id)?;
        Some(Mandate {
            vault: env::current_account_id(),
            owner: self.owner_id.clone(),
            key_id,
            paused: self.paused,
            expires_at_ms: U64(agent.expires_at_ms),
            min_interval_ms: U64(agent.min_interval_ms),
            last_payment_ms: U64(agent.last_payment_ms),
            token_id,
            per_tx_max: policy.per_tx_max,
            daily_max: policy.daily_max,
            allowlist_enabled: self.allowlist_enabled,
            allowlist: self.allowlist.iter().cloned().collect(),
        })
    }

    pub fn get_policy(&self, token_id: String) -> Option<Policy> {
        self.policies.get(&token_id).copied()
    }
    pub fn intent_used(&self, intent_id: String) -> bool {
        self.used_intents.contains(&intent_id)
    }
    pub fn allowlist_is_enabled(&self) -> bool {
        self.allowlist_enabled
    }
    pub fn list_allowed(&self) -> Vec<AccountId> {
        self.allowlist.iter().cloned().collect()
    }

    /// (spent today, remaining today) in the token's smallest unit.
    pub fn get_budget(&self, token_id: String) -> Option<(U128, U128)> {
        let p = *self.policies.get(&token_id)?;
        let day = env::block_timestamp() / NANOS_PER_DAY;
        let spent = self
            .tallies
            .get(&token_id)
            .filter(|t| t.day == day)
            .map(|t| t.spent.0)
            .unwrap_or(0);
        Some((U128(spent), U128(p.daily_max.0.saturating_sub(spent))))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::test_utils::{get_logs, VMContextBuilder};
    use near_sdk::{testing_env, PromiseResult};

    const AGENT_PK: &str = "ed25519:6E8sCci9badyRkXb3JoRpBj5p8C6Tw41ELDZoiihKEtp";
    const OTHER_PK: &str = "ed25519:3tysLvy7KGoE8pznUgXvSHa4vYyGvrDZFcT8jgb8PEQ6";
    const USDC: &str = "usdc.test.near";

    fn acct(s: &str) -> AccountId {
        s.parse().unwrap()
    }
    fn pk(s: &str) -> PublicKey {
        s.parse().unwrap()
    }

    /// Context of a call made by `predecessor`, signed by `signer_pk`, at `day`.
    fn ctx(predecessor: &str, signer_pk: &str, day: u64) -> VMContextBuilder {
        let mut b = VMContextBuilder::new();
        b.current_account_id(acct("vault.alice.near"))
            .predecessor_account_id(acct(predecessor))
            .signer_account_id(acct(predecessor))
            .signer_account_pk(pk(signer_pk))
            .block_timestamp(day * NANOS_PER_DAY + 1_000)
            .account_balance(NearToken::from_near(100));
        b
    }
    fn owner(day: u64) -> VMContextBuilder {
        ctx("alice.near", OTHER_PK, day)
    }
    fn agent(day: u64) -> VMContextBuilder {
        ctx("vault.alice.near", AGENT_PK, day)
    }

    /// Vault with owner alice, agent registered, USDC policy per-tx 100, daily 250 (6-decimals units omitted for clarity).
    fn setup() -> Vault {
        testing_env!(owner(10).build());
        let mut v = Vault::new(acct("alice.near"));
        v.set_policy(USDC.into(), U128(100), U128(250));
        v.agents.insert(pk(AGENT_PK)); // add_agent returns a Promise; registration is tested separately
        v
    }

    fn pay(v: &mut Vault, id: &str, amt: u128) {
        let _ = v.pay_direct(
            id.into(),
            USDC.into(),
            acct("ada.near"),
            U128(amt),
            "hash".into(),
        );
    }

    #[test]
    fn within_limits_pays_and_tracks_budget() {
        let mut v = setup();
        testing_env!(agent(10).build());
        pay(&mut v, "i1", 100);
        assert_eq!(v.get_budget(USDC.into()), Some((U128(100), U128(150))));
        let logs = get_logs().join("\n");
        assert!(
            logs.contains(r#""event":"payment_authorized""#)
                && logs.contains(r#""receipt_hash":"hash""#),
            "{logs}"
        );
    }

    #[test]
    #[should_panic(expected = "Over the per-transaction limit")]
    fn over_per_tx_limit_is_refused() {
        let mut v = setup();
        testing_env!(agent(10).build());
        pay(&mut v, "i1", 101); // the "send two thousand dollars" demo case
    }

    #[test]
    #[should_panic(expected = "Over the daily limit")]
    fn over_daily_limit_is_refused() {
        let mut v = setup();
        testing_env!(agent(10).build());
        pay(&mut v, "i1", 100);
        pay(&mut v, "i2", 100);
        pay(&mut v, "i3", 100); // 300 > 250
    }

    #[test]
    fn daily_budget_resets_next_utc_day() {
        let mut v = setup();
        testing_env!(agent(10).build());
        pay(&mut v, "i1", 100);
        pay(&mut v, "i2", 100);
        testing_env!(agent(11).build());
        pay(&mut v, "i3", 100);
        assert_eq!(v.get_budget(USDC.into()), Some((U128(100), U128(150))));
    }

    #[test]
    #[should_panic(expected = "intent_id already used")]
    fn replayed_intent_is_refused() {
        let mut v = setup();
        testing_env!(agent(10).build());
        pay(&mut v, "i1", 10);
        pay(&mut v, "i1", 10);
    }

    #[test]
    #[should_panic(expected = "No policy for this token")]
    fn unknown_token_is_refused() {
        let mut v = setup();
        testing_env!(agent(10).build());
        let _ = v.pay_direct(
            "i1".into(),
            "evil.near".into(),
            acct("ada.near"),
            U128(1),
            "h".into(),
        );
    }

    #[test]
    #[should_panic(expected = "Only a registered agent key")]
    fn unregistered_key_cannot_pay() {
        let mut v = setup();
        testing_env!(ctx("vault.alice.near", OTHER_PK, 10).build());
        pay(&mut v, "i1", 1);
    }

    #[test]
    #[should_panic(expected = "Only a registered agent key")]
    fn another_contract_cannot_pay_even_with_the_agent_key() {
        let mut v = setup();
        testing_env!(ctx("attacker.near", AGENT_PK, 10).build());
        pay(&mut v, "i1", 1);
    }

    #[test]
    #[should_panic(expected = "Only the owner")]
    fn agent_cannot_change_policy() {
        let mut v = setup();
        testing_env!(agent(10).build());
        v.set_policy(USDC.into(), U128(1_000_000), U128(1_000_000));
    }

    #[test]
    #[should_panic(expected = "Only the owner")]
    fn agent_cannot_withdraw() {
        let mut v = setup();
        testing_env!(agent(10).build());
        let _ = v.owner_withdraw(USDC.into(), acct("evil.near"), U128(1));
    }

    #[test]
    #[should_panic(expected = "Vault is paused")]
    fn pause_blocks_payments() {
        let mut v = setup();
        testing_env!(owner(10).build());
        v.set_paused(true);
        testing_env!(agent(10).build());
        pay(&mut v, "i1", 1);
    }

    #[test]
    #[should_panic(expected = "not on the allow-list")]
    fn allowlist_blocks_unknown_recipient_on_direct_path() {
        let mut v = setup();
        testing_env!(owner(10).build());
        v.set_allowlist_enabled(true);
        v.allow_recipient(acct("bob.near"));
        testing_env!(agent(10).build());
        pay(&mut v, "i1", 1); // ada.near is not allowed
    }

    #[test]
    fn allowlist_permits_listed_recipient() {
        let mut v = setup();
        testing_env!(owner(10).build());
        v.set_allowlist_enabled(true);
        v.allow_recipient(acct("ada.near"));
        testing_env!(agent(10).build());
        pay(&mut v, "i1", 1);
    }

    #[test]
    fn intents_path_enforces_caps_and_logs_declared_recipient() {
        let mut v = setup();
        testing_env!(agent(10).build());
        let _ = v.pay_via_intents(
            "i1".into(),
            USDC.into(),
            acct("a1b2.deposit.near"),
            U128(50),
            "0xAbC...evm".into(),
            "h".into(),
        );
        let logs = get_logs().join("\n");
        assert!(
            logs.contains(r#""kind":"intents""#) && logs.contains("0xAbC...evm"),
            "{logs}"
        );
    }

    #[test]
    #[should_panic(expected = "Over the per-transaction limit")]
    fn intents_path_cannot_bypass_caps() {
        let mut v = setup();
        testing_env!(agent(10).build());
        let _ = v.pay_via_intents(
            "i1".into(),
            USDC.into(),
            acct("a1b2.deposit.near"),
            U128(2000),
            "x".into(),
            "h".into(),
        );
    }

    #[test]
    fn failed_transfer_restores_budget_and_allows_retry() {
        let mut v = setup();
        testing_env!(agent(10).build());
        pay(&mut v, "i1", 100);
        testing_env!(
            ctx("vault.alice.near", AGENT_PK, 10).build(),
            near_sdk::test_vm_config(),
            near_sdk::RuntimeFeesConfig::test(),
            Default::default(),
            vec![PromiseResult::Failed]
        );
        assert!(!v.on_payment_done("i1".into(), USDC.into(), U128(100), None));
        assert_eq!(v.get_budget(USDC.into()), Some((U128(0), U128(250))));
        assert!(!v.intent_used("i1".into()));
    }

    #[test]
    fn successful_transfer_keeps_spend() {
        let mut v = setup();
        testing_env!(agent(10).build());
        pay(&mut v, "i1", 100);
        testing_env!(
            ctx("vault.alice.near", AGENT_PK, 10).build(),
            near_sdk::test_vm_config(),
            near_sdk::RuntimeFeesConfig::test(),
            Default::default(),
            vec![PromiseResult::Successful(vec![])]
        );
        assert!(v.on_payment_done("i1".into(), USDC.into(), U128(100), None));
        assert_eq!(v.get_budget(USDC.into()), Some((U128(100), U128(150))));
        assert!(v.intent_used("i1".into()));
    }

    #[test]
    fn native_near_respects_policy() {
        let mut v = setup();
        testing_env!(owner(10).build());
        v.set_policy(
            "near".into(),
            U128(NearToken::from_near(2).as_yoctonear()),
            U128(NearToken::from_near(3).as_yoctonear()),
        );
        testing_env!(agent(10).build());
        let _ = v.pay_direct(
            "n1".into(),
            "near".into(),
            acct("ada.near"),
            U128(NearToken::from_near(2).as_yoctonear()),
            "h".into(),
        );
    }

    #[test]
    #[should_panic(expected = "need 0 < per_tx_max <= daily_max")]
    fn nonsense_policy_is_rejected() {
        let mut v = setup();
        testing_env!(owner(10).build());
        v.set_policy(USDC.into(), U128(500), U128(100));
    }

    #[test]
    fn owner_can_register_and_revoke_agents() {
        testing_env!(owner(10).build());
        let mut v = Vault::new(acct("alice.near"));
        let _ = v.add_agent(
            pk(AGENT_PK),
            U128(NearToken::from_millinear(250).as_yoctonear()),
        );
        assert!(v.is_agent(pk(AGENT_PK)));
        let _ = v.revoke_agent(pk(AGENT_PK));
        assert!(!v.is_agent(pk(AGENT_PK)));
    }

    // ------------------------------------------------------------------ ML-DSA-65 cheques (v2)

    use near_sdk::serde_json::{self, Value};

    fn fixtures() -> Value {
        serde_json::from_str(include_str!("../tests/fixtures/cheques.json")).unwrap()
    }
    fn b64(v: &Value) -> Vec<u8> {
        serde_json::from_value::<Base64VecU8>(v.clone()).unwrap().0
    }
    fn relayer(ms: u64) -> VMContextBuilder {
        let mut b = ctx("relayer.near", OTHER_PK, 10);
        b.block_timestamp(ms * 1_000_000);
        b
    }
    /// 10 days after the epoch, in ms (the `ctx` helper's day 10 plus a millisecond of slack).
    const NOW_MS: u64 = 10 * 86_400_000;

    /// Vault with a NEAR policy (2 per tx, 3 per day, in NEAR) and the fixture key registered.
    fn pq_setup(expires_at_ms: u64, min_interval_ms: u64) -> (Vault, String) {
        testing_env!(owner(10).build());
        let mut v = Vault::new(acct("alice.near"));
        let one_near = 10u128.pow(24);
        v.set_policy(NATIVE.into(), U128(2 * one_near), U128(3 * one_near));
        let f = fixtures();
        let key_id = v.add_pq_agent(
            serde_json::from_value(f["public_key_b64"].clone()).unwrap(),
            U64(expires_at_ms),
            U64(min_interval_ms),
        );
        assert_eq!(key_id, f["key_id"].as_str().unwrap());
        (v, key_id)
    }

    fn cheque_call(v: &mut Vault, name: &str, key_id: &str) -> Promise {
        let f = fixtures();
        let c = &f["cheques"][name];
        v.pay_with_cheque(
            c["intent_id"].as_str().unwrap().into(),
            c["token_id"].as_str().unwrap().into(),
            acct(c["to"].as_str().unwrap()),
            U128(c["amount"].as_str().unwrap().parse().unwrap()),
            U64(c["expires_at_ms"].as_str().unwrap().parse().unwrap()),
            c["receipt_hash"].as_str().unwrap().into(),
            key_id.into(),
            serde_json::from_value(c["signature_b64"].clone()).unwrap(),
        )
    }

    #[test]
    fn cheque_message_matches_the_python_encoding() {
        let (v, _) = pq_setup(0, 0);
        let f = fixtures();
        for name in ["ok1", "other_payee", "stale"] {
            let c = &f["cheques"][name];
            let message = v.cheque_message(
                c["intent_id"].as_str().unwrap().into(),
                c["token_id"].as_str().unwrap().into(),
                acct(c["to"].as_str().unwrap()),
                U128(c["amount"].as_str().unwrap().parse().unwrap()),
                U64(c["expires_at_ms"].as_str().unwrap().parse().unwrap()),
                c["receipt_hash"].as_str().unwrap().into(),
            );
            assert_eq!(message.0, b64(&c["message_b64"]), "{name}");
        }
    }

    #[test]
    fn valid_cheque_is_paid_by_any_relayer_and_names_the_key() {
        let (mut v, key_id) = pq_setup(0, 0);
        testing_env!(relayer(NOW_MS).build());
        let _ = cheque_call(&mut v, "ok1", &key_id);
        let logs = get_logs().join("\n");
        assert!(logs.contains(r#""event":"payment_authorized""#), "{logs}");
        assert!(
            logs.contains(&format!(r#""agent_key":"ml-dsa-65:{key_id}""#)),
            "{logs}"
        );
        assert!(logs.contains(r#""relayer":"relayer.near""#), "{logs}");
        assert!(logs.contains(r#""kind":"cheque""#), "{logs}");
        let (spent, _) = v.get_budget(NATIVE.into()).unwrap();
        assert_eq!(spent.0, 10u128.pow(24));
        assert_eq!(v.get_pq_agent(key_id).unwrap().last_payment_ms.0, NOW_MS);
    }

    #[test]
    #[should_panic(expected = "Invalid cheque signature")]
    fn cheque_signed_for_another_payee_is_refused() {
        let (mut v, key_id) = pq_setup(0, 0);
        let f = fixtures();
        // Valid signature for "ok1" but presented with the "other_payee" arguments.
        testing_env!(relayer(NOW_MS).build());
        let c = &f["cheques"]["other_payee"];
        let _ = v.pay_with_cheque(
            "c4".into(),
            "near".into(),
            acct(c["to"].as_str().unwrap()),
            U128(10u128.pow(24)),
            U64(4_000_000_000_000),
            "wf1:abc".into(),
            key_id,
            serde_json::from_value(f["cheques"]["ok1"]["signature_b64"].clone()).unwrap(),
        );
    }

    #[test]
    #[should_panic(expected = "Invalid cheque signature")]
    fn cheque_with_a_tampered_amount_is_refused() {
        let (mut v, key_id) = pq_setup(0, 0);
        let f = fixtures();
        testing_env!(relayer(NOW_MS).build());
        let c = &f["cheques"]["ok1"];
        let _ = v.pay_with_cheque(
            "c1".into(),
            "near".into(),
            acct("ada.test.near"),
            U128(2 * 10u128.pow(24)), // signed for 1 NEAR
            U64(4_000_000_000_000),
            "wf1:abc".into(),
            key_id,
            serde_json::from_value(c["signature_b64"].clone()).unwrap(),
        );
    }

    #[test]
    #[should_panic(expected = "ML-DSA-65 signature must be 3309 bytes")]
    fn wrong_signature_length_is_refused() {
        let (mut v, key_id) = pq_setup(0, 0);
        testing_env!(relayer(NOW_MS).build());
        let _ = v.pay_with_cheque(
            "c1".into(),
            "near".into(),
            acct("ada.test.near"),
            U128(10u128.pow(24)),
            U64(4_000_000_000_000),
            "wf1:abc".into(),
            key_id,
            vec![0u8; 100].into(),
        );
    }

    #[test]
    #[should_panic(expected = "Cheque has expired")]
    fn expired_cheque_is_refused() {
        let (mut v, key_id) = pq_setup(0, 0);
        testing_env!(relayer(NOW_MS).build());
        let _ = cheque_call(&mut v, "stale", &key_id);
    }

    #[test]
    #[should_panic(expected = "Unknown agent key")]
    fn unknown_or_revoked_key_is_refused() {
        let (mut v, key_id) = pq_setup(0, 0);
        testing_env!(owner(10).build());
        v.revoke_pq_agent(key_id.clone());
        testing_env!(relayer(NOW_MS).build());
        let _ = cheque_call(&mut v, "ok1", &key_id);
    }

    #[test]
    #[should_panic(expected = "intent_id already used")]
    fn a_cheque_cannot_be_settled_twice() {
        let (mut v, key_id) = pq_setup(0, 0);
        testing_env!(relayer(NOW_MS).build());
        let _ = cheque_call(&mut v, "ok1", &key_id);
        let _ = cheque_call(&mut v, "ok1", &key_id);
    }

    #[test]
    #[should_panic(expected = "Agent mandate has expired")]
    fn expired_mandate_is_refused() {
        let (mut v, key_id) = pq_setup(NOW_MS + 5_000, 0);
        testing_env!(relayer(NOW_MS + 5_001).build());
        let _ = cheque_call(&mut v, "ok1", &key_id);
    }

    #[test]
    fn mandate_still_valid_before_its_end_date() {
        let (mut v, key_id) = pq_setup(NOW_MS + 5_000, 0);
        testing_env!(relayer(NOW_MS + 4_999).build());
        let _ = cheque_call(&mut v, "ok1", &key_id);
    }

    #[test]
    #[should_panic(expected = "Minimum interval between payments has not elapsed")]
    fn minimum_interval_blocks_a_second_payment() {
        let (mut v, key_id) = pq_setup(0, 60_000);
        testing_env!(relayer(NOW_MS).build());
        let _ = cheque_call(&mut v, "ok1", &key_id);
        testing_env!(relayer(NOW_MS + 59_999).build());
        let _ = cheque_call(&mut v, "ok2", &key_id);
    }

    #[test]
    fn minimum_interval_allows_the_next_payment_when_elapsed() {
        let (mut v, key_id) = pq_setup(0, 60_000);
        testing_env!(relayer(NOW_MS).build());
        let _ = cheque_call(&mut v, "ok1", &key_id);
        testing_env!(relayer(NOW_MS + 60_000).build());
        let _ = cheque_call(&mut v, "ok2", &key_id);
        assert_eq!(
            v.get_pq_agent(key_id).unwrap().last_payment_ms.0,
            NOW_MS + 60_000
        );
    }

    #[test]
    #[should_panic(expected = "Over the daily limit")]
    fn the_policy_caps_still_apply_to_cheques() {
        let (mut v, key_id) = pq_setup(0, 0);
        testing_env!(relayer(NOW_MS).build());
        let _ = cheque_call(&mut v, "ok1", &key_id);
        let _ = cheque_call(&mut v, "ok2", &key_id);
        let _ = cheque_call(&mut v, "ok3", &key_id); // 3 NEAR: exactly the daily cap
                                                     // A fourth signed cheque (1 NEAR to another payee) would make 4 > 3.
        let _ = cheque_call(&mut v, "other_payee", &key_id);
    }

    #[test]
    #[should_panic(expected = "Vault is paused")]
    fn a_paused_vault_refuses_cheques() {
        let (mut v, key_id) = pq_setup(0, 0);
        testing_env!(owner(10).build());
        v.set_paused(true);
        testing_env!(relayer(NOW_MS).build());
        let _ = cheque_call(&mut v, "ok1", &key_id);
    }

    #[test]
    #[should_panic(expected = "Recipient is not on the allow-list")]
    fn the_allow_list_applies_to_cheques() {
        let (mut v, key_id) = pq_setup(0, 0);
        testing_env!(owner(10).build());
        v.set_allowlist_enabled(true);
        testing_env!(relayer(NOW_MS).build());
        let _ = cheque_call(&mut v, "ok1", &key_id);
    }

    #[test]
    fn a_failed_cheque_transfer_gives_back_budget_and_the_interval_slot() {
        let (mut v, key_id) = pq_setup(0, 60_000);
        testing_env!(relayer(NOW_MS).build());
        let _ = cheque_call(&mut v, "ok1", &key_id);
        testing_env!(
            ctx("vault.alice.near", AGENT_PK, 10).build(),
            near_sdk::test_vm_config(),
            near_sdk::RuntimeFeesConfig::test(),
            Default::default(),
            vec![PromiseResult::Failed]
        );
        let rollback = PqRollback {
            key_id: key_id.clone(),
            previous_last_payment_ms: U64(0),
        };
        assert!(!v.on_payment_done(
            "c1".into(),
            NATIVE.into(),
            U128(10u128.pow(24)),
            Some(rollback)
        ));
        assert_eq!(v.get_pq_agent(key_id).unwrap().last_payment_ms.0, 0);
        assert_eq!(v.get_budget(NATIVE.into()).unwrap().0 .0, 0);
        assert!(!v.intent_used("c1".into()));
    }

    #[test]
    #[should_panic(expected = "Only the owner can call this")]
    fn only_the_owner_registers_pq_agents() {
        let (mut v, _) = pq_setup(0, 0);
        testing_env!(relayer(NOW_MS).build());
        let f = fixtures();
        v.add_pq_agent(
            serde_json::from_value(f["public_key_b64"].clone()).unwrap(),
            U64(0),
            U64(0),
        );
    }

    #[test]
    #[should_panic(expected = "ML-DSA-65 public key must be 1952 bytes")]
    fn short_pq_keys_are_rejected() {
        let (mut v, _) = pq_setup(0, 0);
        testing_env!(owner(10).build());
        v.add_pq_agent(vec![1u8; 32].into(), U64(0), U64(0));
    }

    #[test]
    #[should_panic(expected = "expires_at_ms must be in the future")]
    fn a_mandate_that_already_ended_cannot_be_registered() {
        testing_env!(owner(10).build());
        let mut v = Vault::new(acct("alice.near"));
        let f = fixtures();
        v.add_pq_agent(
            serde_json::from_value(f["public_key_b64"].clone()).unwrap(),
            U64(1),
            U64(0),
        );
    }

    #[test]
    fn get_mandate_reports_the_terms() {
        let (v, key_id) = pq_setup(NOW_MS + 1_000_000, 60_000);
        let m = v.get_mandate(key_id.clone(), NATIVE.into()).unwrap();
        assert_eq!(m.vault, acct("vault.alice.near"));
        assert_eq!(m.owner, acct("alice.near"));
        assert_eq!(
            (m.expires_at_ms.0, m.min_interval_ms.0),
            (NOW_MS + 1_000_000, 60_000)
        );
        assert_eq!(
            (m.per_tx_max.0, m.daily_max.0),
            (2 * 10u128.pow(24), 3 * 10u128.pow(24))
        );
        assert!(v
            .get_mandate(key_id.clone(), "usdc.test.near".into())
            .is_none());
        assert!(v.get_mandate("nope".into(), NATIVE.into()).is_none());
        assert_eq!(v.list_pq_agents().len(), 1);
    }
}
