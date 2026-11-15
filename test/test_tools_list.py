"""Smoke test: boots the MCP server over stdio and lists its tools.

Requires no live farmOS server — only that the config loads (dummy values)
and all tools register. Run with: .venv/bin/python test/test_tools_list.py
"""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {
    "farmos_info",
    "farmos_list_resources",
    "farmos_assets",
    "farmos_logs",
    "farmos_get",
    "farmos_terms",
    "farmos_summary",
    "farmos_create_asset",
    "farmos_create_log",
    "farmos_update_entity",
    "farmos_archive",
    "farmos_flag",
    "farmos_checkin_log",
    "farmos_inventory_change",
}


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "farmos_mcp"],
        env={
            **os.environ,
            "FARMOS_URL": "https://farm.example.invalid",
            "FARMOS_ACCESS_TOKEN": "smoke-test-token",
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}

            # Error path: an unreachable server must surface as a clean tool
            # error result, and the server must stay alive afterwards.
            result = await session.call_tool("farmos_list_resources", {})
            assert result.isError, "expected an error result for an unreachable server"
            assert "Error" in (result.content[0].text or ""), result.content
            ok = await session.call_tool("farmos_list_resources", {"entity": "log"})
            assert ok.isError, "server should still be responsive after an error"

    # Unconfigured launch: no FARMOS_* env vars and no discoverable .env.
    # The server must still start (tools/list works) and tool calls must
    # return a clean, actionable error instead of the process dying.
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("FARMOS_")}
    clean_env["FARMOS_MCP_ENV_FILE"] = "/tmp/farmos-mcp-test-missing.env"
    params_unconf = StdioServerParameters(
        command=sys.executable, args=["-m", "farmos_mcp"], env=clean_env, cwd="/tmp"
    )
    async with stdio_client(params_unconf) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            unconf_tools = await session.list_tools()
            assert EXPECTED <= {t.name for t in unconf_tools.tools}, (
                "tools must register even when unconfigured"
            )
            res = await session.call_tool("farmos_info", {})
            assert res.isError, "expected a config error for an unconfigured server"
            assert "FARMOS_URL" in (res.content[0].text or ""), res.content

    missing = EXPECTED - names
    extra = names - EXPECTED
    assert not missing, f"missing tools: {missing}"
    assert not extra, f"unexpected tools: {extra}"
    print(f"OK: {len(names)} tools registered: {sorted(names)}")
    print("OK: tool errors propagate as clean MCP error results")
    print(f"server: {init.serverInfo.name} v{init.serverInfo.version}")


if __name__ == "__main__":
    asyncio.run(main())
