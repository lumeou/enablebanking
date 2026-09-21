"""DataUpdateCoordinator: fetches balances and transactions for every account."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    EnableBankingApi,
    EnableBankingApiError,
    EnableBankingConnectionError,
    EnableBankingKeyError,
)
from .const import (
    CONF_ACCOUNTS,
    CONF_SCAN_INTERVAL_HOURS,
    CONF_SESSION_ID,
    CONF_TRANSACTION_DAYS,
    CONF_VALID_UNTIL,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DEFAULT_TRANSACTION_DAYS,
    DOMAIN,
    MAX_PAGES,
    MAX_TRANSACTIONS,
)

_LOGGER = logging.getLogger(__name__)


def _tx_sort_key(tx: dict[str, Any]) -> str:
    return tx.get("booking_date") or tx.get("value_date") or tx.get("transaction_date") or ""


class EnableBankingCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """data = {identification_hash: {"details", "balances", "transactions"}}"""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: EnableBankingApi) -> None:
        hours = entry.options.get(CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(hours=hours),
        )
        self.api = api
        self.accounts: list[dict[str, Any]] = entry.data[CONF_ACCOUNTS]
        self.setup_options = dict(entry.options)  # to detect option changes later
        self._details: dict[str, dict] = {}  # rarely changes: fetched once

    def _check_expiry(self) -> None:
        valid_until = dt_util.parse_datetime(self.config_entry.data.get(CONF_VALID_UNTIL) or "")
        if valid_until and dt_util.utcnow() >= valid_until:
            raise ConfigEntryAuthFailed("The bank authorisation has expired")

    async def _async_raise_if_session_invalid(self) -> None:
        try:
            session = await self.api.get_session(self.config_entry.data[CONF_SESSION_ID])
        except EnableBankingApiError as err:
            if err.status == 404:
                raise ConfigEntryAuthFailed("Session not found") from err
            return
        except (EnableBankingConnectionError, EnableBankingKeyError):
            return
        if session.get("status") != "AUTHORIZED":
            raise ConfigEntryAuthFailed(f"Session status is {session.get('status')}")

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        self._check_expiry()
        days = self.config_entry.options.get(CONF_TRANSACTION_DAYS, DEFAULT_TRANSACTION_DAYS)
        date_from = (dt_util.now().date() - timedelta(days=days)).isoformat() if days else None

        result: dict[str, dict[str, Any]] = {}
        try:
            for account in self.accounts:
                key, uid = account["identification_hash"], account["uid"]
                if key not in self._details:
                    try:
                        self._details[key] = await self.api.get_account_details(uid)
                    except EnableBankingApiError as err:
                        _LOGGER.debug("Account details not available: %s", err)
                        self._details[key] = {}
                balances = await self.api.get_balances(uid)
                transactions = await self.api.get_transactions(
                    uid, date_from=date_from, max_pages=MAX_PAGES
                )
                transactions.sort(key=_tx_sort_key, reverse=True)  # newest first
                result[key] = {
                    "details": self._details[key],
                    "balances": balances,
                    "transactions": transactions[:MAX_TRANSACTIONS],
                }
        except EnableBankingApiError as err:
            await self._async_raise_if_session_invalid()
            raise UpdateFailed(f"Enable Banking API error: {err}") from err
        except (EnableBankingConnectionError, EnableBankingKeyError) as err:
            raise UpdateFailed(f"Cannot reach Enable Banking: {err}") from err
        return result
