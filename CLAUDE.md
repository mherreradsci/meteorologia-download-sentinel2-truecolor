# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-script tool (`download_sentinel2_truecolor.py`) that downloads the latest N true-color
(RGB, bands B04/B03/B02) Sentinel-2 L2A images for an area of interest in Chile, via the
Copernicus Data Space Ecosystem (CDSE) Sentinel Hub Catalog API + Process API. There is no
package structure, build system, or test suite — everything lives in this one file.

`prompt-inicial.txt` is the original one-off spec prompt used to generate the first version of
the script. It is kept only for historical reference — do not read it or treat it as the current
requirements; requirements evolve from conversation with the user, not from that file.

## Setup and running

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requires CDSE OAuth credentials (`SH_CLIENT_ID`, `SH_CLIENT_SECRET`), created via the
[Copernicus Data Space Dashboard](https://shapps.dataspace.copernicus.eu/dashboard/). Either
export them or put them in a `.env` file (auto-loaded by `load_dotenv_if_present()`; does not
override already-exported shell vars).

```bash
# By region + point of interest (geocoded with Nominatim/OSM inside the region)
python download_sentinel2_truecolor.py --region "Coquimbo" --point "Tongoy" -n 5 --output-dir ./salidas

# By explicit bbox [min_lon, min_lat, max_lon, max_lat]
python download_sentinel2_truecolor.py --bbox -71.7 -30.3 -71.3 -29.9 -n 3 --no-preview

# By GeoJSON file (bbox extracted from the geometry via shapely)
python download_sentinel2_truecolor.py --geojson ./aoi/mi_area.geojson -n 3 --no-preview

# List supported Chile regions
python download_sentinel2_truecolor.py --list-regions
```

There is a small `tests/` suite (pytest) covering `build_output_filename`'s filename-sanitization
logic — see `tests/test_build_output_filename.py`. Install dev deps and run it with:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

Beyond that, there is no linter or CI config in this repo — don't assume `ruff`/etc. are
available unless you add them yourself.

## Architecture

The script is organized as a linear pipeline, all driven from
`download_latest_true_color_images(...)` (the "main" function referenced by the CLI and meant
to be reusable programmatically):

1. **AOI resolution** — exactly one of: an explicit `--bbox`, `--region` + `--point`, or
   `--geojson`. For the region+point path: `normalize_region_name()` resolves the region against
   `CHILE_REGIONS` (accent/case-insensitive), then `geocode_point()` calls Nominatim scoped to
   that region's bbox, then `point_to_bbox()` expands the point into a bbox using `--buffer-km`.
   `CHILE_REGIONS` bboxes are coarse — used only to bound/validate geocoding, not as
   administrative boundaries. For `--geojson`, `bbox_from_geojson()` reads the file (accepting a
   `FeatureCollection`, a bare `Feature`, or a raw geometry) and uses shapely to take the union
   bounds of all geometries found; no buffer is applied, since the geometry already defines the
   precise boundary. The AOI label used in output filenames is the point name, a `bbox_...`
   slug, or the geojson filename stem, respectively.
2. **Auth** — `TokenManager` caches a CDSE OAuth token and refetches it ~60s before expiry.
3. **Catalog search** — `find_latest_items()` queries `search_catalog()` (Sentinel Hub Catalog
   API) over a date window, filters via `is_probably_daytime()`, and sorts descending by date.
   If fewer than N items are found, the window doubles (`lookback_days` → `max_lookback_days`)
   until enough results appear or the cap is hit. No cloud filtering is applied anywhere.
4. **Rendering** — for each catalog item, `build_true_color_evalscript()` generates a Sentinel
   Hub evalscript (v3) applying gain/gamma to B04/B03/B02, and `fetch_true_color_image()` posts
   it to the Process API to render a PNG at dimensions computed by
   `compute_image_dimensions()` (target resolution in m/px, capped by `--max-image-dim` and a
   hard 2500px cap).
5. **Output** — each image is saved via `save_png()` with a filename from
   `build_output_filename()`: `{aoi_slug}_{acquisition_timestamp}_{sha1(item_id)[:8]}.png`. The
   timestamp (not just date) and hashed item ID both guard against collisions, since a tile can
   have multiple acquisitions per day and item IDs share a common prefix. Unless
   `--no-preview`, `preview_images()` shows a matplotlib grid of the downloaded images.

Cross-cutting concerns worth knowing about when touching any of the above:

- **Retries**: `retry_with_backoff()` wraps `TokenManager._fetch`, `geocode_point`,
  `search_catalog`, and `fetch_true_color_image`, retrying on 429/5xx and network errors
  (honoring `Retry-After` when present). `search_catalog`/`fetch_true_color_image` re-raise
  non-transient HTTP errors as `CatalogError`/`ProcessAPIError` before the decorator sees them,
  so only transient statuses actually get retried.
- **Nominatim rate limiting**: `_NominatimPacer` enforces a ~1.1s minimum gap between calls per
  Nominatim's usage policy — this is a shared module-level instance, not per-call.
- **No cloud filtering, daytime-only**: intentional per the original spec — all diurnal
  acquisitions are downloaded regardless of cloud cover; `is_probably_daytime()` only excludes
  items with explicit non-positive sun elevation metadata (absence of the field doesn't exclude,
  since Sentinel-2 passes over Chile around ~10:30 local solar time).
