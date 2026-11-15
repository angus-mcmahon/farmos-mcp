"""FastMCP server assembly for farmOS."""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from .client import FarmOSClient
from .config import Config, LazyConfig
from .discovery import Discovery
from .tools import register_tools

INSTRUCTIONS = (
    "Tools for reading and writing a farmOS farm: assets (plants, animals, land, "
    "equipment), logs (events like seeding, harvest, input), and taxonomy terms. "
    "Start with farmos_info to learn the server, farmos_list_resources to see "
    "available resource types, then use farmos_assets / farmos_logs / "
    "farmos_terms to search. farmos_get fetches records with relationships "
    "resolved — by a single uuid, or a batch of 2-10 uuids of one type. "
    "farmos_summary aggregates logs within a time window server-side and "
    "returns compact counts (optionally quantity totals and week/day "
    "intervals) — prefer it over paging raw logs for month/year-scale "
    "questions. Write tools (farmos_create_asset, farmos_create_log, "
    "farmos_checkin_log, farmos_inventory_change, farmos_update_entity, "
    "farmos_archive, farmos_flag) mutate the farm — confirm intent before "
    "calling them. All identifiers are UUIDs. Responses are compacted; lists "
    "include next_offset for paging."
)


def create_server(config: Config | Callable[[], Config] | None = None) -> FastMCP:
    """Build the FastMCP server with all farmOS tools registered.

    Config resolution is deferred to the first tool call, so an unconfigured
    server still starts and reports a helpful error per call.
    """
    loader: Callable[[], Config]
    if config is None:
        loader = LazyConfig()
    elif callable(config):
        loader = config
    else:

        def loader() -> Config:
            return config

    client = FarmOSClient(loader)
    discovery = Discovery()
    mcp = FastMCP("farmos-mcp", instructions=INSTRUCTIONS)
    register_tools(mcp, client, loader, discovery)
    return mcp


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    create_server().run()


if __name__ == "__main__":
    main()
