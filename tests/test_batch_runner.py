from __future__ import annotations

from pathlib import Path

from geoscan import batch_runner


def test_batch_runner_passes_optional_area_flag_to_each_program_config(tmp_path: Path) -> None:
    captured = []

    def fake_runner(config):
        captured.append(config)
        return {"conversion": {"ok": None, "status": "not_requested"}}

    report = batch_runner.run_batch(
        batch_runner.BatchConfig(
            project_root=tmp_path,
            source_rasters=(tmp_path / "t01_0001.tif",),
            include_areas=True,
        ),
        runner=fake_runner,
    )

    assert report["counts"]["completed"] == 1
    assert captured[0].include_areas is True


def test_batch_cli_accepts_optional_area_flag() -> None:
    args = batch_runner.build_arg_parser().parse_args(
        ["run", "--source-dir", "scans", "--include-areas"]
    )

    assert args.include_areas is True
