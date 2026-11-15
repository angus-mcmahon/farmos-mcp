"""Log tools: farmos_logs (search across log bundles)."""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from ..client import FarmOSClient, Filter
from ..config import Config
from ..discovery import Discovery
from ..summarize import summarize_collection
from .helpers import fan_out_collections, to_epoch

_LOG_INCLUDES = ["asset", "location", "category", "quantity"]


def register(mcp: FastMCP, client: FarmOSClient, config: Callable[[], Config], discovery: Discovery) -> None:

    @mcp.tool()
    async def farmos_logs(
        type: str | None = None,
        asset_id: str | None = None,
        location_id: str | None = None,
        status: str | None = None,
        timestamp_after: str | None = None,
        timestamp_before: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """Search farmOS logs: activity, observation, seeding, transplanting, harvest, input, maintenance, medical, birth, lab_test...

        Args:
            type: log bundle such as 'activity', 'seeding', 'harvest'. Omit for all bundles.
            asset_id: UUID of an asset the log references.
            location_id: UUID of a location asset the log is assigned to.
            status: 'done' or 'pending'.
            timestamp_after / timestamp_before: ISO-8601 timestamps, e.g.
                '2024-05-01T00:00:00Z' or a bare date '2024-05-01' (UTC assumed).
            limit: page size (server caps apply). offset: page start.
        """
        await discovery.ensure(client)
        limit = max(1, min(limit, config().max_limit))
        filters = _log_filters(asset_id, location_id, status, timestamp_after, timestamp_before)
        if type:
            discovery.validate_type(f"log--{type}")
            doc = await client.get_collection(
                "log",
                type,
                filters=filters,
                includes=_LOG_INCLUDES,
                sort=["-timestamp"],
                limit=limit,
                offset=offset,
            )
            return summarize_collection(doc, offset=offset)
        return await fan_out_collections(
            client, discovery, "log", filters=filters, includes=_LOG_INCLUDES,
            sort="-timestamp", limit=limit, offset=offset,
            server_max_limit=config().max_limit,
        )


def _log_filters(
    asset_id: str | None,
    location_id: str | None,
    status: str | None,
    timestamp_after: str | None,
    timestamp_before: str | None,
) -> list[Filter]:
    filters: list[Filter] = []
    if asset_id:
        filters.append(Filter("asset.id", asset_id))
    if location_id:
        filters.append(Filter("location.id", location_id))
    if status:
        filters.append(Filter("status", status))
    if timestamp_after:
        filters.append(Filter("timestamp", to_epoch(timestamp_after), ">"))
    if timestamp_before:
        filters.append(Filter("timestamp", to_epoch(timestamp_before), "<"))
    return filters
