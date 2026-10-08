"""The sync engine: the only piece that talks to Enable Banking.

Everything else (sensors, services) reads from the local Store. This module
decides WHETHER to call the bank (budget, backoff, rate limits) and WHAT to
ask for (first sync uses strategy=longest; later syncs are incremental).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import EnableBankingApi, EnableBankingApiError, EnableBankingConnectionError, EnableBankingKeyError
from .categories import ensure_rules_file, load_rules
from .const import (
    DAILY_SYNC_BUDGET,
    DB_DIR,
    EVENT_NEW_TRANSACTIONS,
    GENERIC_429_BLOCK,
    MAX_TX_PAGES,
    OVERLAP_DAYS,
    PSU_HEADERS_TTL_SECONDS,
    RATE_LIMIT_BLOCK,
    RETRY_BACKOFF,
    SESSION_AUTH_ERROR,
    SESSION_EXPIRED,
    SESSION_OK,
)
from .store import Store

_LOGGER = logging.getLogger(__name__)

# Error codes documented/observed as meaning "the consent/session itself is gone".
SESSION_EXPIRED_CODES = {"EXPIRED_SESSION", "SESSION_EXPIRED", "CONSENT_EXPIRED", "INVALID_SESSION"}
RATE_LIMIT_CODES = {"ASPSP_RATE_LIMIT_EXCEEDED"}
RETRYABLE_PERIOD_CODES = {"WRONG_TRANSACTIONS_PERIOD"}

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_SKIPPED_BUDGET = "skipped_budget"
STATUS_SKIPPED_BACKOFF = "skipped_backoff"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_AUTH_EXPIRED = "auth_expired"
STATUS_AUTH_ERROR = "auth_error"
STATUS_ERROR = "error"


@dataclass
class SyncResult:
    status: str
    accounts_synced: int = 0
    new_transactions: int = 0
    updated_transactions: int = 0
    message: str = ""
    new_by_account: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _now_iso() -> str:
    return dt_util.utcnow().isoformat()


class SyncEngine:
    def __init__(
        self, hass: HomeAssistant, store: Store, api: EnableBankingApi, accounts: list[dict[str, Any]],
        entry_id: str = "", bank_title: str = "",
    ) -> None:
        self.hass = hass
        self.store = store
        self.api = api
        self.accounts = accounts  # [{"uid","identification_hash","title","iban","currency"}, ...]
        self.entry_id = entry_id
        self.bank_title = bank_title

    # ------------------------------------------------------------ scheduling
    async def async_is_due(self, scan_interval: timedelta, force: bool) -> tuple[bool, str]:
        """Return (due, reason_if_not)."""
        meta = await self.hass.async_add_executor_job(self.store.get_meta_all)
        now = dt_util.utcnow()

        blocked_until = meta.get("blocked_until")
        if blocked_until and not force:
            until = dt_util.parse_datetime(blocked_until)
            if until and now < until:
                return False, f"blocked until {blocked_until}"

        if force:
            return True, ""

        last_started = await self.hass.async_add_executor_job(self.store.last_run_started)
        if last_started:
            last_dt = dt_util.parse_datetime(last_started)
            if last_dt and now - last_dt < scan_interval:
                return False, "not due yet"

        since = (now - timedelta(hours=24)).isoformat()
        runs = await self.hass.async_add_executor_job(self.store.runs_since, since)
        if len(runs) >= DAILY_SYNC_BUDGET:
            return False, "daily budget reached"

        return True, ""

    # ------------------------------------------------------------------ run
    async def async_run(self, reason: str, psu_headers: dict[str, str] | None = None) -> SyncResult:
        now = _now_iso()
        run_id = await self.hass.async_add_executor_job(self.store.start_run, reason, now)
        api_calls = 0
        new_total = updated_total = accounts_done = 0
        new_by_account: dict[str, list[dict[str, Any]]] = {}
        overall_status = STATUS_OK

        try:
            initial_needed = set(await self.hass.async_add_executor_job(self.store.accounts_needing_initial))
            # Loaded once per run, not per account/page: categories.yaml is shared
            # across every connected bank, and a new transaction gets categorized
            # right at insert time (see Store.upsert_transactions) - no separate
            # "recategorize" step needed for newly downloaded transactions.
            rules_path = self.hass.config.path(DB_DIR, "categories.yaml")
            await self.hass.async_add_executor_job(ensure_rules_file, rules_path)
            rules = await self.hass.async_add_executor_job(load_rules, rules_path)
            for account in self.accounts:
                acc_hash, uid = account["identification_hash"], account["uid"]
                is_initial = acc_hash in initial_needed
                headers = psu_headers if (is_initial and psu_headers) else None
                try:
                    if not await self.hass.async_add_executor_job(self.store.has_details, acc_hash):
                        details = await self.api.get_account_details(uid, psu_headers=headers)
                        api_calls += 1
                        await self.hass.async_add_executor_job(self.store.set_details, acc_hash, details)

                    balances = await self.api.get_balances(uid, psu_headers=headers)
                    api_calls += 1
                    await self.hass.async_add_executor_job(
                        self.store.add_balances, acc_hash, balances, now
                    )

                    new_rows, upd = await self._sync_transactions(acc_hash, uid, is_initial, headers, rules)
                    api_calls += 1  # at least one call was made (page counting happens inside)
                    if new_rows:
                        new_by_account[acc_hash] = new_rows
                    new_total += len(new_rows)
                    updated_total += upd
                    accounts_done += 1
                    if is_initial:
                        await self.hass.async_add_executor_job(self.store.mark_initial_done, acc_hash, now)
                    await self.hass.async_add_executor_job(self.store.set_meta, {"blocked_until": None, "consecutive_errors": "0"})

                except EnableBankingApiError as err:
                    classified = await self._handle_api_error(err)
                    if classified in (STATUS_AUTH_EXPIRED, STATUS_RATE_LIMITED):
                        overall_status = classified
                        break  # stop the whole run: these block every account equally
                    overall_status = STATUS_PARTIAL
                    _LOGGER.warning("Enable Banking: account %s failed (%s): %s", account["title"], classified, err)
                    continue
                except (EnableBankingConnectionError, EnableBankingKeyError) as err:
                    overall_status = await self._handle_other_error(err)
                    break

        finally:
            await self.hass.async_add_executor_job(
                self.store.finish_run, run_id, _now_iso(), overall_status, None, None, None,
                accounts_done, new_total, updated_total, api_calls,
            )

        if new_total:
            self.hass.bus.async_fire(
                EVENT_NEW_TRANSACTIONS,
                {
                    "entry_id": self.entry_id,
                    "bank": self.bank_title,
                    "new_transactions": new_total,
                    "accounts": list(new_by_account.keys()),
                },
            )
        return SyncResult(overall_status, accounts_done, new_total, updated_total, "", new_by_account)

    async def _sync_transactions(
        self, acc_hash: str, uid: str, is_initial: bool, headers: dict[str, str] | None,
        rules: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        if is_initial:
            date_from, strategy = None, "longest"
        else:
            latest = await self.hass.async_add_executor_job(self.store.latest_effective_date, acc_hash)
            date_from, strategy = None, None
            if latest:
                start = dt_util.parse_date(latest) - timedelta(days=OVERLAP_DAYS)
                date_from = start.isoformat()

        all_tx: list[dict[str, Any]] = []
        continuation_key: str | None = None
        for _ in range(MAX_TX_PAGES):
            try:
                page = await self.api.get_transactions_page(
                    uid, date_from=date_from, strategy=strategy,
                    continuation_key=continuation_key, psu_headers=headers,
                )
            except EnableBankingApiError as err:
                if err.error_code in RETRYABLE_PERIOD_CODES and (date_from or strategy):
                    # Retry once without a date filter / strategy hint.
                    date_from, strategy, continuation_key = None, None, None
                    continue
                raise
            all_tx.extend(page.get("transactions", []))
            continuation_key = page.get("continuation_key")
            if not continuation_key:
                break
        else:
            _LOGGER.warning("Enable Banking: reached the %s-page safety limit for one account", MAX_TX_PAGES)

        result = await self.hass.async_add_executor_job(
            self.store.upsert_transactions, acc_hash, all_tx, _now_iso(), rules
        )
        return result["new"], result["updated"]

    # --------------------------------------------------------------- errors
    async def _handle_api_error(self, err: EnableBankingApiError) -> str:
        now = dt_util.utcnow()
        if err.status == 401:
            if err.error_code in SESSION_EXPIRED_CODES or err.error_code is None:
                await self.hass.async_add_executor_job(
                    self.store.set_meta, {"session_status": SESSION_EXPIRED}
                )
                return STATUS_AUTH_EXPIRED
            await self.hass.async_add_executor_job(
                self.store.set_meta, {"session_status": SESSION_AUTH_ERROR}
            )
            return STATUS_AUTH_ERROR
        if err.status == 429:
            block = RATE_LIMIT_BLOCK if err.error_code in RATE_LIMIT_CODES else GENERIC_429_BLOCK
            until = (now + block).isoformat()
            await self.hass.async_add_executor_job(self.store.set_meta, {"blocked_until": until})
            _LOGGER.info("Enable Banking: rate limited (%s), pausing until %s", err.error_code, until)
            return STATUS_RATE_LIMITED
        return await self._handle_other_error(err)

    async def _handle_other_error(self, err: Exception) -> str:
        meta = await self.hass.async_add_executor_job(self.store.get_meta_all)
        consecutive = int(meta.get("consecutive_errors", "0")) + 1
        backoff = RETRY_BACKOFF[min(consecutive - 1, len(RETRY_BACKOFF) - 1)]
        until = (dt_util.utcnow() + backoff).isoformat()
        await self.hass.async_add_executor_job(
            self.store.set_meta, {"blocked_until": until, "consecutive_errors": str(consecutive)}
        )
        _LOGGER.warning("Enable Banking: %s, retrying no sooner than %s", err, until)
        return STATUS_ERROR
