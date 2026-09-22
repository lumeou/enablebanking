"""Enable Banking integration: local-first account information (balances and transactions)."""
from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .api import EnableBankingApi, read_private_key
from .callback_view import async_register_callback_view
from .categories import ensure_rules_file, load_rules, match_category
from .const import (
    CONF_APPLICATION_ID,
    CONF_PRIVATE_KEY_PATH,
    CONF_SESSION_ID,
    DB_DIR,
    DELETE_DB_ON_REMOVE,
    DOMAIN,
    MAX_SERVICE_RESULTS,
    PSU_HEADERS_TTL_SECONDS,
)
from .coordinator import EnableBankingCoordinator
from .store import Store

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# ---------------------------------------------------------------------------- services
SVC_SYNC_NOW = "sync_now"
SVC_GET_TRANSACTIONS = "get_transactions"
SVC_GET_SUMMARY = "get_summary"
SVC_EXPORT = "export"
SVC_SET_CATEGORY = "set_category"
SVC_RECATEGORIZE = "recategorize"
SVC_LIST_UNCATEGORIZED = "list_uncategorized"

SYNC_NOW_SCHEMA = vol.Schema({vol.Optional("force", default=False): cv.boolean})

TX_FILTER_SCHEMA = {
    vol.Optional("account_iban"): cv.string,
    vol.Optional("date_from"): cv.date,
    vol.Optional("date_to"): cv.date,
    vol.Optional("direction"): vol.In(["income", "expense"]),
    vol.Optional("min_amount"): vol.Coerce(float),
    vol.Optional("max_amount"): vol.Coerce(float),
    vol.Optional("text"): cv.string,
    vol.Optional("category"): cv.string,
    vol.Optional("include_pending", default=False): cv.boolean,
}

GET_TRANSACTIONS_SCHEMA = vol.Schema({
    **TX_FILTER_SCHEMA,
    vol.Optional("limit", default=50): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_SERVICE_RESULTS)),
    vol.Optional("offset", default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional("order", default="desc"): vol.In(["asc", "desc"]),
})

GET_SUMMARY_SCHEMA = vol.Schema({
    **TX_FILTER_SCHEMA,
    vol.Optional("group_by", default="category"): vol.In(["category", "month", "account", "counterparty"]),
})

EXPORT_SCHEMA = vol.Schema({
    **TX_FILTER_SCHEMA,
    vol.Optional("format", default="csv"): vol.In(["csv", "json"]),
})

SET_CATEGORY_SCHEMA = vol.Schema({
    vol.Required("transaction_id"): vol.Coerce(int),
    vol.Required("category"): cv.string,
})

RECATEGORIZE_SCHEMA = vol.Schema({vol.Optional("only_uncategorized", default=True): cv.boolean})

LIST_UNCATEGORIZED_SCHEMA = vol.Schema(
    {vol.Optional("limit", default=20): vol.All(vol.Coerce(int), vol.Range(min=1, max=200))}
)


def _resolve_accounts(hass: HomeAssistant, iban: str | None) -> list[tuple[ConfigEntry, EnableBankingCoordinator, dict]]:
    """All (entry, coordinator, account) loaded and, if `iban` given, matching it."""
    wanted = (iban or "").replace(" ", "").upper()
    out = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        coordinator: EnableBankingCoordinator = entry.runtime_data
        for account in coordinator.accounts:
            if wanted and account.get("iban", "").replace(" ", "").upper() != wanted:
                continue
            out.append((entry, coordinator, account))
    return out


def _account_hashes(hass: HomeAssistant, iban: str | None) -> list[str]:
    return [a["identification_hash"] for _, _, a in _resolve_accounts(hass, iban)]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the OAuth-style callback page and the audit/category services."""
    async_register_callback_view(hass)

    async def _svc_sync_now(call: ServiceCall) -> ServiceResponse:
        results = {}
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            coordinator: EnableBankingCoordinator = entry.runtime_data
            results[entry.title] = await coordinator.async_request_sync(force=call.data["force"])
        return {"results": results}

    async def _svc_get_transactions(call: ServiceCall) -> ServiceResponse:
        hashes = _account_hashes(hass, call.data.get("account_iban"))
        if call.data.get("account_iban") and not hashes:
            raise ServiceValidationError("No account matches that IBAN")
        entry = next(iter(hass.config_entries.async_entries(DOMAIN)), None)
        if entry is None:
            return {"transactions": [], "total": 0}
        store: Store = entry.runtime_data.store
        rows, total = await hass.async_add_executor_job(
            lambda: store.query_transactions(
                account_hashes=hashes if call.data.get("account_iban") else None,
                date_from=call.data.get("date_from").isoformat() if call.data.get("date_from") else None,
                date_to=call.data.get("date_to").isoformat() if call.data.get("date_to") else None,
                direction=call.data.get("direction"), min_amount=call.data.get("min_amount"),
                max_amount=call.data.get("max_amount"), text=call.data.get("text"),
                category=call.data.get("category"), include_pending=call.data["include_pending"],
                limit=call.data["limit"], offset=call.data["offset"], order=call.data["order"],
            )
        )
        return {"transactions": rows, "total": total}

    def _summarize(store: Store, hashes: list[str] | None, call: ServiceCall) -> dict[str, Any]:
        rows, _ = store.query_transactions(
            account_hashes=hashes,
            date_from=call.data.get("date_from").isoformat() if call.data.get("date_from") else None,
            date_to=call.data.get("date_to").isoformat() if call.data.get("date_to") else None,
            direction=call.data.get("direction"), min_amount=call.data.get("min_amount"),
            max_amount=call.data.get("max_amount"), text=call.data.get("text"),
            category=call.data.get("category"), include_pending=False, limit=100000, offset=0,
        )
        group_by = call.data["group_by"]
        groups: dict[str, dict[str, float]] = {}
        income = expense = 0.0
        for row in rows:
            amount = row["amount"] or 0.0
            if amount >= 0:
                income += amount
            else:
                expense += amount
            if group_by == "category":
                key = row["category"] or "Sin categoría"
            elif group_by == "month":
                key = (row["booking_date"] or row["value_date"] or "")[:7] or "Sin fecha"
            elif group_by == "account":
                key = row["account_hash"]
            else:
                key = row["counterparty"] or "Desconocido"
            bucket = groups.setdefault(key, {"income": 0.0, "expense": 0.0, "count": 0})
            bucket["count"] += 1
            if amount >= 0:
                bucket["income"] += amount
            else:
                bucket["expense"] += amount
        return {
            "income": round(income, 2), "expense": round(expense, 2), "net": round(income + expense, 2),
            "transaction_count": len(rows),
            "groups": {k: {"income": round(v["income"], 2), "expense": round(v["expense"], 2),
                          "net": round(v["income"] + v["expense"], 2), "count": v["count"]}
                      for k, v in groups.items()},
        }

    async def _svc_get_summary(call: ServiceCall) -> ServiceResponse:
        hashes = _account_hashes(hass, call.data.get("account_iban")) if call.data.get("account_iban") else None
        entry = next(iter(hass.config_entries.async_entries(DOMAIN)), None)
        if entry is None:
            return {"income": 0, "expense": 0, "net": 0, "transaction_count": 0, "groups": {}}
        store: Store = entry.runtime_data.store
        return await hass.async_add_executor_job(_summarize, store, hashes, call)

    async def _svc_export(call: ServiceCall) -> ServiceResponse:
        hashes = _account_hashes(hass, call.data.get("account_iban")) if call.data.get("account_iban") else None
        entry = next(iter(hass.config_entries.async_entries(DOMAIN)), None)
        if entry is None:
            raise ServiceValidationError("No Enable Banking account is set up")
        store: Store = entry.runtime_data.store
        rows, _ = await hass.async_add_executor_job(
            lambda: store.query_transactions(
                account_hashes=hashes,
                date_from=call.data.get("date_from").isoformat() if call.data.get("date_from") else None,
                date_to=call.data.get("date_to").isoformat() if call.data.get("date_to") else None,
                direction=call.data.get("direction"), min_amount=call.data.get("min_amount"),
                max_amount=call.data.get("max_amount"), text=call.data.get("text"),
                category=call.data.get("category"), include_pending=False, limit=MAX_SERVICE_RESULTS * 20, offset=0,
            )
        )
        out_dir = Path(hass.config.path(DB_DIR, "exports"))
        await hass.async_add_executor_job(lambda: out_dir.mkdir(parents=True, exist_ok=True))
        stamp = dt_util.now().strftime("%Y%m%d_%H%M%S")
        fmt = call.data["format"]
        filename = f"transactions_{stamp}.{fmt}"
        path = out_dir / filename

        def _write() -> None:
            if fmt == "json":
                path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                buf = io.StringIO()
                columns = list(rows[0].keys()) if rows else ["id"]
                writer = csv.DictWriter(buf, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)
                path.write_text(buf.getvalue(), encoding="utf-8-sig")

        await hass.async_add_executor_job(_write)
        return {"path": str(path), "count": len(rows)}

    async def _svc_set_category(call: ServiceCall) -> ServiceResponse:
        entry = next(iter(hass.config_entries.async_entries(DOMAIN)), None)
        if entry is None:
            raise ServiceValidationError("No Enable Banking account is set up")
        store: Store = entry.runtime_data.store

        def _set() -> int:
            with store._lock, store.conn:  # noqa: SLF001 - simple enough to not warrant a Store method
                cur = store.conn.execute(
                    "UPDATE transactions SET category=?, category_source='manual' WHERE id=?",
                    (call.data["category"], call.data["transaction_id"]))
                return cur.rowcount

        updated = await hass.async_add_executor_job(_set)
        if not updated:
            raise ServiceValidationError(f"No transaction with id {call.data['transaction_id']}")
        return {"updated": updated}

    async def _svc_recategorize(call: ServiceCall) -> ServiceResponse:
        entry = next(iter(hass.config_entries.async_entries(DOMAIN)), None)
        if entry is None:
            return {"updated": 0}
        store: Store = entry.runtime_data.store
        rules_path = hass.config.path(DB_DIR, "categories.yaml")

        def _run() -> int:
            ensure_rules_file(rules_path)
            rules = load_rules(rules_path)
            # Manual corrections are never overwritten by rules, regardless of only_uncategorized.
            rows = store.conn.execute(
                "SELECT id, counterparty, remittance, mcc FROM transactions "
                "WHERE (category_source IS NULL OR category_source!='manual')"
                + (" AND category IS NULL" if call.data["only_uncategorized"] else "")
            ).fetchall()
            updated = 0
            with store._lock, store.conn:  # noqa: SLF001
                for row in rows:
                    category = match_category(rules, row["counterparty"], row["remittance"], row["mcc"])
                    if category:
                        store.conn.execute(
                            "UPDATE transactions SET category=?, category_source='rule' WHERE id=?",
                            (category, row["id"]))
                        updated += 1
            return updated

        updated = await hass.async_add_executor_job(_run)
        return {"updated": updated}

    async def _svc_list_uncategorized(call: ServiceCall) -> ServiceResponse:
        entry = next(iter(hass.config_entries.async_entries(DOMAIN)), None)
        if entry is None:
            return {"counterparties": []}
        store: Store = entry.runtime_data.store

        def _list() -> list[dict[str, Any]]:
            rows = store.conn.execute(
                "SELECT counterparty, COUNT(*) AS n, SUM(ABS(amount)) AS total FROM transactions "
                "WHERE category IS NULL AND status!='PDNG' AND counterparty IS NOT NULL "
                "GROUP BY counterparty ORDER BY n DESC LIMIT ?", (call.data["limit"],)).fetchall()
            return [{"counterparty": r["counterparty"], "count": r["n"], "total": round(r["total"] or 0, 2)} for r in rows]

        return {"counterparties": await hass.async_add_executor_job(_list)}

    hass.services.async_register(DOMAIN, SVC_SYNC_NOW, _svc_sync_now, schema=SYNC_NOW_SCHEMA, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SVC_GET_TRANSACTIONS, _svc_get_transactions, schema=GET_TRANSACTIONS_SCHEMA, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SVC_GET_SUMMARY, _svc_get_summary, schema=GET_SUMMARY_SCHEMA, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SVC_EXPORT, _svc_export, schema=EXPORT_SCHEMA, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SVC_SET_CATEGORY, _svc_set_category, schema=SET_CATEGORY_SCHEMA, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SVC_RECATEGORIZE, _svc_recategorize, schema=RECATEGORIZE_SCHEMA, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SVC_LIST_UNCATEGORIZED, _svc_list_uncategorized, schema=LIST_UNCATEGORIZED_SCHEMA, supports_response=SupportsResponse.ONLY)
    return True


def _db_path(hass: HomeAssistant, entry: ConfigEntry) -> str:
    return hass.config.path(DB_DIR, f"{entry.entry_id}.db")


def _fresh_psu_headers(entry: ConfigEntry) -> dict[str, str] | None:
    stored = entry.data.get("psu_headers")
    if not stored:
        return None
    captured_at = dt_util.parse_datetime(stored.get("captured_at", ""))
    if not captured_at or (dt_util.utcnow() - captured_at).total_seconds() > PSU_HEADERS_TTL_SECONDS:
        return None
    return {k: v for k, v in stored.items() if k != "captured_at" and v}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    try:
        key = await hass.async_add_executor_job(read_private_key, entry.data[CONF_PRIVATE_KEY_PATH])
    except OSError as err:
        raise ConfigEntryNotReady(f"Cannot read private key {entry.data[CONF_PRIVATE_KEY_PATH]}: {err}") from err

    api = EnableBankingApi(async_get_clientsession(hass), entry.data[CONF_APPLICATION_ID], key)

    store = Store(_db_path(hass, entry))
    try:
        await hass.async_add_executor_job(store.open)
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"Cannot open local database: {err}") from err
    await hass.async_add_executor_job(
        store.upsert_accounts, entry.data["accounts"], dt_util.utcnow().isoformat()
    )
    await hass.async_add_executor_job(ensure_rules_file, hass.config.path(DB_DIR, "categories.yaml"))

    coordinator = EnableBankingCoordinator(hass, entry, api, store)
    # Only applied by the engine to accounts still pending their very first sync,
    # and only while within PSU_HEADERS_TTL_SECONDS of the bank authorisation.
    coordinator.psu_headers = _fresh_psu_headers(entry)
    entry.runtime_data = coordinator

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await hass.async_add_executor_job(store.close)
        raise

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))

    async def _close_store() -> None:
        await hass.async_add_executor_job(store.close)

    entry.async_on_unload(_close_store)
    return True


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when the *options* changed (re-login reloads through the flow)."""
    if dict(entry.options) != entry.runtime_data.setup_options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Close the bank consent (best effort) and delete the local database."""
    try:
        key = await hass.async_add_executor_job(read_private_key, entry.data[CONF_PRIVATE_KEY_PATH])
        api = EnableBankingApi(async_get_clientsession(hass), entry.data[CONF_APPLICATION_ID], key)
        await api.delete_session(entry.data[CONF_SESSION_ID])
    except Exception as err:  # noqa: BLE001 - never block removal
        _LOGGER.debug("Could not delete the bank session: %s", err)
    if DELETE_DB_ON_REMOVE:
        await hass.async_add_executor_job(Store.delete_files, _db_path(hass, entry))
