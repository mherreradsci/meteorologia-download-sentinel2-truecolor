# Sentinel-2 True Color Downloader (Chile)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

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

# Mosaico en tiles para un AOI grande (p.ej. una región entera de Chile)
python download_sentinel2_truecolor.py --geojson ./aoi/mi_region.geojson --tile-km 100 --no-preview
```

Los PNG resultantes se guardan en `--output-dir` (por defecto `output/`), y se
muestra un preview con matplotlib salvo que se use `--no-preview`.

## Mosaico en tiles (`--tile-km`)

Sentinel-2 tiene una órbita casi polar (~98° de inclinación): el borde de cada
pasada (swath) sobre el terreno queda diagonal respecto a la grilla lat/lon, no
vertical. Si el AOI pedido es más ancho que una pasada (~290 km), pedir **una
sola** imagen sobre todo el bbox deja gran parte del área en negro (nodata) —
la mitad del PNG con datos reales y el resto cortado por esa diagonal.

`--tile-km` evita esto dividiendo el AOI en una grilla de sub-bboxes de ~N km
de lado (`compute_regional_grid`), pensados para caer bien adentro de una sola
pasada. Cada tile se consulta por separado en el Catalog API (la
disponibilidad de escenas varía por ubicación) y, si tiene al menos una
adquisición diurna en la fecha del mosaico, se descarga la imagen de ese día
completo — la Process API ya compone por sí sola todas las pasadas de una
misma fecha UTC (`mosaickingOrder: mostRecent` sobre el rango 00:00–23:59Z).
Los tiles se pegan en un único mosaico según offsets en píxeles calculados
con una latitud de referencia y un redondeo compartidos entre todos los
tiles, así calzan exactos sin costuras visibles.

`-n/--count` funciona igual que en el modo de una sola pieza, pero sobre
mosaicos en vez de imágenes sueltas: con `--count 30` arma 30 mosaicos, uno
por cada una de las 30 fechas más recientes con al menos una adquisición en
algún punto del AOI (`find_latest_distinct_dates`, sobre el bbox completo —
no una consulta por tile). Default `--count 1`: solo el mosaico más reciente.

Cada mosaico es un compuesto estricto de **una sola fecha UTC**: un tile sin
cobertura ese día queda en negro (nodata), y un tile cubierto solo
parcialmente conserva el negro fuera del swath — nunca se rellena con datos
de otra fecha. `--black-fraction-threshold` solo controla cuándo se loguea el
aviso de cobertura parcial; no cambia qué datos se usan. Esto es deliberado:
las imágenes se contrastan con mapas de susceptibilidad de inundación de
fecha conocida, y mezclar fechas dentro de un mismo mosaico invalidaría esa
comparación. Con `--keep-tiles` además se guarda cada tile individual en
`<output-dir>/tiles/`, útil para inspeccionar de dónde salió cada parte del
mosaico.

## Notas

- Los bounding boxes de las regiones de Chile en `CHILE_REGIONS` son aproximados;
  solo se usan para acotar/validar la geocodificación del punto de interés, no
  como límites administrativos exactos.
- No se aplica filtro de nubosidad: se descargan todas las adquisiciones diurnas
  encontradas, estén o no nubladas.
