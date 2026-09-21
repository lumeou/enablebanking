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
    """The API answered with an HTTP error."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


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

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._get_jwt()}"}
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

    async def delete_session(self, session_id: str) -> dict:
        return await self._request("DELETE", f"/sessions/{session_id}")

    # ---- account data
    async def get_account_details(self, uid: str) -> dict:
        return await self._request("GET", f"/accounts/{uid}/details")

    async def get_balances(self, uid: str) -> list[dict]:
        data = await self._request("GET", f"/accounts/{uid}/balances")
        return data.get("balances", [])

    async def get_transactions(
        self,
        uid: str,
        date_from: str | None = None,
        date_to: str | None = None,
        max_pages: int = 5,
    ) -> list[dict]:
        """Fetch transactions, following `continuation_key` up to max_pages."""
        params: dict[str, str] = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        transactions: list[dict] = []
        continuation_key: str | None = None
        for _ in range(max_pages):
            page = dict(params)
            if continuation_key:
                page["continuation_key"] = continuation_key
            data = await self._request(
                "GET", f"/accounts/{uid}/transactions", params=page
            )
            transactions.extend(data.get("transactions", []))
            continuation_key = data.get("continuation_key")
            if not continuation_key:
                break
        return transactions

    async def get_transaction_details(self, uid: str, transaction_id: str) -> dict:
        return await self._request(
            "GET", f"/accounts/{uid}/transactions/{transaction_id}"
        )
