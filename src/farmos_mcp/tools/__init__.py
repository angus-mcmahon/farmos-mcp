"""Tool registration for the farmOS MCP server.

One module per tool-cluster (see plan.md §3); each exposes a register()
that attaches its tools to the FastMCP instance.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from ..client import FarmOSClient
from ..config import Config
from ..discovery import Discovery

from . import asset_tools, discovery_tools, log_tools, summary_tools, term_tools, write_tools


def register_tools(
    mcp: FastMCP, client: FarmOSClient, config: Callable[[], Config], discovery: Discovery
) -> None:
    """Register all farmOS tools on the MCP server."""
    discovery_tools.register(mcp, client, config, discovery)
    asset_tools.register(mcp, client, config, discovery)
    log_tools.register(mcp, client, config, discovery)
    term_tools.register(mcp, client, config, discovery)
    summary_tools.register(mcp, client, config, discovery)
    write_tools.register(mcp, client, config, discovery)
