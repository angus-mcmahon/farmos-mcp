"""Async JSON:API client for farmOS 3.x.

This layer is the only thing that knows JSON:API details; tools speak a small
domain language (entity type, bundle, filters, includes).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .auth import OAuthTokenManager
from .config import Config
from .errors import FarmOSAPIError, FarmOSNotFoundError

JSONAPI_HEADERS = {"Accept": "application/vnd.api+json"}


@dataclass(frozen=True)
class Filter:
    """One JSON:API filter condition.

    operator defaults to "="; Drupal JSON:API also accepts CONTAINS,
    STARTS_WITH, ENDS_WITH, <, >, <=, >=, IN, IS_NULL, ... Paths may traverse
    relationships, e.g. "asset.id" or "locations.name".
    """

    path: str
    value: str
    operator: str = "="


class FarmOSClient:
    """Thin async wrapper over farmOS's JSON:API endpoints."""

    def __init__(self, config: Callable[[], Config]) -> None:
        self._config_loader = config
        self._config: Config | None = None
        self._auth: OAuthTokenManager | None = None
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._http is None:
            config = self._config_loader()  # raises an actionable error on first use if misconfigured
            self._config = config
            self._auth = OAuthTokenManager(config)
            self._http = httpx.AsyncClient(
                base_url=config.base_url,
                timeout=config.timeout_seconds,
                headers=JSONAPI_HEADERS,
                follow_redirects=True,
            )

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- low level -----------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """Send one request, retrying once with a fresh token on 401."""
        await self.start()
        assert self._http is not None
        return await self._send(self._http, method, path, params, json_body)

    async def _send(
        self,
        http: httpx.AsyncClient,
        method: str,
        path: str,
        params: dict[str, str] | None,
        json_body: dict | None,
    ) -> dict:
        token = await self._auth.get_token(http)
        headers = {"Authorization": f"Bearer {token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/vnd.api+json"
        resp = await http.request(method, path, params=params, json=json_body, headers=headers)
        if resp.status_code == 401:
            # Token may have been revoked/expired server-side; refresh once and retry.
            self._auth.invalidate()
            token = await self._auth.get_token(http)
            headers["Authorization"] = f"Bearer {token}"
            resp = await http.request(method, path, params=params, json=json_body, headers=headers)

        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            error = FarmOSAPIError.from_response(resp.status_code, body, path=path)
            if resp.status_code == 404:
                raise FarmOSNotFoundError(error.status_code, str(error), error.errors)
            raise error
        return body

    # -- reads -----------------------------------------------------------------

    async def get_collection(
        self,
        entity_type: str,
        bundle: str,
        *,
        filters: list[Filter] | None = None,
        includes: list[str] | None = None,
        sort: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        fields: dict[str, str] | None = None,
    ) -> dict:
        """GET /api/{entity_type}/{bundle} with JSON:API query params.

        `fields` maps a full resource type to a sparse fieldset (e.g.
        {"log--activity": "timestamp,status"}), keeping internal aggregation
        fetches small.
        """
        params: dict[str, str] = {"page[limit]": str(limit), "page[offset]": str(offset)}
        for i, f in enumerate(filters or []):
            base = f"filter[{i}][condition]"
            params[f"{base}[path]"] = f.path
            params[f"{base}[value]"] = f.value
            if f.operator != "=":
                params[f"{base}[operator]"] = f.operator
        if includes:
            params["include"] = ",".join(includes)
        if sort:
            params["sort"] = ",".join(sort)
        for type_key, field_list in (fields or {}).items():
            params[f"fields[{type_key}]"] = field_list
        return await self.request("GET", f"/api/{entity_type}/{bundle}", params=params)

    async def get_resource(
        self, entity_type: str, bundle: str, uuid: str, *, includes: list[str] | None = None
    ) -> dict:
        """GET one resource by UUID."""
        params: dict[str, str] = {}
        if includes:
            params["include"] = ",".join(includes)
        return await self.request("GET", f"/api/{entity_type}/{bundle}/{uuid}", params=params)

    # -- writes (used from M2 on) ------------------------------------------------

    async def create_resource(
        self,
        entity_type: str,
        bundle: str,
        attributes: dict,
        relationships: dict | None = None,
    ) -> dict:
        """POST a new resource; returns the created resource document."""
        data: dict[str, Any] = {"type": f"{entity_type}--{bundle}", "attributes": attributes}
        if relationships:
            data["relationships"] = relationships
        return await self.request("POST", f"/api/{entity_type}/{bundle}", json_body={"data": data})

    async def patch_resource(
        self, entity_type: str, bundle: str, uuid: str, attributes: dict
    ) -> dict:
        """PATCH selected attributes of one resource."""
        data = {"type": f"{entity_type}--{bundle}", "id": uuid, "attributes": attributes}
        return await self.request(
            "PATCH", f"/api/{entity_type}/{bundle}/{uuid}", json_body={"data": data}
        )

    async def delete_resource(self, entity_type: str, bundle: str, uuid: str) -> dict:
        """DELETE one resource (204 on success; body is empty)."""
        return await self.request("DELETE", f"/api/{entity_type}/{bundle}/{uuid}")
