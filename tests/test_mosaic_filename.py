import re

from download_sentinel2_truecolor import build_mosaic_filename


def test_filename_shape():
    filename = build_mosaic_filename("Coquimbo", "20260729", 8)
    assert re.match(r"^Coquimbo_mosaic_20260729_8tiles_[0-9a-f]{8}\.png$", filename)


def test_date_range_is_preserved_verbatim():
    filename = build_mosaic_filename("Tongoy", "20260701_a_20260705", 3)
    assert "20260701_a_20260705" in filename


def test_falls_back_to_aoi_when_label_is_empty():
    assert build_mosaic_filename("", "20260729", 1).startswith("aoi_mosaic_")


def test_hash_differs_when_date_range_differs():
    f1 = build_mosaic_filename("Tongoy", "20260701", 3)
    f2 = build_mosaic_filename("Tongoy", "20260702", 3)
    assert f1 != f2


def test_hash_differs_when_tile_count_differs():
    f1 = build_mosaic_filename("Tongoy", "20260701", 3)
    f2 = build_mosaic_filename("Tongoy", "20260701", 4)
    assert f1 != f2


def test_is_deterministic():
    assert build_mosaic_filename("Tongoy", "20260701", 3) == build_mosaic_filename("Tongoy", "20260701", 3)
