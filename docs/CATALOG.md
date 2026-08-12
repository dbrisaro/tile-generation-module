# Catálogo de datos climáticos — `s3://suyana-tiles`

_Generado 2026-08-12. Cobertura leída de los ledgers reales en S3 (no sólo de la config)._

Todos los datasets son **Zarr diarios** sobre EPSG:4326, partidos por escena (zona geográfica). Detalle completo por escena en [`catalog.csv`](catalog.csv) y [`catalog.yaml`](catalog.yaml).

## Cómo abrir un store

```python
import xarray as xr
ds = xr.open_zarr("s3://suyana-tiles/era5/daily-stats/peru.zarr", consolidated=True)
ds["t2m_mean"].sel(time="2024-01-15").plot()
```

## Datasets

| Dataset | Versión | Resolución | Rango con datos | Variables | Escenas |
|---|---|---|---|---|---|
| **cfs** | operational-analysis | 0.204° | 2011-04-01 → 2026-03-31 | `tmp2m`, `u10m`, `v10m` | 1 |
| **chirps** | v2.0 | 0.05° | 1981-01-01 → 2026-06-28 | `precip` | 20 |
| **chirps-v3** | v3.0 | 0.05° | 1981-01-01 → 2020-01-02 | `precip_rnl`, `precip_sat` | 20 |
| **chirts** | v1.0 | 0.05° | 1983-01-01 → 1989-12-31 | `tmax`, `tmin` | 20 |
| **era5** | daily-stats | 0.25° | 1981-01-01 → 2026-08-06 | `t2m_mean`, `t2m_max`, `precip_sum`, `swvl1` | 21 |

### cfs — CFS operational analysis (CFSv2), ~0.2° global (NOAA NCEI). Media diaria del análisis 6-horario (step=0). tmp2m + viento en componentes separadas u10m/v10m.

- **Fuente:** `cfs_ncei` · **Resolución:** 0.204° · **Rango configurado:** 2011-04-01 -> hoy
- **Variables:** `tmp2m` (K), `u10m` (m s-1), `v10m` (m s-1)
- **Ruta S3:** `s3://suyana-tiles/cfs/operational-analysis/<scene>.zarr`
- **Escenas con datos (1):** bolivia

### chirps — CHIRPS v2.0 — global daily precipitation, 0.05°, 50S–50N (UCSB CHC)

- **Fuente:** `http_geotiff` · **Resolución:** 0.05° · **Rango configurado:** 1981-01-01 -> hoy
- **Variables:** `precip` (mm/day)
- **Ruta S3:** `s3://suyana-tiles/chirps/v2.0/<scene>.zarr`
- **Escenas con datos (20):** mexico_norte, mexico_sur, centroamerica, antillas_mayores, antillas_menores, colombia, venezuela, guayanas, ecuador_galapagos, peru, bolivia, brasil_amazonia, brasil_nordeste, brasil_centro_sudeste, brasil_sur, paraguay, uruguay, chile_norte_centro, argentina_norte, patagonia

### chirps-v3 — CHIRPS v3.0 — global daily precipitation, 0.05°, 60S–60N (UCSB CHC). Dos productos diarios que reparten los totales pentadales a días — rnl (basado en ERA5, histórico consistente 1981+) y sat (basado en NASA IMERG, 2000+).

- **Fuente:** `http_geotiff` · **Resolución:** 0.05° · **Rango configurado:** 1981-01-01 -> hoy
- **Variables:** `precip_rnl` (mm/day), `precip_sat` (mm/day)
- **Ruta S3:** `s3://suyana-tiles/chirps-v3/v3.0/<scene>.zarr`
- **Escenas con datos (20):** mexico_norte, mexico_sur, centroamerica, antillas_mayores, antillas_menores, colombia, venezuela, guayanas, ecuador_galapagos, peru, bolivia, brasil_amazonia, brasil_nordeste, brasil_centro_sudeste, brasil_sur, paraguay, uruguay, chile_norte_centro, argentina_norte, patagonia

### chirts — CHIRTS-daily v1.0 — global daily Tmax/Tmin, 0.05°, fixed archive 1983–2016 (UCSB CHC)

- **Fuente:** `http_geotiff` · **Resolución:** 0.05° · **Rango configurado:** 1983-01-01 -> 2016-12-31
- **Variables:** `tmax` (degC), `tmin` (degC)
- **Ruta S3:** `s3://suyana-tiles/chirts/v1.0/<scene>.zarr`
- **Escenas con datos (20):** mexico_norte, mexico_sur, centroamerica, antillas_mayores, antillas_menores, colombia, venezuela, guayanas, ecuador_galapagos, peru, bolivia, brasil_amazonia, brasil_nordeste, brasil_centro_sudeste, brasil_sur, paraguay, uruguay, chile_norte_centro, argentina_norte, patagonia

### era5 — ERA5 single-levels daily statistics, 0.25° global (Copernicus CDS, ~/.cdsapirc required)

- **Fuente:** `era5_cds` · **Resolución:** 0.25° · **Rango configurado:** 1940-01-01 -> hoy
- **Variables:** `t2m_mean` (K), `t2m_max` (K), `precip_sum` (m), `swvl1` (m3 m-3)
- **Ruta S3:** `s3://suyana-tiles/era5/daily-stats/<scene>.zarr`
- **Escenas con datos (21):** mexico_norte, mexico_sur, centroamerica, antillas_mayores, antillas_menores, colombia, venezuela, guayanas, ecuador_galapagos, peru, bolivia, brasil_amazonia, brasil_nordeste, brasil_centro_sudeste, brasil_sur, paraguay, uruguay, chile_norte_centro, argentina_norte, patagonia, nino34

