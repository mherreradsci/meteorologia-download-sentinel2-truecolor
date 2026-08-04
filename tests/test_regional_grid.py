import pytest

from download_sentinel2_truecolor import compute_image_dimensions, compute_regional_grid

# Bbox real de "Región de Coquimbo, Chile" (192 x 359 km aprox), usado porque
# es el caso que motivó esta función: una sola imagen sobre esta área queda
# mayormente en negro fuera del swath de la adquisición elegida.
COQUIMBO_BBOX = [-71.814915, -32.282101, -69.809744, -29.037888]

# Bbox chico (unos 20x20 km), pensado para caer en un solo tile.
SMALL_BBOX = [-71.3, -30.05, -71.1, -29.85]


def _coverage_counts(tiles, canvas_w, canvas_h):
    """Grilla de enteros del tamaño del canvas: cada celda cuenta cuántos
    tiles la cubren. Sin gaps ni overlaps, todos los valores deben ser 1."""
    grid = [[0] * canvas_w for _ in range(canvas_h)]
    for t in tiles:
        for y in range(t.y0, t.y1):
            row = grid[y]
            for x in range(t.x0, t.x1):
                row[x] += 1
    return grid


def test_large_bbox_splits_into_multiple_tiles():
    tiles, canvas_w, canvas_h = compute_regional_grid(
        COQUIMBO_BBOX, tile_km=100.0, resolution_m=10.0, max_image_dim=2000
    )
    assert len(tiles) > 1


def test_small_bbox_fits_in_a_single_tile():
    tiles, canvas_w, canvas_h = compute_regional_grid(
        SMALL_BBOX, tile_km=100.0, resolution_m=10.0, max_image_dim=2000
    )
    assert len(tiles) == 1
    assert tiles[0].x0 == 0 and tiles[0].y0 == 0
    assert tiles[0].x1 == canvas_w and tiles[0].y1 == canvas_h


def test_canvas_size_matches_single_image_mode():
    # El canvas del mosaico debe ser exactamente el mismo tamaño que pediría
    # compute_image_dimensions para el bbox completo -- mismo resolution_m y
    # max_image_dim, mismo resultado final, solo que ensamblado en tiles.
    tiles, canvas_w, canvas_h = compute_regional_grid(
        COQUIMBO_BBOX, tile_km=100.0, resolution_m=10.0, max_image_dim=2000
    )
    expected_w, expected_h = compute_image_dimensions(COQUIMBO_BBOX, 10.0, 2000)
    assert (canvas_w, canvas_h) == (expected_w, expected_h)


def test_tiles_cover_canvas_exactly_no_gaps_no_overlaps():
    tiles, canvas_w, canvas_h = compute_regional_grid(
        COQUIMBO_BBOX, tile_km=100.0, resolution_m=10.0, max_image_dim=500
    )
    grid = _coverage_counts(tiles, canvas_w, canvas_h)
    counts = {cell for row in grid for cell in row}
    assert counts == {1}


def test_tile_bboxes_are_disjoint_and_span_the_full_extent():
    tiles, _, _ = compute_regional_grid(COQUIMBO_BBOX, tile_km=100.0, resolution_m=10.0, max_image_dim=2000)
    min_lon = min(t.bbox[0] for t in tiles)
    min_lat = min(t.bbox[1] for t in tiles)
    max_lon = max(t.bbox[2] for t in tiles)
    max_lat = max(t.bbox[3] for t in tiles)
    assert min_lon == pytest.approx(COQUIMBO_BBOX[0])
    assert min_lat == pytest.approx(COQUIMBO_BBOX[1])
    assert max_lon == pytest.approx(COQUIMBO_BBOX[2])
    assert max_lat == pytest.approx(COQUIMBO_BBOX[3])


def test_non_positive_tile_km_raises_value_error():
    with pytest.raises(ValueError):
        compute_regional_grid(COQUIMBO_BBOX, tile_km=0, resolution_m=10.0, max_image_dim=2000)
    with pytest.raises(ValueError):
        compute_regional_grid(COQUIMBO_BBOX, tile_km=-5, resolution_m=10.0, max_image_dim=2000)


def test_tile_exceeding_hard_cap_raises_value_error():
    # tile_km grande + resolución fina + max_image_dim alto => cada tile
    # individual pediría más píxeles que el límite duro de la Process API.
    with pytest.raises(ValueError):
        compute_regional_grid(
            COQUIMBO_BBOX, tile_km=1000.0, resolution_m=1.0, max_image_dim=100_000, hard_cap=2500
        )
