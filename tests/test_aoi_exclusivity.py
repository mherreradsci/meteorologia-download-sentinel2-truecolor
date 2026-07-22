import pytest

from download_sentinel2_truecolor import download_latest_true_color_images

DUMMY_CREDS = {"client_id": "fake", "client_secret": "fake"}


def test_bbox_and_geojson_together_raises_value_error():
    with pytest.raises(ValueError):
        download_latest_true_color_images(
            n=1,
            bbox=[-71.0, -30.0, -70.0, -29.0],
            geojson="aoi/whatever.geojson",
            **DUMMY_CREDS,
        )


def test_bbox_and_region_point_together_raises_value_error():
    with pytest.raises(ValueError):
        download_latest_true_color_images(
            n=1,
            bbox=[-71.0, -30.0, -70.0, -29.0],
            region="Coquimbo",
            point="Tongoy",
            **DUMMY_CREDS,
        )


def test_no_aoi_source_raises_value_error():
    with pytest.raises(ValueError):
        download_latest_true_color_images(n=1, **DUMMY_CREDS)


def test_region_without_point_raises_value_error():
    with pytest.raises(ValueError):
        download_latest_true_color_images(n=1, region="Coquimbo", **DUMMY_CREDS)
