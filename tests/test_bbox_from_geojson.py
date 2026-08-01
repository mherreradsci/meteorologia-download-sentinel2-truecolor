import json
from pathlib import Path

import pytest

from download_sentinel2_truecolor import GeoJSONError, bbox_from_geojson

SAMPLE_GEOJSON = str(
    Path(__file__).parent.parent
    / "aoi"
    / "04-Coquimbo"
    / "Chile-Region_de_Coquimbo-La_huiguera-Chungungo.geojson"
)

POLYGON_A = {
    "type": "Polygon",
    "coordinates": [[[-71.0, -30.0], [-71.0, -29.0], [-70.0, -29.0], [-70.0, -30.0], [-71.0, -30.0]]],
}
POLYGON_B = {
    "type": "Polygon",
    "coordinates": [[[-69.0, -28.0], [-69.0, -27.0], [-68.0, -27.0], [-68.0, -28.0], [-69.0, -28.0]]],
}


def test_real_sample_file_bbox():
    bbox = bbox_from_geojson(SAMPLE_GEOJSON)
    assert bbox == pytest.approx([-71.316928, -29.454758, -71.269867, -29.438235])


def test_bare_geometry_no_feature_wrapper(tmp_path):
    path = tmp_path / "bare.geojson"
    path.write_text(json.dumps(POLYGON_A))
    assert bbox_from_geojson(str(path)) == pytest.approx([-71.0, -30.0, -70.0, -29.0])


def test_single_feature(tmp_path):
    path = tmp_path / "feature.geojson"
    path.write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": POLYGON_A}))
    assert bbox_from_geojson(str(path)) == pytest.approx([-71.0, -30.0, -70.0, -29.0])


def test_feature_collection_union_of_two_features(tmp_path):
    path = tmp_path / "fc.geojson"
    path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": POLYGON_A},
            {"type": "Feature", "properties": {}, "geometry": POLYGON_B},
        ],
    }))
    assert bbox_from_geojson(str(path)) == pytest.approx([-71.0, -30.0, -68.0, -27.0])


def test_missing_file_raises_geojson_error():
    with pytest.raises(GeoJSONError):
        bbox_from_geojson("does_not_exist.geojson")


def test_invalid_json_raises_geojson_error(tmp_path):
    path = tmp_path / "broken.geojson"
    path.write_text("{not valid json")
    with pytest.raises(GeoJSONError):
        bbox_from_geojson(str(path))


def test_empty_feature_collection_raises_geojson_error(tmp_path):
    path = tmp_path / "empty.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    with pytest.raises(GeoJSONError):
        bbox_from_geojson(str(path))
