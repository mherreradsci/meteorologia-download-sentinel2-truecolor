# Sentinel-2 True Color Downloader (Chile)

Descarga las últimas N imágenes reales en color verdadero (RGB, bandas B04/B03/B02)
de Sentinel-2 L2A para un área de interés en Chile, vía la Copernicus Data Space
Ecosystem (CDSE) Sentinel Hub Catalog API + Process API. No filtra por nubosidad.

## Requisitos

- Python 3.12.4 (ver `.python-version`).
- Credenciales OAuth de CDSE (Catalog + Process API): cree un cliente OAuth en el
  [Copernicus Data Space Dashboard](https://shapps.dataspace.copernicus.eu/dashboard/).
  Puede exportarlas en el shell:

  ```bash
  export SH_CLIENT_ID="..."
  export SH_CLIENT_SECRET="..."
  ```

  o, más simple, crear un archivo `.env` en el directorio del proyecto (el script
  lo carga automáticamente si existe, sin sobreescribir variables ya exportadas):

  ```
  SH_CLIENT_ID=...
  SH_CLIENT_SECRET=...
  ```

## Instalación

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Uso

```bash
# Por región + punto de interés (geocodificado con Nominatim/OSM dentro de la región)
python download_sentinel2_truecolor.py --region "Coquimbo" --point "Tongoy" -n 5 --output-dir ./salidas

# Por bbox explícito [min_lon, min_lat, max_lon, max_lat]
python download_sentinel2_truecolor.py --bbox -71.7 -30.3 -71.3 -29.9 -n 3 --no-preview

# Por archivo GeoJSON (bbox extraído de la geometría con shapely)
python download_sentinel2_truecolor.py --geojson ./aoi/mi_area.geojson -n 3 --no-preview

# Listar las regiones de Chile soportadas
python download_sentinel2_truecolor.py --list-regions
```

Los PNG resultantes se guardan en `--output-dir` (por defecto `output/`), y se
muestra un preview con matplotlib salvo que se use `--no-preview`.

## Notas

- Los bounding boxes de las regiones de Chile en `CHILE_REGIONS` son aproximados;
  solo se usan para acotar/validar la geocodificación del punto de interés, no
  como límites administrativos exactos.
- No se aplica filtro de nubosidad: se descargan todas las adquisiciones diurnas
  encontradas, estén o no nubladas.
