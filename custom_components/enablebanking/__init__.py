"""Enable Banking integration (account information: balances and transactions)."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import EnableBankingApi, read_private_key
from .callback_view import async_register_callback_view
from .const import (
    ATTR_IBAN,
    ATTR_LIMIT,
    CONF_APPLICATION_ID,
    CONF_PRIVATE_KEY_PATH,
    CONF_SESSION_ID,
    DOMAIN,
    MAX_TRANSACTIONS,
)
from .coordinator import EnableBankingCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

GET_TRANSACTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_LIMIT, default=50): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_TRANSACTIONS)
        ),
        vol.Optional(ATTR_IBAN): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the OAuth-style callback page and the service."""
    async_register_callback_view(hass)

    async def _async_get_transactions(call: ServiceCall) -> ServiceResponse:
        limit = call.data[ATTR_LIMIT]
        wanted = (call.data.get(ATTR_IBAN) or "").replace(" ", "").upper()
        accounts_out = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            coordinator: EnableBankingCoordinator = entry.runtime_data
            for account in coordinator.accounts:
                if wanted and account.get("iban", "").replace(" ", "").upper() != wanted:
                    continue
                data = (coordinator.data or {}).get(account["identification_hash"], {})
                accounts_out.append(
                    {
                        "bank": entry.title,
                        "account": account["title"],
                        "iban": account.get("iban"),
                        "currency": account.get("currency"),
                        "balances": data.get("balances", []),
                        "transactions": data.get("transactions", [])[:limit],
                    }
                )
        return {"accounts": accounts_out}

    hass.services.async_register(
        DOMAIN,
        "get_transactions",
        _async_get_transactions,
        schema=GET_TRANSACTIONS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    try:
        key = await hass.async_add_executor_job(
            read_private_key, entry.data[CONF_PRIVATE_KEY_PATH]
        )
    except OSError as err:
        raise ConfigEntryNotReady(
            f"Cannot read private key {entry.data[CONF_PRIVATE_KEY_PATH]}: {err}"
        ) from err

    api = EnableBankingApi(
        async_get_clientsession(hass), entry.data[CONF_APPLICATION_ID], key
    )
    coordinator = EnableBankingCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    return True


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when the *options* changed (re-login reloads through the flow)."""
    if dict(entry.options) != entry.runtime_data.setup_options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Best effort: close the bank consent when the integration is deleted."""
    try:
        key = await hass.async_add_executor_job(
            read_private_key, entry.data[CONF_PRIVATE_KEY_PATH]
        )
        api = EnableBankingApi(
            async_get_clientsession(hass), entry.data[CONF_APPLICATION_ID], key
        )
        await api.delete_session(entry.data[CONF_SESSION_ID])
    except Exception as err:  # noqa: BLE001 - never block removal
        _LOGGER.debug("Could not delete the bank session: %s", err)
