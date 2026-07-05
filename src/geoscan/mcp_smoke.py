from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


EXPECTED_MCP_TOOLS = {
    "probe_environment",
    "prepare_batch",
    "verify_batch",
    "collect_ready",
    "section_batch_convert",
    "run_dxf_to_wl_wt_pipeline",
}


def build_server_parameters(project_root: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command="python",
        args=["-m", "geoscan.mapgis67_mcp_server"],
        env={"PYTHONPATH": str(project_root)},
    )


async def list_mcp_tools(project_root: Path) -> list[str]:
    params = build_server_parameters(project_root)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return sorted(tool.name for tool in result.tools)


def smoke_check(project_root: Path) -> dict[str, object]:
    tools = asyncio.run(list_mcp_tools(project_root))
    found = set(tools)
    return {
        "ok": EXPECTED_MCP_TOOLS.issubset(found),
        "tools": tools,
        "missing": sorted(EXPECTED_MCP_TOOLS - found),
        "unexpected": sorted(found - EXPECTED_MCP_TOOLS),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the MapGIS67 MCP server.")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    report = smoke_check(args.project_root)
    print("ok:", report["ok"])
    print("tools:", ", ".join(report["tools"]))
    if report["missing"]:
        print("missing:", ", ".join(report["missing"]))
    if report["unexpected"]:
        print("unexpected:", ", ".join(report["unexpected"]))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
