#!/usr/bin/env python3
"""Probe the OFFICIAL Flagsmith MCP server (Flagsmith/flagsmith repo, mcp/ subdir) over stdio
against the local self-hosted API. Run with the MCP venv python:
    $FLAGSMITH_RUNTIME/mcpvenv/bin/python scripts/mcp-probe.py
Reads FS_ADMIN_API_KEY from $FLAGSMITH_RUNTIME/local.env; never prints it."""
import asyncio, json, os
from pathlib import Path
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

RUNTIME = Path(os.environ.get("FLAGSMITH_RUNTIME", "../flagsmith-runtime"))
env = dict(l.split("=", 1) for l in (RUNTIME / "local.env").read_text().splitlines() if "=" in l)
state = json.loads((RUNTIME / "seed-state.json").read_text())

transport = StdioTransport(
    command=str(RUNTIME / "mcpvenv/bin/flagsmith-mcp"), args=[],
    env={"PATH": os.environ["PATH"], "TRANSPORT": "stdio",
         "FLAGSMITH_API_URL": os.environ.get("FLAGSMITH_API_URL", "http://127.0.0.1:8600"),
         "FLAGSMITH_API_TOKEN": env["FS_ADMIN_API_KEY"], "LOG_LEVEL": "WARNING"})


def short(res, n=300):
    txt = res.content[0].text if res.content else str(res.data)
    return txt[:n].replace(env["FS_ENV_KEY"], "<env-key>")


async def main():
    async with Client(transport) as c:
        tools = await c.list_tools()
        print("tools:", len(tools))
        print("sample:", sorted(t.name for t in tools)[:8])
        pid = state["project_id"]
        r = await c.call_tool("list_organizations", {})
        print("list_organizations ->", short(r, 200))
        r = await c.call_tool("list_project_features", {"project_pk": pid})
        names = [f["name"] for f in json.loads(r.content[0].text)["results"]]
        print("list_project_features ->", names)
        r = await c.call_tool("list_project_segments", {"project_pk": pid})
        print("list_project_segments ->", [s["name"] for s in json.loads(r.content[0].text)["results"]])
        if "mcp_probe_flag" not in names:
            r = await c.call_tool("create_feature", {"project_pk": pid, "name": "mcp_probe_flag",
                                                     "type": "STANDARD", "default_enabled": False,
                                                     "description": "created through official MCP (stdio)"})
            print("create_feature (WRITE) ->", short(r, 160))
        else:
            print("create_feature skipped (exists)")


asyncio.run(main())
