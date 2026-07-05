"""Map id must be derivable from ANY input filename, not just t01_0007.

Colleague bug 2026-07-04: a raster renamed to a plain number (or any non-
convention name) left the GUI Map ID field empty, so the run produced no
output. Derivation now falls back to a sanitized stem.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from geoscan.batch_runner import map_id_from_raster
from geoscan.production_program import (
    default_line_target_file,
    default_text_target_file,
    derive_map_id_from_filename,
    sanitize_map_id,
    short_output_root_for_map_id,
)


def test_standard_convention_still_wins():
    assert derive_map_id_from_filename("t01_0007.tif") == "T01_0007"
    assert derive_map_id_from_filename("T01_0128.JPG") == "T01_0128"
    # Embedded in a longer name.
    assert derive_map_id_from_filename("scan_t01_0007_final.tif") == "T01_0007"


def test_numeric_and_freeform_names_fall_back_to_sanitized_stem():
    assert derive_map_id_from_filename("12345.tif") == "12345"
    assert derive_map_id_from_filename("00123.png") == "00123"
    assert derive_map_id_from_filename("custom.png") == "CUSTOM"
    assert derive_map_id_from_filename("map a.tif") == "MAP_A"
    assert derive_map_id_from_filename("2025-01-01 scan.tiff") == "2025_01_01_SCAN"


def test_sanitize_keeps_cjk_and_collapses_separators():
    assert sanitize_map_id("嫩北矿区 3") == "嫩北矿区_3"
    assert sanitize_map_id("a...b---c") == "A_B_C"
    assert sanitize_map_id("__t99__") == "T99"
    assert sanitize_map_id("") == ""
    assert sanitize_map_id("@@@") == ""


def test_derived_ids_produce_valid_output_folder_and_targets():
    # Pure-number id flows through the folder + default target-file helpers.
    map_id = derive_map_id_from_filename("12345.tif")
    root = short_output_root_for_map_id(Path("C:/maps"), map_id)
    assert root == Path("C:/maps/12345_P")
    assert default_text_target_file(map_id) == "T45TXT.WT"
    assert default_line_target_file(map_id) == "T45LINE.WL"


def test_batch_uses_same_derivation_and_rejects_only_empty(tmp_path):
    assert map_id_from_raster(Path("999.tif")) == "999"
    assert map_id_from_raster(Path("嫩北.tif")) == "嫩北"
    # A stem with no usable character is a real error, not silently ""-> broken folder.
    with pytest.raises(ValueError, match="no usable map id"):
        map_id_from_raster(Path("@@@.tif"))
