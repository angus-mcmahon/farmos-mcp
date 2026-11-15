"""Taxonomy term tools: farmos_terms (vocabulary listing/autocomplete)."""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from ..client import FarmOSClient, Filter
from ..config import Config
from ..discovery import Discovery
from ..summarize import summarize_collection


def register(mcp: FastMCP, client: FarmOSClient, config: Callable[[], Config], discovery: Discovery) -> None:

    @mcp.tool()
    async def farmos_terms(
        type: str,
        name_contains: str | None = None,
        limit: int = 50,
    ) -> dict:
        """List taxonomy terms for a vocabulary, e.g. plant_type, animal_type, season, unit, log_category, material_type, equipment_type.

        Args:
            type: vocabulary/bundle name, e.g. 'plant_type' or 'season'.
            name_contains: case-insensitive substring match on the term name
                (autocomplete-friendly). Omit to list all terms.
            limit: max terms to return (server caps apply).
        """
        await discovery.ensure(client)
        discovery.validate_type(f"taxonomy_term--{type}")
        limit = max(1, min(limit, config().max_limit))
        filters = [Filter("name", name_contains, "CONTAINS")] if name_contains else None
        doc = await client.get_collection(
            "taxonomy_term",
            type,
            filters=filters,
            includes=["parent"],
            sort=["name"],
            limit=limit,
        )
        return summarize_collection(doc, offset=0)
