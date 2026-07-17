# tile_generation_module (`tilegen`)

Pipeline de ingeniería de datos que descarga datasets climáticos (ERA5, CHIRPS,
CHIRTS, extensible a otros) y los publica en S3 como **cubos Zarr por escena, optimizados para series
largas de tiempo** (formato principal), o como mosaicos de **GeoTIFF
cloud-optimized (COG)** por fecha (formato alternativo para mapas).

Las **escenas** son el mapa fijo de rectángulos con nombre — una por país
(los chicos agrupados, los gigantes partidos en regiones), extendida mar
adentro donde hay costa, más regiones índice como Niño 3.4. Están definidas
en `config/scenes.yaml` y dibujadas en `docs/escenas_borrador.png`
(regenerar con `python3 docs/make_scenes_map.py`). La misma escena sirve
para variables de tierra y de mar: cada dataset llena los píxeles donde
tiene datos.

```
 fuente (HTTP / CDS API)            tilegen                          S3
┌───────────────────────┐   ┌───────────────────────┐   ┌─────────────────────────────┐
│ CHIRPS (UCSB, .tif.gz) │   │ 1. plan   ¿qué falta?  │   │ s3://suyana-tiles/          │
│ CHIRTS (UCSB, .tif)    │──▶│ 2. fetch  descarga     │──▶│   chirps/v2.0/peru.zarr     │
│ ERA5   (CDS, .nc)      │   │ 3. write  escribe cubo │   │   chirps/v2.0/bolivia.zarr  │
│ ...                    │   │           o tiles COG  │   │   era5/daily-stats/...zarr  │
└───────────────────────┘   └───────────────────────┘   └─────────────────────────────┘
```

## La idea

Un store Zarr por (dataset, escena): un solo cubo `tiempo × lat × lon` con la
historia completa del dataset, partido internamente en chunks de ~1 año ×
128×128 píxeles. Pedir 40 años de un punto o de una zona lee solo los pedacitos
necesarios, directo de S3:

```python
import xarray as xr

ds = xr.open_zarr("s3://suyana-tiles/chirps/v2.0/peru.zarr")
serie = ds.precip.sel(latitude=-3.75, longitude=-73.25, method="nearest")
serie.sel(time=slice("1981-01-01", "2025-12-31")).plot()   # ~1 lectura por año
```

Cómo se mantiene al día:

- El cubo se crea una sola vez con el **eje de tiempo completo** del dataset
  (solo metadata — no ocupa espacio). Escribir un día es rellenar su casillero,
  en cualquier orden. Días no escritos se leen como NaN.
- Un **ledger** (JSON por variable) registra qué días están escritos y cuáles
  no existen en la fuente. Re-correr salta directo a lo pendiente:
  **toda corrida es idempotente y reanudable**, y un cron diario la mantiene al día.
- **¿Por qué no Airflow/Kedro?** Todo corre en una máquina, para un usuario.
  El estado vive en S3, la orquestación la hace un cron. Si algún día hace
  falta un orquestador, las etapas ya son funciones puras.

### ¿Por qué mantener nuestra propia copia?

Ya existen copias cloud analysis-ready de parte de estos datos (ARCO-ERA5 de
Google, el mirror de DestinE, CHIRPS en Earth Engine). No recomputamos nada —
los valores pasan intactos, y solo bajamos las escenas y variables que usamos.
Aun así conviene la copia propia:

1. **Un layout uniforme para todo.** CHIRPS, ERA5 y lo que venga se ven
   idénticos para el código de análisis: mismo bucket, mismas escenas, mismas
   dimensiones. Sin malabares entre tres ecosistemas con tres autenticaciones.
2. **Independencia y estabilidad.** Mirrors de terceros piden token, se
   actualizan a su ritmo y pueden cambiar o desaparecer; la cola del CDS
   fluctúa. El cálculo de un payout no debe depender de que un servicio ajeno
   esté arriba ese día. Nuestra copia está en nuestra región de AWS: lecturas
   rápidas, gratis y bajo nuestro control.
3. **Reproducibilidad.** Un trigger paramétrico calculado hoy debe poder
   recalcularse idéntico en dos años. Datos congelados y versionados
   (`chirps/v2.0/...`) dan eso; apuntar a un servicio externo vivo, no.
4. **El costo de duplicar es trivial** — unos GB de S3 por escena, contra
   re-descargar los mismos archivos globales en cada análisis.

El límite: si algún día hace falta cobertura **global** o muchas más
variables, deja de tener sentido espejar y conviene leer ARCO-ERA5/EDH
directo. Para escenas por país y un puñado de variables, la copia local gana.

## Layout en S3

```
s3://suyana-tiles/
  chirps/v2.0/peru.zarr/                    # cubo: precip (tiempo x lat x lon)
  chirps/v2.0/bolivia.zarr/                 # mismo dataset, otra escena
  chirps/v2.0/_ledger/peru/precip.json      # qué días están escritos
  era5/daily-stats/nino34.zarr/             # t2m_mean, t2m_max, precip_sum
  chirps/v2.0/precip/s20w080/*.tif          # (formato cog, si se usa --format cog)
```

Escenas definidas en `config/scenes.yaml` (21 escenas: ver
`tilegen scenes` o el mapa en `docs/`).

## Instalación

```bash
cd ~/tile_generation_module
pip install -e .
```

Credenciales: `~/.aws` o rol de instancia para S3; `~/.cdsapirc` para ERA5.

## Uso

```bash
tilegen datasets                     # datasets configurados
tilegen scenes                       # el mapa de escenas (numeradas)
tilegen init-bucket                  # crea el bucket (privado) si no existe

# ver qué haría, sin descargar nada
tilegen plan -d chirps -x peru -s 2024-01-01 -e 2024-12-31

# descargar y escribir al cubo (idempotente: solo lo que falta)
tilegen run -d chirps -x peru -s 2024-01-01 -e 2024-12-31
tilegen run -d era5 -v t2m_mean -x nino34 -s 2024-01-01 -e 2024-01-31
tilegen run -d chirps -x peru -x bolivia     # varias escenas en una corrida
tilegen run -d chirps -x peru                # sin fechas = último día disponible

# probar sin tocar S3 (escribe bajo .work/output/)
tilegen run -d chirps -x peru -s 2024-01-15 -e 2024-01-18 --local-only

# cobertura y huecos
tilegen verify                     # catálogo: todo lo que hay en S3, por dataset/escena/variable
tilegen verify -d chirps -x peru   # o acotado a un dataset/escena
```

Opciones útiles: `--overwrite` (reescribir), `--workers N` (descargas
paralelas), `--format cog` (mosaico de GeoTIFFs por fecha en vez de cubo,
con `--bbox` en vez de `-x/--scene`).

### Backfills largos

Correr por bloques anuales, en orden cronológico (cada corrida es corta y
reanudable; si algo falla, se relanza y sigue donde quedó):

```bash
for y in $(seq 1981 2025); do
  tilegen run -d chirps -x peru -s $y-01-01 -e $y-12-31 || break
done
```

Para ERA5 la escena importa: a Copernicus se le pide solo esa ventana
espacial. Las requests van por mes y en serie (su cola penaliza el paralelo).

**ERA5 rápido, sin cola (mirror Earth Data Hub)**: con `--source era5_edh`
las lecturas van directo al espejo Zarr de DestinE en vez de la cola del CDS
— órdenes de magnitud más rápido para backfills. Requiere: (1) API key de
https://earthdatahub.destine.eu en `~/.netrc` (`machine
api.earthdatahub.destine.eu` / `password <key>`), y (2) el entorno
`tilegen` de conda (zarr>=3, Python 3.12): `conda activate tilegen`.

```bash
for y in $(seq 1981 2026); do
  tilegen run -d era5 --source era5_edh -x peru -s $y-01-01 -e $y-12-31 || break
done
```

Limitaciones del mirror: no tiene máximas diarias (`t2m_max` se baja igual
que siempre, por CDS) y va ~1 mes atrás del presente (la actualización
diaria de cron sigue por CDS).

## Actualización automática (cron)

`lag_days` en cada YAML define el retraso de publicación de la fuente; sin
fechas, `run` procesa hasta `hoy - lag_days`. Con un rango corto hacia atrás
también rellena días publicados tarde:

```cron
0 6 * * * tilegen run -d chirps -x peru -x bolivia -s $(date -d '-70 days' +\%F) >> ~/tile_generation_module/.work/cron.log 2>&1
30 6 * * * tilegen run -d era5 -x peru -x nino34 -s $(date -d '-16 days' +\%F) >> ~/tile_generation_module/.work/cron.log 2>&1
```

## Agregar un dataset o escena nueva

1. **Escena**: una entrada en `config/scenes.yaml` (y regenerar el mapa con
   `python3 docs/make_scenes_map.py`).
2. **Dataset publicado como GeoTIFF por día vía HTTP** (lo más común): crear
   `config/datasets/<nombre>.yaml` con `source: http_geotiff` y el template de
   URL — sin escribir código. Ver `chirps.yaml`.
3. **Dataset con otra API**: subclasear `DataSource` en `tilegen/sources/`
   (implementar `granules()` y `fetch()`, ~40 líneas — ver `era5.py`) y
   registrarla en `tilegen/sources/__init__.py`.


## Estructura del código

```
config/config.yaml          bucket, chunks zarr, opciones COG
config/scenes.yaml          el mapa de escenas (21 rectángulos con nombre)
config/datasets/*.yaml      un YAML por dataset (URL/CDS, variables, fechas, nodata)
tilegen/config.py           modelos pydantic + carga de YAML
tilegen/sources/            adaptadores por fuente (http_geotiff, era5_cds, era5_edh)
tilegen/assets.py           normaliza cualquier fuente a DataArray (lat, lon, float32, NaN)
tilegen/zarrstore.py        el cubo: creación, escritura por día, ledger
tilegen/zarr_pipeline.py    plan -> fetch -> write (formato zarr)
tilegen/grid.py             grilla de tiles fijos estilo MERIT (formato cog)
tilegen/tiler.py            corte a COG (formato cog)
tilegen/pipeline.py         plan -> fetch -> tile -> upload (formato cog)
tilegen/s3io.py             S3: bucket, listados, uploads con retry
tilegen/cli.py              comandos tilegen
tests/test_grid.py          tests de la grilla
```
