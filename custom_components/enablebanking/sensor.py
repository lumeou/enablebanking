"""Sensors: one balance and one 'last transaction' per account."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BALANCE_PRIORITY, CONF_ASPSP_NAME, DOMAIN
from .coordinator import EnableBankingCoordinator


def _to_decimal(amount: Any) -> Decimal | None:
    try:
        return Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return None


def select_balance(balances: list[dict]) -> dict | None:
    """Pick the most meaningful balance according to BALANCE_PRIORITY."""
    by_type: dict[str, dict] = {}
    for balance in balances:
        by_type.setdefault(balance.get("balance_type", ""), balance)
    for balance_type in BALANCE_PRIORITY:
        if balance_type in by_type:
            return by_type[balance_type]
    return balances[0] if balances else None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EnableBankingCoordinator = entry.runtime_data
    entities: list[EnableBankingEntity] = []
    for account in coordinator.accounts:
        entities.append(BalanceSensor(coordinator, account))
        entities.append(LastTransactionSensor(coordinator, account))
    async_add_entities(entities)


class EnableBankingEntity(CoordinatorEntity[EnableBankingCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: EnableBankingCoordinator, account: dict, key: str) -> None:
        super().__init__(coordinator)
        self._account = account
        self._hash = account["identification_hash"]  # stable across re-logins
        self._attr_unique_id = f"{self._hash}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._hash)},
            name=account["title"],
            manufacturer=coordinator.config_entry.data[CONF_ASPSP_NAME],
            model="Bank account",
        )

    @property
    def _data(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._hash, {})


class BalanceSensor(EnableBankingEntity):
    def __init__(self, coordinator: EnableBankingCoordinator, account: dict) -> None:
        super().__init__(coordinator, account, "balance")

    @property
    def _balance(self) -> dict | None:
        return select_balance(self._data.get("balances", []))

    @property
    def native_value(self) -> Decimal | None:
        balance = self._balance
        return _to_decimal(balance["balance_amount"]["amount"]) if balance else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        balance = self._balance
        return (balance["balance_amount"].get("currency") if balance else None) or self._account.get("currency") or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        balance = self._balance or {}
        details = self._data.get("details", {})
        return {
            "iban": self._account.get("iban"),
            "balance_type": balance.get("balance_type"),
            "reference_date": balance.get("reference_date"),
            "last_change": balance.get("last_change_date_time"),
            "account_type": details.get("cash_account_type"),
            "product": details.get("product"),
            "credit_limit": details.get("credit_limit"),
            "all_balances": self._data.get("balances", []),
        }


class LastTransactionSensor(EnableBankingEntity):
    # Full transaction (names, IBANs...) is shown live but kept out of the history database.
    _unrecorded_attributes = frozenset({"transaction"})

    def __init__(self, coordinator: EnableBankingCoordinator, account: dict) -> None:
        super().__init__(coordinator, account, "last_transaction")

    @property
    def _latest(self) -> dict | None:
        transactions = self._data.get("transactions") or []
        return transactions[0] if transactions else None

    @property
    def native_value(self) -> Decimal | None:
        tx = self._latest
        if tx is None:
            return None
        value = _to_decimal((tx.get("transaction_amount") or {}).get("amount"))
        if value is not None and tx.get("credit_debit_indicator") == "DBIT":
            value = -value
        return value

    @property
    def native_unit_of_measurement(self) -> str | None:
        tx = self._latest
        currency = ((tx or {}).get("transaction_amount") or {}).get("currency")
        return currency or self._account.get("currency") or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        transactions = self._data.get("transactions") or []
        tx = self._latest
        if tx is None:
            return {"transactions_loaded": 0}
        outgoing = tx.get("credit_debit_indicator") == "DBIT"
        party = tx.get("creditor" if outgoing else "debtor") or {}
        return {
            "booking_date": tx.get("booking_date"),
            "status": tx.get("status"),
            "counterparty": party.get("name"),
            "description": " | ".join(tx.get("remittance_information") or []),
            "transactions_loaded": len(transactions),
            "transaction": tx,
        }
