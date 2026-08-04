#!/usr/bin/env python3
"""
Descarga las últimas N imágenes reales en color verdadero (RGB, bandas B04/B03/B02)
de Sentinel-2 L2A para un área de interés en Chile, usando la Copernicus Data Space
Ecosystem (CDSE) Sentinel Hub Catalog API + Process API.

Autenticación OAuth vía variables de entorno:
    SH_CLIENT_ID
    SH_CLIENT_SECRET

Ver el ejemplo de uso al final del archivo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests
from shapely.geometry import shape

def _resolve_log_level_from_env(default: int = logging.INFO) -> int:
    """Nivel de logging leído de LOGLEVEL (nombre estándar: DEBUG/INFO/WARNING/...).
    Si no está seteada o no es un nombre válido, usa `default`."""
    env_level = os.environ.get("LOGLEVEL")
    if not env_level:
        return default
    resolved = getattr(logging, env_level.strip().upper(), None)
    return resolved if isinstance(resolved, int) else default


logging.basicConfig(level=_resolve_log_level_from_env(), format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sentinel2_truecolor")

# --------------------------------------------------------------------------------------
# Constantes / endpoints
# --------------------------------------------------------------------------------------

CDSE_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
SH_CATALOG_URL = "https://sh.dataspace.copernicus.eu/api/v1/catalog/1.0.0/search"
SH_PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "sentinel2-chile-truecolor-script/1.0 (contact: marcelo.herrerat@gmail.com)"
COLLECTION = "sentinel-2-l2a"

# Bounding boxes aproximados (min_lon, min_lat, max_lon, max_lat) de las 16 regiones
# de Chile. Son estimaciones de grano grueso pensadas solo para acotar la búsqueda de
# geocodificación (Nominatim) y validar que el punto encontrado cae dentro de la
# región indicada -- no pretenden ser límites administrativos exactos.
CHILE_REGIONS: Dict[str, Tuple[float, float, float, float]] = {
    "Arica y Parinacota": (-70.2, -19.5, -68.6, -17.5),
    "Tarapacá": (-70.2, -21.5, -68.5, -19.2),
    "Antofagasta": (-70.5, -26.1, -67.0, -20.9),
    "Atacama": (-71.3, -29.5, -68.0, -25.6),
    "Coquimbo": (-71.7, -32.3, -69.8, -29.0),
    "Valparaíso": (-71.8, -33.6, -70.0, -32.0),
    "Metropolitana de Santiago": (-71.2, -34.3, -70.0, -32.9),
    "Libertador General Bernardo O'Higgins": (-72.0, -35.0, -70.0, -33.9),
    "Maule": (-72.7, -36.5, -70.3, -34.6),
    "Ñuble": (-72.5, -37.2, -71.0, -36.0),
    "Biobío": (-73.6, -38.5, -71.0, -36.6),
    "La Araucanía": (-73.6, -39.3, -70.9, -37.8),
    "Los Ríos": (-73.2, -40.6, -71.4, -39.2),
    "Los Lagos": (-74.4, -43.5, -71.0, -40.5),
    "Aysén": (-75.8, -49.2, -70.0, -43.4),
    "Magallanes y de la Antártica Chilena": (-75.5, -56.0, -66.0, -48.3),
}


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def normalize_region_name(name: str) -> str:
    """Resuelve `name` a una clave exacta de CHILE_REGIONS, tolerando
    mayúsculas/minúsculas y acentos. Lanza ValueError si no hay coincidencia."""
    target = _strip_accents(name).strip().lower()
    for region_name in CHILE_REGIONS:
        if _strip_accents(region_name).lower() == target:
            return region_name
    valid = ", ".join(sorted(CHILE_REGIONS))
    raise ValueError(f"Región '{name}' no reconocida. Regiones válidas: {valid}")


def load_dotenv_if_present(path: str = ".env") -> None:
    """Carga pares KEY=VALUE desde un archivo .env (si existe) hacia el entorno,
    sin sobreescribir variables ya exportadas en el shell. Evita depender de
    python-dotenv para mantener requirements.txt mínimo; no es un parser
    dotenv completo (solo líneas KEY=VALUE, comentarios '#' y comillas)."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


# --------------------------------------------------------------------------------------
# Excepciones
# --------------------------------------------------------------------------------------

class GeocodingError(Exception):
    pass


class AuthError(Exception):
    pass


class CatalogError(Exception):
    pass


class ProcessAPIError(Exception):
    pass


class GeoJSONError(Exception):
    pass


# --------------------------------------------------------------------------------------
# Reintentos / rate limiting básico
# --------------------------------------------------------------------------------------

def retry_with_backoff(
    max_retries: int = 5,
    backoff_factor: float = 1.5,
    retry_statuses: Tuple[int, ...] = (429, 500, 502, 503, 504),
) -> Callable:
    """Decorador: reintenta con backoff exponencial ante errores de red o
    respuestas HTTP en `retry_statuses`, respetando el header Retry-After."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    if status not in retry_statuses or attempt == max_retries - 1:
                        raise
                    last_exc = exc
                    retry_after = None
                    if exc.response is not None:
                        retry_after = exc.response.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            sleep_s = float(retry_after)
                        except ValueError:
                            sleep_s = backoff_factor ** attempt
                    else:
                        sleep_s = backoff_factor ** attempt
                    log.warning(
                        "%s devolvió %s, reintentando en %.1fs (intento %d/%d)",
                        func.__name__, status, sleep_s, attempt + 1, max_retries,
                    )
                    time.sleep(sleep_s)
                except requests.RequestException as exc:
                    if attempt == max_retries - 1:
                        raise
                    last_exc = exc
                    sleep_s = backoff_factor ** attempt
                    log.warning(
                        "%s falló (%s), reintentando en %.1fs (intento %d/%d)",
                        func.__name__, exc, sleep_s, attempt + 1, max_retries,
                    )
                    time.sleep(sleep_s)
            assert last_exc is not None  # el for siempre retorna o re-lanza en el último intento
            raise last_exc  # pragma: no cover - inalcanzable en la práctica
        return wrapper
    return decorator


class _NominatimPacer:
    """Fuerza un mínimo de ~1.1s entre llamadas a Nominatim, según su política de uso."""

    def __init__(self, min_interval: float = 1.1):
        self.min_interval = min_interval
        self._last_call: Optional[float] = None

    def wait(self) -> None:
        if self._last_call is not None:
            elapsed = time.monotonic() - self._last_call
            remaining = self.min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_call = time.monotonic()


_nominatim_pacer = _NominatimPacer()


# --------------------------------------------------------------------------------------
# Autenticación OAuth (CDSE)
# --------------------------------------------------------------------------------------

class TokenManager:
    """Obtiene y cachea el token OAuth de CDSE, renovándolo antes de que expire."""

    def __init__(self, client_id: str, client_secret: str, session: requests.Session):
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session
        self._access_token: Optional[str] = None
        self._fetched_at: float = 0.0
        self._expires_in: float = 0.0

    @retry_with_backoff()
    def _fetch(self) -> None:
        response = self.session.post(
            CDSE_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        if response.status_code in (400, 401):
            raise AuthError(
                f"Autenticación OAuth rechazada por CDSE ({response.status_code}): {response.text}"
            )
        response.raise_for_status()
        payload = response.json()
        access_token = payload.get("access_token")
        if not access_token:
            raise AuthError(f"Respuesta OAuth sin access_token: {payload}")
        self._access_token = access_token
        self._fetched_at = time.time()
        self._expires_in = float(payload.get("expires_in", 300))

    @property
    def token(self) -> str:
        expired = self._access_token is None or (
            time.time() - self._fetched_at > self._expires_in - 60
        )
        if expired:
            self._fetch()
        assert self._access_token is not None
        return self._access_token


# --------------------------------------------------------------------------------------
# Geocodificación (Nominatim / OSM)
# --------------------------------------------------------------------------------------

@retry_with_backoff()
def geocode_point(
    point_name: str,
    region: str,
    region_bbox: Tuple[float, float, float, float],
    session: requests.Session,
) -> Tuple[float, float]:
    """Geocodifica `point_name` dentro de `region` usando Nominatim. Devuelve (lon, lat)."""
    min_lon, min_lat, max_lon, max_lat = region_bbox
    params = {
        "q": f"{point_name}, {region}, Chile",
        "format": "json",
        "limit": 5,
        "countrycodes": "cl",
        # Nominatim espera el viewbox como left,top,right,bottom = min_lon,max_lat,max_lon,min_lat
        "viewbox": f"{min_lon},{max_lat},{max_lon},{min_lat}",
        "bounded": 1,
    }
    _nominatim_pacer.wait()
    response = session.get(
        NOMINATIM_URL, params=params, headers={"User-Agent": NOMINATIM_USER_AGENT}, timeout=20
    )
    response.raise_for_status()
    results = response.json()
    if not results:
        raise GeocodingError(
            f"No se encontró '{point_name}' dentro de la región '{region}' vía Nominatim."
        )
    top = results[0]
    lon, lat = float(top["lon"]), float(top["lat"])
    if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
        raise GeocodingError(
            f"El punto geocodificado para '{point_name}' ({lat:.4f}, {lon:.4f}) "
            f"cae fuera de la región '{region}'."
        )
    return lon, lat


def point_to_bbox(lon: float, lat: float, buffer_km: float) -> List[float]:
    """Construye un bbox [min_lon, min_lat, max_lon, max_lat] centrado en (lon, lat)."""
    dlat = buffer_km / 111.32
    dlon = buffer_km / (111.32 * math.cos(math.radians(lat)))
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]


def bbox_from_geojson(path: str) -> List[float]:
    """Extrae un bbox [min_lon, min_lat, max_lon, max_lat] de un archivo GeoJSON,
    tomando la unión de los bounds de todas las geometrías encontradas. Soporta
    FeatureCollection, Feature suelto, o una geometría GeoJSON directa."""
    geojson_path = Path(path)
    if not geojson_path.is_file():
        raise GeoJSONError(f"Archivo GeoJSON no encontrado: {path}")
    try:
        data = json.loads(geojson_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GeoJSONError(f"Archivo GeoJSON inválido '{path}': {exc}") from exc

    gj_type = data.get("type")
    if gj_type == "FeatureCollection":
        geometries = [shape(f["geometry"]) for f in data.get("features", []) if f.get("geometry")]
    elif gj_type == "Feature":
        geometries = [shape(data["geometry"])] if data.get("geometry") else []
    else:
        geometries = [shape(data)]

    if not geometries:
        raise GeoJSONError(f"El archivo GeoJSON '{path}' no contiene geometrías.")

    min_lons, min_lats, max_lons, max_lats = zip(*(g.bounds for g in geometries))
    return [min(min_lons), min(min_lats), max(max_lons), max(max_lats)]


def validate_bbox(bbox: List[float]) -> None:
    min_lon, min_lat, max_lon, max_lat = bbox
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError(f"bbox inválido: {bbox}")
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > 1.0:
        log.warning(
            "El área de interés es grande (~%.2f grados^2); la resolución efectiva "
            "por imagen puede verse reducida por el límite de píxeles.", area
        )


def compute_image_dimensions(
    bbox: List[float], resolution_m: float, max_dim: int, hard_cap: int = 2500
) -> Tuple[int, int]:
    """Calcula el ancho/alto en píxeles del request al Process API."""
    min_lon, min_lat, max_lon, max_lat = bbox
    center_lat = (min_lat + max_lat) / 2
    width_m = (max_lon - min_lon) * 111_320 * math.cos(math.radians(center_lat))
    height_m = (max_lat - min_lat) * 110_540
    width_px = max(1, width_m / resolution_m)
    height_px = max(1, height_m / resolution_m)
    scale = min(1.0, max_dim / max(width_px, height_px))
    width_px = max(1, round(width_px * scale))
    height_px = max(1, round(height_px * scale))
    width_px = min(width_px, hard_cap)
    height_px = min(height_px, hard_cap)
    return width_px, height_px


@dataclass
class TileSpec:
    """Un sub-bbox de la grilla de mosaico, con su posición en píxeles dentro
    del canvas final. (x0, y0) es la esquina superior izquierda, (x1, y1) la
    inferior derecha (exclusiva) -- misma convención que PIL.Image.paste."""

    bbox: List[float]
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0


def compute_regional_grid(
    bbox: List[float],
    tile_km: float,
    resolution_m: float,
    max_image_dim: int,
    hard_cap: int = 2500,
) -> Tuple[List[TileSpec], int, int]:
    """Divide `bbox` en una grilla de sub-bboxes de ~`tile_km` de lado, pensada
    para que cada tile quede bien adentro de una sola pasada (swath) de
    Sentinel-2 (~290 km de ancho), evitando el corte diagonal de nodata que
    aparece al pedir una sola imagen sobre un AOI más grande que eso -- el
    swath es diagonal en vez de alineado a lat/lon porque la órbita es casi
    polar (~98° de inclinación), no vertical.

    Usa una latitud de referencia única (el centro de `bbox`) para la
    conversión grados<->metros de TODA la grilla, no una por tile: si cada
    fila usara su propio cos(lat), el ancho en píxeles de cada tile
    divergiría ligeramente del de sus vecinos y quedarían costuras visibles
    al pegar el mosaico. La escala efectiva resultante es la misma que
    devolvería `compute_image_dimensions` para el bbox completo, así que el
    canvas final respeta `max_image_dim` igual que el modo de una sola
    imagen.

    Los offsets en píxeles de cada tile se calculan como una posición
    absoluta redondeada desde el origen del bbox (no como una suma de
    anchos de tile redondeados por separado), así los tiles calzan exactos
    en el canvas sin overlap ni gaps de redondeo."""
    if tile_km <= 0:
        raise ValueError(f"tile_km debe ser positivo, se recibió {tile_km}.")

    min_lon, min_lat, max_lon, max_lat = bbox
    ref_lat = (min_lat + max_lat) / 2
    m_per_deg_lon = 111_320 * math.cos(math.radians(ref_lat))
    m_per_deg_lat = 110_540

    raw_width_px = (max_lon - min_lon) * m_per_deg_lon / resolution_m
    raw_height_px = (max_lat - min_lat) * m_per_deg_lat / resolution_m
    scale = min(1.0, max_image_dim / max(raw_width_px, raw_height_px))
    eff_resolution_m = resolution_m / scale

    def px_x(lon: float) -> int:
        return round((lon - min_lon) * m_per_deg_lon / eff_resolution_m)

    def px_y(lat: float) -> int:
        return round((max_lat - lat) * m_per_deg_lat / eff_resolution_m)

    tile_deg_lon = tile_km * 1000 / m_per_deg_lon
    tile_deg_lat = tile_km * 1000 / m_per_deg_lat
    n_cols = max(1, math.ceil((max_lon - min_lon) / tile_deg_lon))
    n_rows = max(1, math.ceil((max_lat - min_lat) / tile_deg_lat))

    tiles: List[TileSpec] = []
    for row in range(n_rows):
        tile_max_lat = max_lat - row * tile_deg_lat
        tile_min_lat = max(min_lat, tile_max_lat - tile_deg_lat)
        for col in range(n_cols):
            tile_min_lon = min_lon + col * tile_deg_lon
            tile_max_lon = min(max_lon, tile_min_lon + tile_deg_lon)
            x0, x1 = px_x(tile_min_lon), px_x(tile_max_lon)
            y0, y1 = px_y(tile_max_lat), px_y(tile_min_lat)
            if x1 <= x0 or y1 <= y0:
                continue
            tiles.append(
                TileSpec(
                    bbox=[tile_min_lon, tile_min_lat, tile_max_lon, tile_max_lat],
                    x0=x0, y0=y0, x1=x1, y1=y1,
                )
            )

    canvas_w = px_x(max_lon)
    canvas_h = px_y(min_lat)
    for t in tiles:
        if t.width > hard_cap or t.height > hard_cap:
            raise ValueError(
                f"Tile {t.bbox} pide {t.width}x{t.height}px, sobre el límite de "
                f"{hard_cap}px de la Process API. Usá --tile-km más chico o "
                f"--max-image-dim más chico."
            )
    return tiles, canvas_w, canvas_h


# --------------------------------------------------------------------------------------
# Catalog API
# --------------------------------------------------------------------------------------

@retry_with_backoff()
def search_catalog(
    session: requests.Session,
    token_manager: TokenManager,
    bbox: List[float],
    start_date: datetime,
    end_date: datetime,
    limit: int,
) -> List[dict]:
    """Busca ítems Sentinel-2 L2A en el rango de fechas dado. Sin filtro de nubosidad.

    No se envía 'sortby' (la Catalog API de CDSE lo rechaza); el orden
    descendente por fecha se garantiza igualmente vía el re-sort en
    find_latest_items."""
    body = {
        "collections": [COLLECTION],
        "bbox": bbox,
        "datetime": f"{start_date:%Y-%m-%dT00:00:00Z}/{end_date:%Y-%m-%dT23:59:59Z}",
        "limit": min(limit, 100),
    }
    response = session.post(
        SH_CATALOG_URL,
        json=body,
        headers={"Authorization": f"Bearer {token_manager.token}"},
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        if response.status_code not in (429, 500, 502, 503, 504):
            raise CatalogError(f"Catalog API devolvió {response.status_code}: {response.text}") from exc
        raise  # deja que retry_with_backoff reintente los códigos transitorios
    return response.json().get("features", [])


@retry_with_backoff()
def _fetch_catalog_page(session: requests.Session, token_manager: TokenManager, body: dict) -> dict:
    """POST crudo a la Catalog API devolviendo la respuesta completa (no solo
    'features' como search_catalog), para poder seguir el link de paginación
    'next' -- la API cadena páginas de hasta 100 ítems cada una."""
    response = session.post(
        SH_CATALOG_URL,
        json=body,
        headers={"Authorization": f"Bearer {token_manager.token}"},
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        if response.status_code not in (429, 500, 502, 503, 504):
            raise CatalogError(f"Catalog API devolvió {response.status_code}: {response.text}") from exc
        raise  # deja que retry_with_backoff reintente los códigos transitorios
    return response.json()


def is_probably_daytime(item: dict) -> bool:
    """Guardia defensiva: Sentinel-2 sobrevuela Chile siempre en horario diurno
    (~10:30 hora solar local), así que la ausencia de metadatos de elevación
    solar no descarta el ítem. Solo se descarta si el metadato existe y es <= 0."""
    props = item.get("properties", {})
    sun_elevation = props.get("sun_elevation", props.get("view:sun_elevation"))
    if sun_elevation is not None and sun_elevation <= 0:
        log.debug(
            "Descartando ítem %s por sun_elevation=%s (no diurno).",
            item.get("id", "?"),
            sun_elevation,
        )
        return False
    return True


def find_latest_items(
    session: requests.Session,
    token_manager: TokenManager,
    bbox: List[float],
    n: int,
    lookback_days: int,
    max_lookback_days: int,
) -> List[dict]:
    """Busca las últimas N adquisiciones diurnas, expandiendo la ventana temporal
    si no se encuentran suficientes resultados."""
    end = datetime.now(timezone.utc)
    window = lookback_days
    items: List[dict] = []
    while True:
        start = end - timedelta(days=window)
        raw_items = search_catalog(session, token_manager, bbox, start, end, max(n * 2, 20))
        items = [i for i in raw_items if is_probably_daytime(i)]
        items.sort(key=lambda i: i["properties"]["datetime"], reverse=True)
        if len(items) >= n or window >= max_lookback_days:
            break
        window = min(window * 2, max_lookback_days)
    if len(items) < n:
        log.warning("Solo se encontraron %d/%d ítems dentro de %d días.", len(items), n, window)
    return items[:n]


def find_latest_distinct_dates(
    session: requests.Session,
    token_manager: TokenManager,
    bbox: List[float],
    count: int,
    max_lookback_days: int,
    max_items: int = 2000,
) -> List[str]:
    """Busca, sobre `bbox`, las `count` fechas (YYYY-MM-DD) más recientes en
    que hubo al menos una adquisición Sentinel-2 L2A. Define los cortes
    temporales de una serie de mosaicos regionales (`download_regional_mosaic`
    con `count > 1`): un bbox grande (varios tiles) puede tener varios ítems
    (distintas órbitas/tiles) el mismo día, que acá cuentan como una sola
    fecha.

    Confirmado en vivo contra la Catalog API real: los ítems vienen
    ordenados del más reciente al más antiguo, y la respuesta trae paginación
    real (`context`/`links` con `rel: next`) -- por eso esta función pagina
    en vez de ensanchar la ventana de fechas como `find_latest_items`. Antes
    se pedía una sola página de 100 ítems por consulta y se reintentaba con
    una ventana más ancha si `count` no se alcanzaba, pero como la API ya
    devuelve los más recientes primero sin importar cuán atrás llegue el
    filtro de fecha, esa primera página de 100 es siempre la misma sin
    importar la ventana -- ensanchar nunca ayudaba, solo repetía la consulta.
    `max_items` es un techo de seguridad (no expuesto en la CLI) para no
    paginar sin límite si `count` es muy alto sobre un AOI con cobertura
    densa continua."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max_lookback_days)
    body = {
        "collections": [COLLECTION],
        "bbox": bbox,
        "datetime": f"{start:%Y-%m-%dT00:00:00Z}/{end:%Y-%m-%dT23:59:59Z}",
        "limit": 100,
    }
    dates: set = set()
    items_seen = 0
    while True:
        page = _fetch_catalog_page(session, token_manager, body)
        features = page.get("features", [])
        items_seen += len(features)
        dates.update(f["properties"]["datetime"][:10] for f in features if is_probably_daytime(f))
        if len(dates) >= count or items_seen >= max_items:
            break
        next_link = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if next_link is None:
            break
        next_body = next_link.get("body", {})
        body = {**body, **next_body} if next_link.get("merge") else next_body

    sorted_dates = sorted(dates, reverse=True)
    if len(sorted_dates) < count:
        log.warning(
            "Solo se encontraron %d/%d fechas distintas tras revisar %d ítems (máx %d) en %d días.",
            len(sorted_dates), count, items_seen, max_items, max_lookback_days,
        )
    return sorted_dates[:count]


# --------------------------------------------------------------------------------------
# Process API
# --------------------------------------------------------------------------------------

def build_true_color_evalscript(gain: float, gamma: float) -> str:
    """Evalscript v3: color verdadero real (B04/B03/B02) con ajuste de ganancia/gamma."""
    return f"""//VERSION=3
function setup() {{
  return {{
    input: ["B04", "B03", "B02"],
    output: {{ bands: 3, sampleType: "AUTO" }},
    mosaicking: "SIMPLE"
  }};
}}
function clip(v) {{ return Math.max(0, Math.min(1, v)); }}
function evaluatePixel(sample) {{
  const gain = {gain};
  const gamma = {gamma};
  return [
    clip(Math.pow(sample.B04 * gain, gamma)),
    clip(Math.pow(sample.B03 * gain, gamma)),
    clip(Math.pow(sample.B02 * gain, gamma)),
  ];
}}
"""


@retry_with_backoff()
def fetch_true_color_image(
    session: requests.Session,
    token_manager: TokenManager,
    bbox: List[float],
    date_str: str,
    width: int,
    height: int,
    evalscript: str,
) -> bytes:
    """Renderiza vía Process API la imagen true color de `bbox` para el día `date_str`."""
    body = {
        "input": {
            "bounds": {
                "bbox": bbox,
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
            },
            "data": [
                {
                    "type": COLLECTION,
                    "dataFilter": {
                        "timeRange": {
                            "from": f"{date_str}T00:00:00Z",
                            "to": f"{date_str}T23:59:59Z",
                        },
                        "mosaickingOrder": "mostRecent",
                    },
                }
            ],
        },
        "output": {
            "width": width,
            "height": height,
            "responses": [{"identifier": "default", "format": {"type": "image/png"}}],
        },
        "evalscript": evalscript,
    }
    response = session.post(
        SH_PROCESS_URL,
        json=body,
        headers={
            "Authorization": f"Bearer {token_manager.token}",
            "Accept": "image/png",
        },
        timeout=60,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        if response.status_code not in (429, 500, 502, 503, 504):
            raise ProcessAPIError(f"Process API devolvió {response.status_code}: {response.text}") from exc
        raise  # deja que retry_with_backoff reintente los códigos transitorios
    return response.content


# --------------------------------------------------------------------------------------
# Guardado y preview
# --------------------------------------------------------------------------------------

def _slugify_label(label: str) -> str:
    # Solo se reemplazan caracteres no imprimibles (de control, etc.) y espacios por "_";
    # se preservan letras acentuadas/ñ y demás caracteres imprimibles de
    # label para mantener nombres de archivo legibles.
    slug = "".join(c if not c.isspace() and c.isprintable() else "_" for c in label)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "aoi"


def build_output_filename(aoi_label: str, item: dict) -> str:
    slug = _slugify_label(aoi_label)
    # Timestamp real de adquisición (UTC, precisión de segundos) tomado de
    # properties.datetime -- no solo la fecha, porque un mismo tile puede
    # tener más de una adquisición el mismo día (p.ej. distintas órbitas
    # relativas que se solapan sobre el AOI).
    timestamp = item["properties"]["datetime"][:19].replace("-", "").replace(":", "")
    # Todos los IDs de items Sentinel-2 L2A comparten el mismo prefijo
    # (p.ej. "S2B_MSIL2A_..."), así que un slice del inicio no es único:
    # se agrega un hash del ID completo como red de seguridad adicional
    # contra colisiones/overwrites (p.ej. reprocesos con igual timestamp).
    item_id = str(item.get("id", "na"))
    item_id_short = hashlib.sha1(item_id.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{timestamp}-{item_id_short}.png"


def build_mosaic_filename(aoi_label: str, date_range: str, n_tiles: int) -> str:
    """Nombre de archivo para el mosaico compuesto. No hay un único `item`
    STAC del que derivar timestamp/id (el mosaico junta N tiles, cada uno
    con su propia fecha), así que el hash se deriva de slug+date_range+
    n_tiles en su lugar -- mismo propósito anti-colisión que en
    build_output_filename, adaptado a que acá no hay un id de item real."""
    slug = _slugify_label(aoi_label)
    tag = hashlib.sha1(f"{slug}-{date_range}-{n_tiles}".encode("utf-8")).hexdigest()[:8]
    return f"{slug}_mosaic_{date_range}_{n_tiles}tiles_{tag}.png"


def fraction_black(image_bytes: bytes, threshold: int = 8) -> float:
    """Fracción de píxeles "negros" (nodata) en un PNG, entendido como
    luminancia por debajo de `threshold`. Sirve para detectar tiles que en
    su mayoría cayeron fuera del swath de la adquisición elegida, sin
    depender de que la Process API exponga una máscara de nodata explícita
    (entrega un PNG plano, sin canal alfa de validez). Real: con gain=2.5 y
    gamma=1.0, un píxel de reflectancia real rara vez cae en (0,0,0) exacto
    en los tres canales a la vez -- eso sí es lo que la API rellena fuera
    del área con datos."""
    from PIL import Image
    import io

    with Image.open(io.BytesIO(image_bytes)) as img:
        gray = img.convert("L")
        total = gray.width * gray.height
        if total == 0:
            return 0.0
        black = sum(gray.histogram()[:threshold])
    return black / total


def save_png(image_bytes: bytes, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    path.write_bytes(image_bytes)
    return path


def preview_images(image_paths: List[Path], titles: List[str]) -> None:
    if not image_paths:
        log.info("No hay imágenes para previsualizar.")
        return
    import matplotlib.pyplot as plt

    n = len(image_paths)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    _, axes = plt.subplots(rows, cols, figsize=(4 * cols, 4 * rows))
    axes = [axes] if n == 1 else axes.flatten()
    for ax, path, title in zip(axes, image_paths, titles):
        img = plt.imread(path)
        ax.imshow(img)
        ax.set_title(title, fontsize=9)
        ax.axis("off")
    for ax in axes[n:]:
        ax.axis("off")
    plt.tight_layout()
    plt.show()


# --------------------------------------------------------------------------------------
# Resolución de AOI (compartida entre el flujo de un solo bbox y el de mosaico en tiles)
# --------------------------------------------------------------------------------------

def resolve_aoi(
    session: requests.Session,
    region: Optional[str] = None,
    point: Optional[str] = None,
    bbox: Optional[List[float]] = None,
    geojson: Optional[str] = None,
    buffer_km: float = 10.0,
) -> Tuple[List[float], str]:
    """Resuelve el AOI a un bbox [min_lon, min_lat, max_lon, max_lat] + una etiqueta legible
    para nombres de archivo, a partir de exactamente uno de: --bbox, --geojson, o
    --region junto con --point. Válida el bbox resultante (validate_bbox) antes de
    devolverlo."""
    has_bbox = bbox is not None
    has_region_point = region is not None and point is not None
    has_geojson = geojson is not None
    if sum([has_bbox, has_region_point, has_geojson]) != 1:
        raise ValueError(
            "Debe indicar exactamente uno de: --bbox, --geojson, o --region junto con --point."
        )

    if has_region_point:
        assert region is not None and point is not None
        region_name = normalize_region_name(region)
        region_bbox = CHILE_REGIONS[region_name]
        lon, lat = geocode_point(point, region_name, region_bbox, session)
        resolved_bbox = point_to_bbox(lon, lat, buffer_km)
        aoi_label = point
        log.info("Punto '%s' geocodificado en (%.4f, %.4f) -> bbox %s", point, lat, lon, resolved_bbox)
    elif has_geojson:
        assert geojson is not None
        resolved_bbox = bbox_from_geojson(geojson)
        aoi_label = Path(geojson).stem
        log.info("Bbox %s extraído de geojson '%s'", resolved_bbox, geojson)
    else:
        assert bbox is not None
        resolved_bbox = list(bbox)
        aoi_label = f"bbox_{resolved_bbox[0]:.2f}_{resolved_bbox[1]:.2f}"

    validate_bbox(resolved_bbox)
    return resolved_bbox, aoi_label


# --------------------------------------------------------------------------------------
# Función principal
# --------------------------------------------------------------------------------------

def download_latest_true_color_images(
    n: int,
    output_dir: str = "output",
    region: Optional[str] = None,
    point: Optional[str] = None,
    bbox: Optional[List[float]] = None,
    geojson: Optional[str] = None,
    buffer_km: float = 10.0,
    lookback_days: int = 30,
    max_lookback_days: int = 365,
    resolution_m: float = 10.0,
    max_image_dim: int = 2000,
    gain: float = 2.5,
    gamma: float = 1.0,
    show_preview: bool = True,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> List[Path]:
    """Descarga las últimas `n` imágenes true color (Sentinel-2 L2A) para el área
    de interés indicada, ya sea por `bbox` explícito, por `geojson` (bbox extraído
    de un archivo GeoJSON), o por `region` + `point` (geocodificado dentro de la
    región). Guarda cada imagen como PNG en `output_dir`, muestra un preview con
    matplotlib (si `show_preview`) y devuelve la lista de rutas guardadas."""
    load_dotenv_if_present()
    client_id = client_id or os.environ.get("SH_CLIENT_ID")
    client_secret = client_secret or os.environ.get("SH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise AuthError(
            "Faltan credenciales OAuth: defina SH_CLIENT_ID y SH_CLIENT_SECRET "
            "como variables de entorno o páselas explícitamente."
        )

    session = requests.Session()
    resolved_bbox, aoi_label = resolve_aoi(session, region, point, bbox, geojson, buffer_km)

    token_manager = TokenManager(client_id, client_secret, session)
    width, height = compute_image_dimensions(resolved_bbox, resolution_m, max_image_dim)
    log.info("Dimensiones de imagen solicitadas: %dx%d px", width, height)

    items = find_latest_items(session, token_manager, resolved_bbox, n, lookback_days, max_lookback_days)
    if not items:
        raise CatalogError("No se encontraron adquisiciones Sentinel-2 L2A para el área/periodo indicado.")

    evalscript = build_true_color_evalscript(gain, gamma)
    saved_paths: List[Path] = []
    titles: List[str] = []
    for item in items:
        item_id = item.get("id", "?")
        date_str = item["properties"]["datetime"][:10]
        try:
            png_bytes = fetch_true_color_image(
                session, token_manager, resolved_bbox, date_str, width, height, evalscript
            )
            filename = build_output_filename(aoi_label, item)
            path = save_png(png_bytes, Path(output_dir), filename)
            saved_paths.append(path)
            titles.append(date_str)
            log.info("Guardada %s (%s)", path, date_str)
        except (ProcessAPIError, requests.RequestException) as exc:
            log.error("Omitiendo ítem %s (%s): %s", item_id, date_str, exc)

    if not saved_paths:
        raise ProcessAPIError("Todas las descargas de imágenes fallaron.")

    if show_preview:
        preview_images(saved_paths, titles)

    return saved_paths


def download_regional_mosaic(
    output_dir: str = "output",
    region: Optional[str] = None,
    point: Optional[str] = None,
    bbox: Optional[List[float]] = None,
    geojson: Optional[str] = None,
    buffer_km: float = 10.0,
    tile_km: float = 100.0,
    count: int = 1,
    max_lookback_days: int = 365,
    resolution_m: float = 10.0,
    max_image_dim: int = 2000,
    gain: float = 2.5,
    gamma: float = 1.0,
    black_fraction_threshold: float = 0.05,
    keep_tiles: bool = False,
    show_preview: bool = True,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> List[Path]:
    """Arma `count` mosaicos true color de un AOI grande, dividiéndolo en
    tiles del tamaño de un swath de Sentinel-2 (`tile_km`) para evitar el
    corte diagonal de nodata que aparece al pedir una sola imagen para un
    AOI más ancho que una pasada (ver `compute_regional_grid`).

    Primero se eligen los `count` cortes temporales con `find_latest_distinct_dates`
    sobre el AOI completo (las fechas más recientes con al menos una
    adquisición en algún punto del AOI); con `count=1` (el default) esa
    fecha es, por construcción, la más reciente de cualquier tile individual,
    así que el comportamiento coincide con "el mosaico más reciente posible"
    sin necesidad de un caso especial.

    Cada mosaico es un compuesto estricto de una sola fecha UTC (requisito
    del caso de uso: contrastar imagen de fecha conocida contra mapas de
    susceptibilidad de inundación). Para cada fecha objetivo, cada tile
    consulta el Catalog API por separado (la cobertura varía por ubicación)
    y, solo si tiene al menos una adquisición diurna en esa fecha exacta,
    descarga la imagen del día completo: la Process API compone server-side
    todas las pasadas de ese día (`mosaickingOrder: mostRecent` sobre el
    rango 00:00-23:59Z), así que las adquisiciones de una misma fecha ya se
    rellenan entre sí sin compositing cliente-lado. Un tile sin cobertura
    esa fecha queda en negro (nodata honesto), y uno cubierto solo
    parcialmente conserva el negro fuera del swath -- se loguea un warning
    si la fracción negra supera `black_fraction_threshold`, pero nunca se
    rellena con datos de otra fecha. Los tiles se pegan en un único canvas
    por fecha según los offsets en píxeles de `compute_regional_grid`, que
    calzan exactos sin costuras."""
    from PIL import Image
    import io

    load_dotenv_if_present()
    client_id = client_id or os.environ.get("SH_CLIENT_ID")
    client_secret = client_secret or os.environ.get("SH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise AuthError(
            "Faltan credenciales OAuth: defina SH_CLIENT_ID y SH_CLIENT_SECRET "
            "como variables de entorno o páselas explícitamente."
        )

    session = requests.Session()
    resolved_bbox, aoi_label = resolve_aoi(session, region, point, bbox, geojson, buffer_km)
    token_manager = TokenManager(client_id, client_secret, session)

    tiles, canvas_w, canvas_h = compute_regional_grid(resolved_bbox, tile_km, resolution_m, max_image_dim)
    log.info(
        "AOI dividido en %d tile(s) para un canvas de %dx%d px (tile_km=%.1f)",
        len(tiles), canvas_w, canvas_h, tile_km,
    )

    target_dates = find_latest_distinct_dates(session, token_manager, resolved_bbox, count, max_lookback_days)
    if not target_dates:
        raise CatalogError("No se encontraron adquisiciones Sentinel-2 L2A para el área/periodo indicado.")

    evalscript = build_true_color_evalscript(gain, gamma)
    tile_dir = Path(output_dir) / "tiles" if keep_tiles else None
    saved_paths: List[Path] = []
    titles: List[str] = []

    for date_idx, target_date in enumerate(target_dates):
        day = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
        any_success = False

        for idx, tile in enumerate(tiles):
            raw_items = search_catalog(session, token_manager, tile.bbox, day, day, 20)
            if not any(is_probably_daytime(i) for i in raw_items):
                log.info(
                    "Mosaico %s, tile %d/%d %s: sin adquisiciones esa fecha, queda en negro.",
                    target_date, idx + 1, len(tiles), tile.bbox,
                )
                continue

            try:
                png_bytes = fetch_true_color_image(
                    session, token_manager, tile.bbox, target_date, tile.width, tile.height, evalscript
                )
            except (ProcessAPIError, requests.RequestException) as exc:
                log.warning(
                    "Mosaico %s, tile %d/%d %s falló: %s; queda en negro.",
                    target_date, idx + 1, len(tiles), tile.bbox, exc,
                )
                continue

            black = fraction_black(png_bytes)
            if black > black_fraction_threshold:
                log.warning(
                    "Mosaico %s, tile %d/%d %s: %.0f%% nodata -- la(s) pasada(s) de esa fecha no "
                    "cubren el tile completo; se conserva el negro para no mezclar fechas.",
                    target_date, idx + 1, len(tiles), tile.bbox, black * 100,
                )

            with Image.open(io.BytesIO(png_bytes)) as tile_img:
                canvas.paste(tile_img.convert("RGB"), (tile.x0, tile.y0))
            any_success = True

            if tile_dir is not None:
                tile_dir.mkdir(parents=True, exist_ok=True)
                (tile_dir / f"{aoi_label}_{target_date}_tile{idx:03d}.png").write_bytes(png_bytes)

            log.info(
                "Mosaico %s, tile %d/%d %s: %.0f%% negro",
                target_date, idx + 1, len(tiles), tile.bbox, black * 100,
            )

        if not any_success:
            log.warning("Mosaico %s: ningún tile con datos, se omite.", target_date)
            continue

        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        filename = build_mosaic_filename(aoi_label, target_date, len(tiles))
        path = save_png(buf.getvalue(), Path(output_dir), filename)
        saved_paths.append(path)
        titles.append(target_date)
        log.info(
            "Mosaico %d/%d guardado en %s (%d tiles, fecha %s)",
            date_idx + 1, len(target_dates), path, len(tiles), target_date,
        )

    if not saved_paths:
        raise ProcessAPIError("Todos los mosaicos fallaron.")

    if show_preview:
        preview_images(saved_paths, titles)

    return saved_paths


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Descarga las últimas N imágenes Sentinel-2 L2A true color para un área de Chile."
    )
    parser.add_argument(
        "--bbox", nargs=4, type=float, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help="Bbox explícito del área de interés (excluyente con --region/--point y --geojson).",
    )
    parser.add_argument(
        "--geojson", type=str,
        help="Ruta a un archivo GeoJSON cuyo bbox se usará como AOI "
             "(excluyente con --bbox y --region/--point).",
    )
    parser.add_argument("--region", type=str, help="Nombre de la región de Chile (ver --list-regions).")
    parser.add_argument("--point", type=str, help="Nombre del punto de interés (ej: 'Tongoy').")
    parser.add_argument(
        "-n", "--count", type=int,
        help="Cantidad de imágenes a descargar (o de mosaicos, con --tile-km; "
             "default 1 en ese modo, requerido en el modo de una sola pieza).",
    )
    parser.add_argument("--output-dir", type=str, default="output", help="Carpeta de salida para los PNG.")
    parser.add_argument("--buffer-km", type=float, default=10.0, help="Semi-buffer en km alrededor del punto geocodificado.")
    parser.add_argument(
        "--lookback-days", type=int, default=30,
        help="Ventana inicial de búsqueda, en días (sin efecto en modo mosaico, --tile-km).",
    )
    parser.add_argument("--max-lookback-days", type=int, default=365, help="Ventana máxima de búsqueda, en días.")
    parser.add_argument("--resolution-m", type=float, default=10.0, help="Resolución objetivo en metros/píxel.")
    parser.add_argument("--max-image-dim", type=int, default=2000, help="Lado máximo de la imagen en píxeles.")
    parser.add_argument("--gain", type=float, default=2.5, help="Ganancia del realce true color.")
    parser.add_argument("--gamma", type=float, default=1.0, help="Gamma del realce true color.")
    parser.add_argument(
        "--tile-km", type=float, default=None,
        help="Activa el modo mosaico: divide el AOI en tiles de ~N km de lado "
             "(del tamaño de un swath de Sentinel-2) y arma composites en vez de "
             "descargar N imágenes de una sola pieza. Pensado para AOIs más "
             "anchos que una pasada (p.ej. una región entera), donde pedir una "
             "sola imagen deja gran parte del área en negro (nodata fuera del "
             "swath). Con -n/--count > 1 arma esa cantidad de mosaicos, uno por "
             "cada fecha reciente distinta con adquisiciones en el AOI (default "
             "1: solo el mosaico más reciente). Cada mosaico contiene SOLO datos "
             "de su fecha UTC: un tile sin cobertura ese día queda en negro, "
             "nunca se rellena con otra fecha.",
    )
    parser.add_argument(
        "--black-fraction-threshold", type=float, default=0.05,
        help="Solo con --tile-km. Fracción de píxeles negros (nodata) en un tile "
             "sobre la cual se loguea un warning de cobertura parcial; es solo "
             "informativo, el negro se conserva para no mezclar fechas.",
    )
    parser.add_argument(
        "--keep-tiles", action="store_true",
        help="Solo con --tile-km. Además del mosaico, guarda cada tile individual "
             "en '<output-dir>/tiles/' (útil para depurar coberturas parciales).",
    )
    parser.add_argument("--no-preview", action="store_true", help="No mostrar preview con matplotlib.")
    parser.add_argument("--list-regions", action="store_true", help="Listar las regiones de Chile soportadas y salir.")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Fuerza nivel de logging DEBUG (prioridad sobre la variable de entorno LOGLEVEL).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_regions:
        for name in sorted(CHILE_REGIONS):
            print(name)
        return 0

    aoi_flags_set = sum([bool(args.bbox), bool(args.geojson), bool(args.region or args.point)])
    if aoi_flags_set > 1:
        parser.error("--bbox, --geojson y --region/--point son mutuamente excluyentes.")
    if aoi_flags_set == 0:
        parser.error("Debe indicar --bbox, --geojson, o bien --region junto con --point.")
    if bool(args.region) != bool(args.point):
        parser.error("--region y --point deben usarse juntos.")
    if args.tile_km is None and args.count is None:
        parser.error("-n/--count es requerido (salvo en modo mosaico, --tile-km).")

    try:
        if args.tile_km is not None:
            download_regional_mosaic(
                output_dir=args.output_dir,
                region=args.region,
                point=args.point,
                bbox=args.bbox,
                geojson=args.geojson,
                buffer_km=args.buffer_km,
                tile_km=args.tile_km,
                count=args.count if args.count is not None else 1,
                max_lookback_days=args.max_lookback_days,
                resolution_m=args.resolution_m,
                max_image_dim=args.max_image_dim,
                gain=args.gain,
                gamma=args.gamma,
                black_fraction_threshold=args.black_fraction_threshold,
                keep_tiles=args.keep_tiles,
                show_preview=not args.no_preview,
            )
        else:
            download_latest_true_color_images(
                n=args.count,
                output_dir=args.output_dir,
                region=args.region,
                point=args.point,
                bbox=args.bbox,
                geojson=args.geojson,
                buffer_km=args.buffer_km,
                lookback_days=args.lookback_days,
                max_lookback_days=args.max_lookback_days,
                resolution_m=args.resolution_m,
                max_image_dim=args.max_image_dim,
                gain=args.gain,
                gamma=args.gamma,
                show_preview=not args.no_preview,
            )
    except (AuthError, GeocodingError, CatalogError, ProcessAPIError, GeoJSONError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"Error de red: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    # ------------------------------------------------------------------------------
    # Ejemplo de uso:
    #
    #   export SH_CLIENT_ID="..."
    #   export SH_CLIENT_SECRET="..."
    #
    #   # Por región + punto de interés
    #   python download_sentinel2_truecolor.py --region "Coquimbo" --point "Tongoy" \
    #       -n 5 --output-dir ./salidas
    #
    #   # Por bbox explícito
    #   python download_sentinel2_truecolor.py --bbox -71.7 -30.3 -71.3 -29.9 \
    #       -n 3 --no-preview
    #
    #   # Por archivo GeoJSON (bbox extraído de la geometría)
    #   python download_sentinel2_truecolor.py --geojson ./aoi/mi_area.geojson \
    #       -n 3 --no-preview
    #
    #   # Mosaico en tiles para un AOI grande (p.ej. una región entera), evitando
    #   # el corte diagonal de nodata que deja una sola imagen sobre un AOI más
    #   # ancho que un swath de Sentinel-2
    #   python download_sentinel2_truecolor.py --geojson ./aoi/mi_region.geojson \
    #       --tile-km 100 --no-preview
    # ------------------------------------------------------------------------------
    sys.exit(main())
