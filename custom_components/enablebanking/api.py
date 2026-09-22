"""Async client for the Enable Banking API.

Authentication follows the official Quick Start: a JWT signed with RS256 using the
application's private key, header {"kid": <application id>}, payload
{"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat", "exp"}.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiohttp
import jwt as pyjwt

API_BASE = "https://api.enablebanking.com"


class EnableBankingError(Exception):
    """Base error."""


class EnableBankingKeyError(EnableBankingError):
    """The private key could not be used to sign a token."""


class EnableBankingConnectionError(EnableBankingError):
    """Network problem talking to the API."""


class EnableBankingApiError(EnableBankingError):
    """The API answered with an HTTP error.

    Body format (confirmed from real error responses):
    {"code": 429, "message": "...", "error": "ASPSP_RATE_LIMIT_EXCEEDED", "detail": {...}}
    `code` here is the error code, not a class of error.
    """

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.error_code: str | None = None
        self.detail: dict | None = None
        try:
            parsed = json.loads(body)
            self.error_code = parsed.get("error")
            self.detail = parsed.get("detail")
        except (json.JSONDecodeError, AttributeError):
            pass


def read_private_key(path: str) -> bytes:
    """Blocking file read: always call through hass.async_add_executor_job."""
    return Path(path).read_bytes()


class EnableBankingApi:
    """Thin async wrapper around the endpoints we need."""

    def __init__(
        self, session: aiohttp.ClientSession, application_id: str, private_key: bytes
    ) -> None:
        self._session = session
        self._application_id = application_id
        self._private_key = private_key
        self._jwt: str | None = None
        self._jwt_expires_at = 0

    def _get_jwt(self) -> str:
        now = int(time.time())
        if self._jwt is None or now >= self._jwt_expires_at - 60:
            body = {
                "iss": "enablebanking.com",
                "aud": "api.enablebanking.com",
                "iat": now,
                "exp": now + 3600,
            }
            try:
                self._jwt = pyjwt.encode(
                    body,
                    self._private_key,
                    algorithm="RS256",
                    headers={"kid": self._application_id},
                )
            except (ValueError, TypeError, pyjwt.PyJWTError) as err:
                raise EnableBankingKeyError(str(err)) from err
            self._jwt_expires_at = now + 3600
        return self._jwt

    async def _request(
        self, method: str, path: str, psu_headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        headers = {"Authorization": f"Bearer {self._get_jwt()}", **(psu_headers or {})}
        try:
            async with self._session.request(
                method,
                API_BASE + path,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
                **kwargs,
            ) as response:
                text = await response.text()
        except asyncio.TimeoutError as err:
            raise EnableBankingConnectionError("Timeout") from err
        except aiohttp.ClientError as err:
            raise EnableBankingConnectionError(str(err)) from err
        if response.status >= 400:
            raise EnableBankingApiError(response.status, text)
        return json.loads(text) if text else {}

    # ---- application / banks
    async def get_application(self) -> dict:
        return await self._request("GET", "/application")

    async def list_aspsps(self, country: str) -> list[dict]:
        data = await self._request("GET", "/aspsps", params={"country": country})
        return data.get("aspsps", [])

    # ---- authorisation
    async def start_authorization(
        self,
        aspsp_name: str,
        aspsp_country: str,
        redirect_url: str,
        valid_until_iso: str,
        state: str,
        psu_type: str = "personal",
    ) -> dict:
        body = {
            "access": {"valid_until": valid_until_iso},
            "aspsp": {"name": aspsp_name, "country": aspsp_country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": psu_type,
        }
        return await self._request("POST", "/auth", json=body)

    async def create_session(self, code: str) -> dict:
        return await self._request("POST", "/sessions", json={"code": code})

    async def get_session(self, session_id: str) -> dict:
        return await self._request("GET", f"/sessions/{session_id}")

    async def delete_session(self, session_id: str, psu_headers: dict[str, str] | None = None) -> dict:
        return await self._request("DELETE", f"/sessions/{session_id}", psu_headers=psu_headers)

    # ---- account data
    async def get_account_details(self, uid: str, psu_headers: dict[str, str] | None = None) -> dict:
        return await self._request("GET", f"/accounts/{uid}/details", psu_headers=psu_headers)

    async def get_balances(self, uid: str, psu_headers: dict[str, str] | None = None) -> list[dict]:
        data = await self._request("GET", f"/accounts/{uid}/balances", psu_headers=psu_headers)
        return data.get("balances", [])

    async def get_transactions_page(
        self,
        uid: str,
        date_from: str | None = None,
        date_to: str | None = None,
        continuation_key: str | None = None,
        strategy: str | None = None,
        transaction_status: str | None = None,
        psu_headers: dict[str, str] | None = None,
    ) -> dict:
        """Fetch ONE page. Caller is responsible for following continuation_key.

        A single page can legitimately be an empty transaction list together with a
        continuation_key (per the API docs): that still means "call again".
        """
        params: dict[str, str] = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        if continuation_key:
            params["continuation_key"] = continuation_key
        if strategy:
            params["strategy"] = strategy
        if transaction_status:
            params["transaction_status"] = transaction_status
        return await self._request(
            "GET", f"/accounts/{uid}/transactions", params=params, psu_headers=psu_headers
        )

    async def get_transaction_details(
        self, uid: str, transaction_id: str, psu_headers: dict[str, str] | None = None
    ) -> dict:
        return await self._request(
            "GET", f"/accounts/{uid}/transactions/{transaction_id}", psu_headers=psu_headers
        )
