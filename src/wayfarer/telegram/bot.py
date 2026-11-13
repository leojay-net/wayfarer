"""Long-polling entry point: ``wayfarer-bot`` (or ``python -m wayfarer.telegram.bot``)."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from wayfarer.config import Settings
from wayfarer.cost import CostMeter
from wayfarer.errors import WayfarerError
from wayfarer.intent import IntentExtractor
from wayfarer.near.keys import KeyPair
from wayfarer.near.rpc import DEFAULT_TESTNET_RPC, NearRpc
from wayfarer.nearai import NearAiClient
from wayfarer.receipts import ReceiptLedger
from wayfarer.telegram.api import TelegramApi
from wayfarer.telegram.service import BotService
from wayfarer.vault import TESTNET_EXPLORER, VaultClient

log = logging.getLogger("wayfarer.bot")


def build_service(settings: Settings) -> tuple[TelegramApi, BotService]:
    meter = CostMeter(settings.cost_cap_usd, settings.cost_state_path)
    nearai = NearAiClient(settings, meter=meter)
    api = TelegramApi(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    vault: VaultClient | None = None
    account, key_file = os.environ.get("VAULT_ACCOUNT"), os.environ.get("AGENT_KEY_FILE")
    if account and key_file:
        rpc = NearRpc(os.environ.get("NEAR_RPC_URL", DEFAULT_TESTNET_RPC))
        vault = VaultClient(
            account,
            KeyPair.from_file(Path(key_file)),
            rpc,
            explorer_base=os.environ.get("NEAR_EXPLORER", TESTNET_EXPLORER),
        )
    allowed = os.environ.get("WAYFARER_ALLOWED_CHAT")
    service = BotService(
        api=api,
        nearai=nearai,
        extractor=IntentExtractor(nearai, max_amount=settings.max_amount),
        ledger=ReceiptLedger(settings.ledger_path),
        vault=vault,
        allowed_chat_id=int(allowed) if allowed else None,
        language=settings.language,
    )
    return api, service


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    api, service = build_service(Settings.from_env())
    offset = 0
    log.info("bot started")
    while True:
        try:
            for update in api.get_updates(offset):
                offset = int(update["update_id"]) + 1
                service.handle_update(update)
        except WayfarerError as error:  # polling itself failed (network); keep going
            log.warning("polling error: %s", error)
            time.sleep(3)


if __name__ == "__main__":
    main()
