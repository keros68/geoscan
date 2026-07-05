"""cli conversion needs the MapGIS dongle service (dog67.exe). The pre-flight
fails fast BEFORE any vectorization so a missing dongle no longer wastes the
whole pipeline only to fail at the final ~300s SECTION/W60 timeout.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from geoscan import batch_runner, production_program
from geoscan.env_probe import (
    DONGLE_PROCESS_NAME,
    dongle_status,
)
from geoscan.production_program import (
    DonglePrecheckError,
    ProgramConfig,
    run_production_program,
)


def test_dongle_status_shape() -> None:
    status = dongle_status()
    assert status["process"] == DONGLE_PROCESS_NAME
    assert isinstance(status["running"], bool)
    assert isinstance(status["checked"], bool)


def test_cli_run_fails_fast_without_dongle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(production_program, "dongle_process_running", lambda: False)
    with pytest.raises(DonglePrecheckError) as excinfo:
        run_production_program(
            ProgramConfig(
                project_root=tmp_path,
                source_raster=tmp_path / "missing.tif",
                map_id="T01_9990",
                conversion_mode="cli",
            )
        )
    assert DONGLE_PROCESS_NAME in str(excinfo.value)
    # Fail fast: nothing was created before the guard.
    assert not (tmp_path / "T01_9990_P").exists()


def test_non_cli_modes_skip_dongle_check(tmp_path: Path, monkeypatch) -> None:
    """none/prepare never launch MapGIS, so they must not be blocked by the dongle."""
    monkeypatch.setattr(production_program, "dongle_process_running", lambda: False)
    for mode in ("none", "prepare"):
        with pytest.raises(Exception) as excinfo:  # missing raster fails later, NOT at the dongle
            run_production_program(
                ProgramConfig(
                    project_root=tmp_path,
                    source_raster=tmp_path / "missing.tif",
                    map_id="T01_9991",
                    conversion_mode=mode,
                )
            )
        assert not isinstance(excinfo.value, DonglePrecheckError)


def test_skip_flag_bypasses_dongle_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(production_program, "dongle_process_running", lambda: False)
    with pytest.raises(Exception) as excinfo:  # gets past the gate, fails later on missing raster
        run_production_program(
            ProgramConfig(
                project_root=tmp_path,
                source_raster=tmp_path / "missing.tif",
                map_id="T01_9992",
                conversion_mode="cli",
                skip_dongle_check=True,
            )
        )
    assert not isinstance(excinfo.value, DonglePrecheckError)


def test_batch_aborts_up_front_without_dongle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(batch_runner, "dongle_process_running", lambda: False)
    with pytest.raises(DonglePrecheckError):
        batch_runner.run_batch(
            batch_runner.BatchConfig(
                project_root=tmp_path,
                source_rasters=(tmp_path / "t01_0001.tif",),
                conversion_mode="cli",
            )
        )


def test_batch_skip_flag_bypasses_gate(tmp_path: Path, monkeypatch) -> None:
    """With the escape hatch, a missing dongle does not abort the batch at t=0."""
    monkeypatch.setattr(batch_runner, "dongle_process_running", lambda: False)
    report = batch_runner.run_batch(
        batch_runner.BatchConfig(
            project_root=tmp_path,
            source_rasters=(),  # empty queue -> returns cleanly, proves the gate was passed
            conversion_mode="cli",
            skip_dongle_check=True,
        )
    )
    assert report["counts"]["failed"] == 0


def test_cli_defaults_do_not_skip_dongle() -> None:
    assert ProgramConfig(
        project_root=Path("."), source_raster=Path("x.tif"), map_id="T01_0001"
    ).skip_dongle_check is False
    assert batch_runner.BatchConfig(
        project_root=Path("."), source_rasters=()
    ).skip_dongle_check is False
    assert production_program.build_arg_parser().parse_args(
        ["run", "--source-raster", "x.jpg", "--map-id", "T01_0001", "--skip-dongle-check"]
    ).skip_dongle_check is True
    assert batch_runner.build_arg_parser().parse_args(
        ["run", "--source-dir", "scans", "--skip-dongle-check"]
    ).skip_dongle_check is True
