"""Config flow: form -> bank login in the browser -> callback view resumes -> entry."""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util

from .api import (
    EnableBankingApi,
    EnableBankingApiError,
    EnableBankingConnectionError,
    EnableBankingKeyError,
    read_private_key,
)
from .callback_view import async_register_callback_view
from .const import (
    CALLBACK_PATH,
    CONF_ACCOUNTS,
    CONF_APPLICATION_ID,
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_PRIVATE_KEY_PATH,
    CONF_REDIRECT_URL,
    CONF_SCAN_INTERVAL_HOURS,
    CONF_SESSION_ID,
    CONF_TRANSACTION_DAYS,
    CONF_VALID_UNTIL,
    DATA_PENDING_STATES,
    DEFAULT_ASPSP_COUNTRY,
    DEFAULT_ASPSP_NAME,
    DEFAULT_REDIRECT_BASE,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DEFAULT_TRANSACTION_DAYS,
    DOMAIN,
    FALLBACK_CONSENT_DAYS,
    MAX_CONSENT_DAYS,
)

_LOGGER = logging.getLogger(__name__)

STATIC_KEYS = (
    CONF_APPLICATION_ID,
    CONF_PRIVATE_KEY_PATH,
    CONF_ASPSP_NAME,
    CONF_ASPSP_COUNTRY,
    CONF_REDIRECT_URL,
)


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/")


def _key_path_allowed(config_dir: str, path: str) -> bool:
    """Blocking (disk access): key must live in the config dir but NOT in www/ (public)."""
    root = Path(config_dir).resolve()
    resolved = Path(path).resolve()
    return resolved.is_relative_to(root) and not resolved.is_relative_to(root / "www")


def _account_title(account: dict, index: int) -> str:
    iban = (account.get("account_id") or {}).get("iban")
    return account.get("details") or iban or account.get("name") or f"Account {index + 1}"


class EnableBankingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Enable Banking."""

    VERSION = 1

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._api: EnableBankingApi | None = None
        self._state: str | None = None
        self._code: str | None = None
        self._auth_error = ""
        self._registered_urls = "-"
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnableBankingOptionsFlow()

    @callback
    def async_remove(self) -> None:
        """Flow is going away: forget the pending state."""
        if self._state:
            self.hass.data.get(DOMAIN, {}).get(DATA_PENDING_STATES, {}).pop(
                self._state, None
            )

    # ------------------------------------------------------------------ form
    def _default_redirect(self) -> str:
        try:
            base = get_url(
                self.hass, allow_external=False, allow_cloud=False, prefer_external=False
            )
        except NoURLAvailableError:
            base = DEFAULT_REDIRECT_BASE
        return f"{base.rstrip('/')}{CALLBACK_PATH}"

    def _user_schema(self, defaults: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_APPLICATION_ID, default=defaults.get(CONF_APPLICATION_ID, "")): str,
                vol.Required(CONF_PRIVATE_KEY_PATH, default=defaults.get(CONF_PRIVATE_KEY_PATH) or self.hass.config.path("enablebanking", "private.key")): str,
                vol.Required(CONF_ASPSP_NAME, default=defaults.get(CONF_ASPSP_NAME, DEFAULT_ASPSP_NAME)): str,
                vol.Required(CONF_ASPSP_COUNTRY, default=defaults.get(CONF_ASPSP_COUNTRY, DEFAULT_ASPSP_COUNTRY)): str,
                vol.Required(CONF_REDIRECT_URL, default=defaults.get(CONF_REDIRECT_URL) or self._default_redirect()): str,
            }
        )

    async def _async_create_api(self, config: dict[str, Any]) -> EnableBankingApi:
        key = await self.hass.async_add_executor_job(
            read_private_key, config[CONF_PRIVATE_KEY_PATH]
        )
        return EnableBankingApi(
            async_get_clientsession(self.hass), config[CONF_APPLICATION_ID], key
        )

    async def _async_validate(self, config: dict[str, Any]) -> dict[str, str]:
        """Return a dict of errors (empty when everything is fine)."""
        path = config[CONF_PRIVATE_KEY_PATH]
        if not await self.hass.async_add_executor_job(
            _key_path_allowed, self.hass.config.config_dir, path
        ):
            return {CONF_PRIVATE_KEY_PATH: "key_path_not_allowed"}
        if urlparse(config[CONF_REDIRECT_URL]).path.rstrip("/") != CALLBACK_PATH:
            return {CONF_REDIRECT_URL: "redirect_bad_path"}
        try:
            api = await self._async_create_api(config)
        except OSError:
            return {CONF_PRIVATE_KEY_PATH: "key_not_found"}

        try:
            app = await api.get_application()
        except EnableBankingKeyError:
            return {CONF_PRIVATE_KEY_PATH: "invalid_key"}
        except EnableBankingConnectionError:
            return {"base": "cannot_connect"}
        except EnableBankingApiError as err:
            _LOGGER.debug("Validation failed: %s", err)
            return {"base": "invalid_auth" if err.status in (401, 403) else "unknown"}

        registered = [_norm_url(u) for u in app.get("redirect_urls", [])]
        self._registered_urls = ", ".join(app.get("redirect_urls", [])) or "-"
        if _norm_url(config[CONF_REDIRECT_URL]) not in registered:
            return {CONF_REDIRECT_URL: "redirect_not_registered"}
        self._api = api
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = {
                **{k: str(v).strip() for k, v in user_input.items()},
                CONF_ASPSP_COUNTRY: user_input[CONF_ASPSP_COUNTRY].strip().upper(),
            }
            errors = await self._async_validate(user_input)
            if not errors:
                await self.async_set_unique_id(
                    f"{user_input[CONF_APPLICATION_ID]}_{user_input[CONF_ASPSP_NAME]}_"
                    f"{user_input[CONF_ASPSP_COUNTRY]}"
                )
                self._abort_if_unique_id_configured()
                self._config = user_input
                return await self.async_step_authorize()
        # Registering early guarantees the callback URL is served before the login.
        async_register_callback_view(self.hass)
        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(user_input or {}),
            errors=errors,
            description_placeholders={"registered": self._registered_urls},
        )

    # ------------------------------------------------------- bank login (external)
    async def _async_consent_seconds(self) -> int:
        """Longest consent the bank allows (capped); soft-fails to a safe default."""
        assert self._api is not None
        name = self._config[CONF_ASPSP_NAME].lower()
        try:
            aspsps = await self._api.list_aspsps(self._config[CONF_ASPSP_COUNTRY])
        except EnableBankingApiError:
            aspsps = []
        for aspsp in aspsps:
            if aspsp.get("name", "").lower() == name:
                maximum = aspsp.get("maximum_consent_validity")
                if maximum:
                    return min(int(maximum), MAX_CONSENT_DAYS * 86400)
        return FALLBACK_CONSENT_DAYS * 86400

    async def async_step_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Second time we get here it is the callback view resuming the flow.
        if user_input is not None:
            if "error" in user_input:
                # An external step may only move to "external step done"; the abort
                # itself happens in the next (normal) step.
                self._auth_error = (
                    f"{user_input['error']} {user_input.get('error_description', '')}".strip()
                )
                return self.async_external_step_done(next_step_id="failed")
            self._code = user_input["code"]
            return self.async_external_step_done(next_step_id="finish")

        assert self._api is not None
        self._state = str(uuid.uuid4())
        self.hass.data.setdefault(DOMAIN, {}).setdefault(DATA_PENDING_STATES, {})[
            self._state
        ] = self.flow_id
        valid_until = dt_util.utcnow() + timedelta(seconds=await self._async_consent_seconds())
        try:
            auth = await self._api.start_authorization(
                aspsp_name=self._config[CONF_ASPSP_NAME],
                aspsp_country=self._config[CONF_ASPSP_COUNTRY],
                redirect_url=self._config[CONF_REDIRECT_URL],
                valid_until_iso=valid_until.isoformat(),
                state=self._state,
            )
        except (EnableBankingApiError, EnableBankingConnectionError) as err:
            return self.async_abort(
                reason="authorization_failed",
                description_placeholders={"error": str(err)},
            )
        return self.async_external_step(step_id="authorize", url=auth["url"])

    async def async_step_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(
            reason="authorization_failed",
            description_placeholders={"error": self._auth_error},
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._api is not None and self._code is not None
        try:
            session = await self._api.create_session(self._code)
        except (EnableBankingApiError, EnableBankingConnectionError) as err:
            return self.async_abort(
                reason="session_failed", description_placeholders={"error": str(err)}
            )

        accounts = []
        for index, account in enumerate(session.get("accounts", [])):
            if not account.get("uid"):  # blocked/closed accounts have no uid
                continue
            accounts.append(
                {
                    "uid": account["uid"],
                    "identification_hash": account["identification_hash"],
                    "title": _account_title(account, index),
                    "iban": (account.get("account_id") or {}).get("iban", ""),
                    "currency": account.get("currency", ""),
                }
            )
        if not accounts:
            return self.async_abort(reason="no_accounts")

        data = {
            **self._config,
            CONF_SESSION_ID: session["session_id"],
            CONF_ACCOUNTS: accounts,
            CONF_VALID_UNTIL: (session.get("access") or {}).get("valid_until"),
        }
        if self._reauth_entry is not None:
            return self.async_update_reload_and_abort(
                self._reauth_entry, data=data, reason="reauth_successful"
            )
        return self.async_create_entry(
            title=f"{self._config[CONF_ASPSP_NAME]} ({self._config[CONF_ASPSP_COUNTRY]})",
            data=data,
        )

    # ---------------------------------------------------------------- reauth
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._config = {k: entry_data[k] for k in STATIC_KEYS}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            async_register_callback_view(self.hass)
            return self.async_show_form(step_id="reauth_confirm")
        try:
            self._api = await self._async_create_api(self._config)
        except OSError:
            return self.async_abort(reason="key_not_found")
        return await self.async_step_authorize()


class EnableBankingOptionsFlow(OptionsFlow):
    """Polling interval and transaction window."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL_HOURS,
                    default=options.get(CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=48)),
                vol.Required(
                    CONF_TRANSACTION_DAYS,
                    default=options.get(CONF_TRANSACTION_DAYS, DEFAULT_TRANSACTION_DAYS),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=730)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
