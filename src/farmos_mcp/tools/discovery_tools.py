"""Server discovery tools: farmos_info and farmos_list_resources."""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from ..client import FarmOSClient
from ..config import Config
from ..discovery import Discovery


def register(mcp: FastMCP, client: FarmOSClient, config: Callable[[], Config], discovery: Discovery) -> None:

    @mcp.tool()
    async def farmos_info() -> dict:
        """farmOS server metadata: farm name, farmOS version, system of measurement, and the authenticated user.

        Use this first as a connectivity check and to learn what server you are on.
        """
        await discovery.ensure(client)
        info: dict = {
            "farm_name": discovery.farm_name,
            "farmos_version": discovery.version,
            "system_of_measurement": discovery.system_of_measurement,
        }
        if discovery.me_href:
            try:
                path = urllib.parse.urlsplit(discovery.me_href).path
                me = await client.request("GET", path)
                attrs = (me.get("data") or {}).get("attributes") or {}
                info["authenticated_user"] = attrs.get("display_name") or attrs.get("name")
            except Exception:  # best effort; user endpoint may be restricted
                info["authenticated_user"] = None
        return info

    @mcp.tool()
    async def farmos_list_resources(entity: str | None = None) -> dict:
        """List the JSON:API resource types this farmOS server exposes (e.g. 'asset--plant', 'log--harvest').

        Args:
            entity: optional prefix filter such as 'asset', 'log', 'taxonomy_term', 'user'.
        """
        await discovery.ensure(client)
        types = sorted(discovery.types)
        if entity:
            prefix = f"{entity.strip().rstrip('-')}--"
            types = [t for t in types if t.startswith(prefix)]
        return {"count": len(types), "types": types}
