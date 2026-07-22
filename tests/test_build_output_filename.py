import re

from download_sentinel2_truecolor import build_output_filename


def make_item(item_id="S2B_MSIL2A_TEST", datetime_str="2026-07-16T14:52:04Z"):
    return {"properties": {"datetime": datetime_str}, "id": item_id}


def test_preserves_accented_and_non_ascii_letters():
    assert build_output_filename("Ñuble", make_item()).startswith("Ñuble_")


def test_preserves_apostrophes_and_spaces():
    assert build_output_filename("O'Higgins", make_item()).startswith("O'Higgins_")
    assert build_output_filename("La Serena", make_item()).startswith("La Serena_")


def test_replaces_single_nonprintable_character():
    filename = build_output_filename("foo\tbar", make_item())
    assert filename.startswith("foo_bar_")
    assert "\t" not in filename


def test_collapses_consecutive_nonprintable_characters_into_one_underscore():
    filename = build_output_filename("foo\t\n\rbar", make_item())
    assert filename.startswith("foo_bar_")


def test_strips_leading_and_trailing_underscores():
    assert build_output_filename("\tTongoy\n", make_item()).startswith("Tongoy_")


def test_falls_back_to_aoi_when_label_is_all_nonprintable():
    assert build_output_filename("\t\n\r", make_item()).startswith("aoi_")


def test_falls_back_to_aoi_when_label_is_empty():
    assert build_output_filename("", make_item()).startswith("aoi_")


def test_filename_includes_timestamp_without_separators():
    item = make_item(datetime_str="2026-07-16T14:52:04Z")
    assert "20260716T145204" in build_output_filename("Tongoy", item)


def test_filename_shape_matches_slug_timestamp_hash_png():
    item = make_item(item_id="S2B_MSIL2A_ABCDEF")
    filename = build_output_filename("Tongoy", item)
    assert re.match(r"^Tongoy_20260716T145204_[0-9a-f]{8}\.png$", filename)


def test_hash_suffix_differs_for_different_item_ids():
    f1 = build_output_filename("Tongoy", make_item(item_id="S2B_MSIL2A_AAAA"))
    f2 = build_output_filename("Tongoy", make_item(item_id="S2B_MSIL2A_BBBB"))
    assert f1 != f2


def test_filename_is_deterministic_for_same_inputs():
    item = make_item()
    assert build_output_filename("Tongoy", item) == build_output_filename("Tongoy", item)
