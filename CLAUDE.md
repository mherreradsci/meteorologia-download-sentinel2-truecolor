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

# Tiled mosaic for an AOI wider than one Sentinel-2 swath (e.g. a whole region)
python download_sentinel2_truecolor.py --geojson ./aoi/mi_region.geojson --tile-km 100 --no-preview

# Same, but a time series: 30 mosaics, one per recent distinct acquisition date
python download_sentinel2_truecolor.py --geojson ./aoi/mi_region.geojson --tile-km 100 -n 30 --no-preview
```

There is a small `tests/` suite (pytest) covering `build_output_filename`'s filename-sanitization
logic (`tests/test_build_output_filename.py`), the mosaic tile grid math
(`tests/test_regional_grid.py`), nodata detection (`tests/test_fraction_black.py`), and the
mosaic filename builder (`tests/test_mosaic_filename.py`). Install dev deps and run it with:

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

## Tiled mosaic mode (`--tile-km`)

`download_latest_true_color_images` asks the Process API for a single image over the whole
requested bbox for one calendar day (`fetch_true_color_image`'s `dataFilter.timeRange` spans
that day, `mosaickingOrder: mostRecent`). Sentinel-2's orbit is near-polar (~98° inclination), so
a swath's footprint on the ground is a diagonal strip, not axis-aligned — a bbox wider than one
swath (~290 km) ends up mostly nodata (rendered as pure black by the evalscript's `clip()`)
outside whatever diagonal sliver that day's pass actually covered. This bit real users: a whole
Chilean region (e.g. Coquimbo, ~192×359 km) requested with a small `--max-image-dim` produced
PNGs that were half real imagery, half a hard diagonal black cut.

`download_regional_mosaic` (same file, invoked via `--tile-km`) works around this by splitting
the AOI into a grid of tiles small enough to usually fit inside one swath, downloading each tile
independently, and compositing them:

1. `compute_regional_grid(bbox, tile_km, resolution_m, max_image_dim)` builds the tile grid.
   It deliberately uses **one reference latitude (the bbox center)** for the whole grid's
   degree→meter conversion, not one per tile row — per-row `cos(lat)` would make each row's
   pixel scale diverge slightly from its neighbors', leaving visible seams. Per-tile pixel
   offsets are computed as **absolute, rounded positions from the bbox origin** (not a running
   sum of independently-rounded tile widths), so adjacent tiles butt together with zero gap or
   overlap by construction — verified in `test_regional_grid.py` by rasterizing tile coverage
   and asserting every canvas cell is covered exactly once. The resulting canvas size always
   matches what `compute_image_dimensions` would return for the same bbox/resolution/max-dim, so
   `--tile-km` is a drop-in way to get the same "final image size" contract as the untiled path.
2. `find_latest_distinct_dates(bbox, count, max_lookback_days)` queries the **full AOI** (not per
   tile) to pick the `count` most recent calendar dates that had at least one acquisition
   anywhere in it — these are the mosaic's time steps. With the default `count=1`, this collapses
   to "the single most recent date any tile saw a scene," which is the same date any individual
   tile's own most-recent search would land on, so `count=1` behaves identically to always
   searching "now" — no special-casing needed for the single-mosaic case.

   This function **paginates** through the Catalog API's `context`/`links[rel=next]` mechanism
   instead of the window-widening trick `find_latest_items` uses — confirmed live that the API
   returns items newest-first and that widening the query's date range does *not* change which
   items come back on a single unpaginated call (the first page is always the same ~100 newest
   items regardless of how far back `start` reaches). A real bug shipped from getting this wrong
   the first time: over a big multi-tile bbox, ~9 items/acquisition-date (multiple
   granules/orbits touching the AOI) meant a single 100-item page covered only ~11 distinct
   dates, so `--count 100` silently returned 11. Paginating (capped by `max_items`, default 2000,
   an internal safety valve not exposed on the CLI) fixed it — verified live, `--count 100` over
   Región de Coquimbo now returns exactly 100 dates spanning ~5 months instead of stalling at 11.
3. For each target date, every tile gets its **own** Catalog API query (`search_catalog`, scoped
   to that tile's bbox) restricted to **exactly that UTC date** — scene availability varies by
   location, so the per-tile query decides whether the tile has any daytime data that date. If it
   does, a single Process API day-fetch renders it: `fetch_true_color_image`'s `timeRange`
   already spans the full UTC day with `mosaickingOrder: mostRecent`, so all of that date's
   passes over the tile are composited **server-side** — no client-side layering needed. If it
   doesn't, the tile stays black. There is deliberately **no fallback to earlier dates**: each
   mosaic is a strict single-UTC-date composite, because the images are visually compared against
   flood-susceptibility maps for a known date, and mixing acquisition dates inside one image
   would invalidate that comparison. (An earlier design fell back to the least-black
   recent-or-older scene per tile; it shipped a real failure — a fully white, cloud-saturated
   older scene scored 0% black and silently replaced the target date's mostly-good image, whose
   ~12% swath-edge corners exceeded the 5% threshold.)
4. `fraction_black()` (luminance-histogram based, no numpy needed) measures how much of a
   downloaded tile is nodata (real reflectance data at `gain=2.5, gamma=1.0` essentially never
   renders as literal `(0,0,0)`, so this is a reliable nodata proxy without needing an explicit
   alpha/validity band from the API). It is now **informational only**: above
   `--black-fraction-threshold` (default 0.05) a partial-coverage warning is logged, but the
   result is kept as-is.
5. Per target date, tiles are pasted onto a Pillow canvas at their precomputed pixel offsets and
   saved as one PNG via `build_mosaic_filename` (hashes `slug-target_date-n_tiles` instead of a
   STAC item id, since a mosaic has no single source item). `download_regional_mosaic` returns
   `List[Path]`, one per date where at least one tile had data (a date where every tile came up
   empty or failed is skipped with a warning, not aborted).

**Known limitation, not a bug**: a mosaic can contain black (nodata) areas — tiles the target
date's passes didn't reach, or swath-edge slivers within a partially covered tile. This is the
honest representation of that date's coverage and is intentional (see step 3); it's logged and
called out in the CLI help and README, not hidden.

`find_latest_items` is now used only by the untiled path (`download_latest_true_color_images`)
and still uses the older window-widening approach rather than pagination — left as is because
its per-call `limit` is small and scoped to one AOI, well under the 100-item cap in practice. If
a future change needs many items from a single query, revisit whether it needs the same
pagination treatment as `find_latest_distinct_dates`.

`resolve_aoi()` factors out the region/point/bbox/geojson resolution shared by both
`download_latest_true_color_images` and `download_regional_mosaic`, so both entry points accept
identical AOI inputs.
