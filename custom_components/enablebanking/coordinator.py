"""Coordinator: ticks every TICK_INTERVAL to refresh local data for entities,
and every `scan_interval_hours` (budget permitting) asks the SyncEngine to
pull fresh data from the bank. Sensors always read from local storage.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import EnableBankingApi
from .const import (
    CONF_ACCOUNTS,
    CONF_SCAN_INTERVAL_HOURS,
    CONF_VALID_UNTIL,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    EVENT_SYNC_FINISHED,
    EXPIRY_WARNING_DAYS,
    ISSUE_CONSENT_EXPIRING,
    TICK_INTERVAL,
)
from .store import Store
from .sync import STATUS_AUTH_EXPIRED, SyncEngine, SyncResult

_LOGGER = logging.getLogger(__name__)


class EnableBankingCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """coordinator.data = Store.snapshot(): local-only, always available."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: EnableBankingApi, store: Store) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=TICK_INTERVAL)
        self.store = store
        self.accounts: list[dict[str, Any]] = entry.data[CONF_ACCOUNTS]
        self.account_hashes = [a["identification_hash"] for a in self.accounts]
        self.setup_options = dict(entry.options)
        self.engine = SyncEngine(hass, store, api, self.accounts)
        self.psu_headers: dict[str, str] | None = None

    def _scan_interval(self) -> timedelta:
        hours = self.config_entry.options.get(CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS)
        return timedelta(hours=hours)

    async def async_request_sync(self, force: bool = False) -> dict[str, Any]:
        """Used by the `sync_now` service. Never raises for bank-side errors."""
        due, why = await self.engine.async_is_due(self._scan_interval(), force)
        if not due:
            return {"ran": False, "reason": why}
        result = await self._async_do_sync("service")
        self.async_set_updated_data(await self._read_snapshot())
        return {
            "ran": True,
            "status": result.status,
            "new_transactions": result.new_transactions,
            "updated_transactions": result.updated_transactions,
            "accounts_synced": result.accounts_synced,
        }

    async def _async_do_sync(self, reason: str) -> SyncResult:
        result = await self.engine.async_run(reason, psu_headers=self.psu_headers)
        self.hass.bus.async_fire(EVENT_SYNC_FINISHED, {"status": result.status, "reason": reason})
        if result.status == STATUS_AUTH_EXPIRED:
            self._maybe_warn_expiry(force=True)
            raise ConfigEntryAuthFailed("The bank authorisation has expired")
        return result

    async def _read_snapshot(self) -> dict[str, dict[str, Any]]:
        return await self.hass.async_add_executor_job(self.store.snapshot, self.account_hashes)

    def _maybe_warn_expiry(self, force: bool = False) -> None:
        valid_until = dt_util.parse_datetime(self.config_entry.data.get(CONF_VALID_UNTIL) or "")
        if not valid_until:
            return
        remaining = valid_until - dt_util.utcnow()
        if force or remaining <= timedelta(days=EXPIRY_WARNING_DAYS):
            ir.async_create_issue(
                self.hass, DOMAIN, f"{ISSUE_CONSENT_EXPIRING}_{self.config_entry.entry_id}",
                is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_CONSENT_EXPIRING,
                translation_placeholders={"title": self.config_entry.title, "date": valid_until.date().isoformat()},
            )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        due, _why = await self.engine.async_is_due(self._scan_interval(), force=False)
        if due:
            await self._async_do_sync("scheduled")  # ConfigEntryAuthFailed propagates on purpose
        else:
            self._maybe_warn_expiry()
        return await self._read_snapshot()
