"""HTTP view that receives the browser redirect from Enable Banking.

Flow: bank login finishes -> browser goes to <HA>/enablebanking/callback?code=...&state=...
-> this view looks up which config flow started that login (via `state`) and resumes it.
"""
from __future__ import annotations

import html

from aiohttp import web

from homeassistant.util import dt as dt_util

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import UnknownFlow

from .const import (
    CALLBACK_PATH,
    DATA_PENDING_STATES,
    DATA_PSU_HEADERS,
    DATA_VIEW_REGISTERED,
    DOMAIN,
)


def _page(message: str, status: int = 200) -> web.Response:
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Enable Banking</title></head>"
        "<body style='font-family:sans-serif;max-width:32em;margin:3em auto;padding:0 1em'>"
        f"<h2>Enable Banking</h2><p>{html.escape(message)}</p>"
        "<script>if(window.opener||window.history.length<2){setTimeout(window.close,1500)}</script>"
        "</body></html>"
    )
    return web.Response(text=body, content_type="text/html", status=status)


class EnableBankingCallbackView(HomeAssistantView):
    """Receives ?code=...&state=... after the user authorises at the bank."""

    url = CALLBACK_PATH
    name = "enablebanking:callback"
    # The browser arrives from the bank without a Home Assistant token. Access is
    # protected by `state`: an unguessable, single-use value created by the flow.
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        query = request.query
        state = query.get("state", "")
        domain_data = self.hass.data.get(DOMAIN, {})
        pending: dict[str, str] = domain_data.get(DATA_PENDING_STATES, {})
        flow_id = pending.pop(state, None) if state else None  # single use

        # Best-effort PSU-presence signal for the *initial* transaction download (the API
        # allows a longer history for a short window while the user is present). This is
        # a proxy: the browser calling this HA endpoint, not calling the bank directly.
        if state and flow_id is not None:
            domain_data.setdefault(DATA_PSU_HEADERS, {})[state] = {
                "Psu-Ip-Address": request.remote or "",
                "Psu-User-Agent": request.headers.get("User-Agent", ""),
                "Psu-Accept-Language": request.headers.get("Accept-Language", ""),
                "captured_at": dt_util.utcnow().isoformat(),
            }
        if flow_id is None:
            return _page(
                "Enlace no válido o caducado. Vuelve a Home Assistant e inicia "
                "el proceso de nuevo. / Invalid or expired link.",
                400,
            )

        if "error" in query:
            user_input = {
                "error": query.get("error", ""),
                "error_description": query.get("error_description", ""),
            }
        elif query.get("code"):
            user_input = {"code": query["code"]}
        else:
            user_input = {"error": "missing_code", "error_description": ""}

        try:
            await self.hass.config_entries.flow.async_configure(
                flow_id=flow_id, user_input=user_input
            )
        except UnknownFlow:
            return _page(
                "El proceso de configuración ya no existe. Inícialo de nuevo "
                "desde Home Assistant. / The setup flow no longer exists.",
                400,
            )
        return _page(
            "Autorización recibida. Ya puedes cerrar esta ventana y volver a "
            "Home Assistant. / Authorisation received, you can close this window."
        )


def async_register_callback_view(hass: HomeAssistant) -> None:
    """Register the view once (safe to call many times)."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(DATA_VIEW_REGISTERED):
        hass.http.register_view(EnableBankingCallbackView(hass))
        domain_data[DATA_VIEW_REGISTERED] = True
