"""OAuth2 token management (password / refresh grant against /oauth/token)."""

from __future__ import annotations

import asyncio
import time

import httpx

from .config import Config
from .errors import FarmOSAuthError


class OAuthTokenManager:
    """Acquires, caches, and refreshes a farmOS OAuth2 bearer token.

    farmOS 3.x ships Simple OAuth with a default "farm" consumer; the password
    grant is the practical server-to-server flow for an MCP. Static-token mode
    (FARMOS_ACCESS_TOKEN) bypasses all of this.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0

    async def get_token(self, http: httpx.AsyncClient) -> str:
        """Return a valid bearer token, refreshing/re-acquiring as needed."""
        if self._config.access_token:
            return self._config.access_token
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at - 30.0:
                return self._token
            if self._refresh_token:
                try:
                    await self._request(
                        http, grant_type="refresh_token", refresh_token=self._refresh_token
                    )
                    return self._token or ""
                except FarmOSAuthError:
                    pass  # refresh rejected; fall back to the password grant
            await self._request(
                http,
                grant_type="password",
                username=self._config.username or "",
                password=self._config.password or "",
            )
            return self._token or ""

    def invalidate(self) -> None:
        """Drop the cached token (e.g. after a 401) so the next call re-acquires."""
        self._token = None
        self._expires_at = 0.0

    async def _request(self, http: httpx.AsyncClient, **form: str) -> None:
        data = {"client_id": self._config.client_id, **form}
        if self._config.client_secret:
            data["client_secret"] = self._config.client_secret
        if self._config.scope:
            data["scope"] = self._config.scope
        try:
            resp = await http.post(
                "/oauth/token",
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise FarmOSAuthError(f"Could not reach the farmOS OAuth token endpoint: {exc}") from exc

        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
            detail = (
                body.get("error_description") or body.get("error")
                if isinstance(body, dict)
                else None
            )
            message = (
                f"OAuth token request failed (HTTP {resp.status_code}): "
                f"{detail or resp.text[:200]}"
            )
            if detail and "private key" in str(detail).lower():
                message += (
                    " — the farmOS server has not generated its OAuth2 key pair yet. "
                    "An admin must generate the keys in the Simple OAuth settings "
                    "(/admin/config/people/simple-oauth) or via drush on the server "
                    "host, then retry."
                )
            raise FarmOSAuthError(message)
        self._token = str(body["access_token"])
        self._refresh_token = str(body.get("refresh_token") or self._refresh_token or "")
        self._expires_at = time.monotonic() + float(body.get("expires_in") or 3600)
