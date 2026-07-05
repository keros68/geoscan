from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from geoscan.env_probe import (
    PROJECT_ROOT,
    build_environment_report,
    write_report,
)
from geoscan.mapgis67_bridge import (
    DEFAULT_SECTION_BOOTSTRAP_FILE,
    DEFAULT_SECTION_EXE,
    DEFAULT_W60_CONV_EXE,
    collect_ready,
    prepare_batch,
    run_dxf_to_wl_wt_pipeline,
    section_batch_convert,
    verify_batch,
    w60_batch_convert,
)


mcp = FastMCP(
    "mapgis67-bridge",
    instructions=(
        "MapGIS 6.7 bridge tools for preparing grouped DXF batches, "
        "verifying SECTION outputs, collecting WL/WT files, and running "
        "the current DXF-to-WL/WT pipeline. These tools do not claim a pure "
        "GUI-free MapGIS route; SECTION output must still be verified."
    ),
)


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _optional_path(value: str | None, default: Path) -> Path:
    return _path(value) if value else default


@mcp.tool(
    name="probe_environment",
    description="Probe local MapGIS67/SECTION executables, bootstrap files, Python dependencies, and agent capabilities.",
)
def probe_environment_tool(project_root: str | None = None, write_json: str | None = None) -> dict[str, Any]:
    root = _path(project_root) if project_root else PROJECT_ROOT
    report = build_environment_report(project_root=root)
    if write_json:
        write_report(_path(write_json), report)
    return report


@mcp.tool(
    name="prepare_batch",
    description="Prepare a short-path SECTION DXF batch folder from grouped exchange DXF sources.",
)
def prepare_batch_tool(source_dir: str, batch_dir: str) -> dict[str, Any]:
    return prepare_batch(source_dir=_path(source_dir), batch_dir=_path(batch_dir))


@mcp.tool(
    name="verify_batch",
    description="Verify that a prepared SECTION batch has produced required WL/WT outputs.",
)
def verify_batch_tool(batch_dir: str) -> dict[str, Any]:
    return verify_batch(_path(batch_dir))


@mcp.tool(
    name="collect_ready",
    description="Collect verified SECTION outputs into a flat MAPGIS_READY folder.",
)
def collect_ready_tool(
    conversion_list: str,
    section_batch_dir: str,
    ready_dir: str,
    layer_output_to_target: dict[str, str] | None = None,
) -> dict[str, Any]:
    return collect_ready(
        conversion_list=_path(conversion_list),
        section_batch_dir=_path(section_batch_dir),
        ready_dir=_path(ready_dir),
        layer_output_to_target=layer_output_to_target,
    )


@mcp.tool(
    name="section_batch_convert",
    description=(
        "Run the Win32 SECTION batch-DXF conversion automation and verify outputs. "
        "Use dry_run=true to validate inputs without launching SECTION."
    ),
)
def section_batch_convert_tool(
    batch_dir: str,
    section_exe: str | None = None,
    bootstrap_file: str | None = None,
    dry_run: bool = False,
    timeout: int = 300,
) -> dict[str, Any]:
    return section_batch_convert(
        batch_dir=_path(batch_dir),
        section_exe=_optional_path(section_exe, DEFAULT_SECTION_EXE),
        bootstrap_file=_optional_path(bootstrap_file, DEFAULT_SECTION_BOOTSTRAP_FILE),
        dry_run=dry_run,
        wait_timeout_seconds=timeout,
    )


@mcp.tool(
    name="w60_batch_convert",
    description=(
        "Run the Win32 W60_Conv batch-DXF conversion automation and verify outputs. "
        "Use dry_run=true to validate inputs without launching W60_Conv."
    ),
)
def w60_batch_convert_tool(
    batch_dir: str,
    w60_conv_exe: str | None = None,
    dry_run: bool = False,
    timeout: int = 300,
) -> dict[str, Any]:
    return w60_batch_convert(
        batch_dir=_path(batch_dir),
        w60_conv_exe=_optional_path(w60_conv_exe, DEFAULT_W60_CONV_EXE),
        dry_run=dry_run,
        wait_timeout_seconds=timeout,
    )


@mcp.tool(
    name="run_dxf_to_wl_wt_pipeline",
    description="Run the current prepare/convert/verify/collect pipeline for grouped DXF to WL/WT outputs.",
)
def run_dxf_to_wl_wt_pipeline_tool(
    source_dir: str,
    batch_dir: str,
    conversion_list: str,
    ready_dir: str,
    reuse_batch: bool = False,
    skip_section_convert: bool = False,
    conversion_backend: str = "auto",
    section_exe: str | None = None,
    w60_conv_exe: str | None = None,
    bootstrap_file: str | None = None,
    layer_output_to_target: dict[str, str] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    return run_dxf_to_wl_wt_pipeline(
        source_dir=_path(source_dir),
        batch_dir=_path(batch_dir),
        conversion_list=_path(conversion_list),
        ready_dir=_path(ready_dir),
        reuse_batch=reuse_batch,
        skip_section_convert=skip_section_convert,
        conversion_backend=conversion_backend,
        section_exe=_optional_path(section_exe, DEFAULT_SECTION_EXE),
        w60_conv_exe=_optional_path(w60_conv_exe, DEFAULT_W60_CONV_EXE),
        bootstrap_file=_optional_path(bootstrap_file, DEFAULT_SECTION_BOOTSTRAP_FILE),
        layer_output_to_target=layer_output_to_target,
        wait_timeout_seconds=timeout,
    )


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
