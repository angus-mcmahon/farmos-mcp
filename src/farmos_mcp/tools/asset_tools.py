"""Asset tools: farmos_assets (search) and the generic farmos_get."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from ..client import FarmOSClient, Filter
from ..config import Config
from ..discovery import Discovery
from ..errors import FarmOSError, FarmOSNotFoundError
from ..summarize import summarize_collection, summarize_single
from .helpers import fan_out_collections

_DEFAULT_INCLUDES = {
    "asset": ["parent"],
    "log": ["asset", "location", "category", "quantity"],
    "taxonomy_term": ["parent"],
    "quantity": ["units"],
}

# Max UUIDs accepted per farmos_get batch call (keeps one tool call's
# round-trip cost bounded). Enforced AFTER deduplication.
_GET_BATCH_MAX = 10


def register(mcp: FastMCP, client: FarmOSClient, config: Callable[[], Config], discovery: Discovery) -> None:

    @mcp.tool()
    async def farmos_assets(
        type: str | None = None,
        name_contains: str | None = None,
        status: str | None = None,
        is_location: bool | None = None,
        parent_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """Search farmOS assets: plants, animals, land, equipment, groups, structures, water, sensors...

        Args:
            type: asset bundle such as 'plant', 'animal', 'land', 'equipment',
                'group', 'structure', 'water'. Omit to search all bundles.
            name_contains: case-insensitive substring match on the asset name.
            status: 'active' (default state) or 'archived'.
            is_location: true to find assets usable as locations.
            parent_id: UUID of a parent asset to find its children/descendants.
            limit: page size (server caps apply). offset: page start.
        """
        await discovery.ensure(client)
        limit = max(1, min(limit, config().max_limit))
        filters = _asset_filters(name_contains, status, is_location, parent_id)
        if type:
            discovery.validate_type(f"asset--{type}")
            doc = await client.get_collection(
                "asset",
                type,
                filters=filters,
                includes=["parent"],
                limit=limit,
                offset=offset,
            )
            return summarize_collection(doc, offset=offset)
        return await fan_out_collections(
            client, discovery, "asset", filters=filters, includes=["parent"],
            limit=limit, offset=offset, server_max_limit=config().max_limit,
        )

    @mcp.tool()
    async def farmos_get(
        full_type: str, uuid: str | None = None, uuids: list[str] | None = None
    ) -> dict:
        """Fetch one or more farmOS resources by UUID, relationships resolved (asset, log, term, ...).

        Two modes:

        - Single (uuid=): returns a BARE record (no count/data wrapper),
          exactly like earlier versions of this tool.
        - Batch (uuids=[...]): 1-10 UUIDs of the SAME full_type, fetched
          concurrently; returns {"count": N, "data": [rows]} with rows in the
          requested order. Pass either uuid or uuids, never both.

        Batch behavior:
            - A missing record yields the row {"id": <uuid>, "error": "not
              found"} instead of failing the call; count includes those error
              rows. Any non-404 failure still fails the entire call.
            - Duplicates are removed (order preserved) BEFORE the size cap, so
              the cap error reports the deduplicated count. Max 10 uuids per
              batch; a batch of 1 stays batch-shaped.

        Args:
            full_type: resource type such as 'asset--plant', 'log--harvest',
                'taxonomy_term--plant_type' (see farmos_list_resources).
            uuid: single mode — the resource UUID (from farmos_assets /
                farmos_logs results).
            uuids: batch mode — list of 2-10 (deduped: 1-10) UUIDs, all of the
                same full_type.
        """
        await discovery.ensure(client)
        if uuid is not None and uuids:
            raise FarmOSError("pass either uuid (single) or uuids (batch), not both")
        if uuid is None and not uuids:
            raise FarmOSError("provide uuid (single) or uuids (batch of 2-10)")

        entity_type, bundle = discovery.validate_type(full_type)
        includes = _DEFAULT_INCLUDES.get(entity_type)

        if not uuids:
            doc = await client.get_resource(entity_type, bundle, uuid, includes=includes)
            return summarize_single(doc)

        # Batch mode: dedupe preserving order, cap-check after dedupe.
        seen: set[str] = set()
        ordered: list[str] = []
        for u in uuids:
            if u and u not in seen:
                seen.add(u)
                ordered.append(u)
        if not ordered:
            raise FarmOSError("provide uuid (single) or uuids (batch of 2-10)")
        if len(ordered) > _GET_BATCH_MAX:
            raise FarmOSError(f"max {_GET_BATCH_MAX} uuids per batch; got {len(ordered)}")

        async def _one(u: str) -> dict | None:
            try:
                doc = await client.get_resource(entity_type, bundle, u, includes=includes)
            except FarmOSNotFoundError:
                return None  # 404 -> per-record error row; other errors propagate
            return summarize_single(doc)

        results = await asyncio.gather(*(_one(u) for u in ordered))
        rows: list[dict] = [
            {"id": u, "error": "not found"} if res is None else res
            for u, res in zip(ordered, results)
        ]
        return {"count": len(rows), "data": rows}


def _asset_filters(
    name_contains: str | None,
    status: str | None,
    is_location: bool | None,
    parent_id: str | None,
) -> list[Filter]:
    filters: list[Filter] = []
    if name_contains:
        filters.append(Filter("name", name_contains, "CONTAINS"))
    if status:
        filters.append(Filter("status", status))
    if is_location is not None:
        filters.append(Filter("is_location", "1" if is_location else "0"))
    if parent_id:
        filters.append(Filter("parent.id", parent_id))
    return filters
