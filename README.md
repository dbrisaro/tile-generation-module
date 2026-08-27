# tile_generation_module (`tilegen`)

Downloads climate datasets (ERA5, CHIRPS, CHIRTS, CPC, CFS — extensible) and
publishes them to S3 as **per-scene Zarr cubes optimized for long time series**.
A per-date **COG mosaic** format also exists, for maps (`--format cog`).

**Scenes** are 21 fixed named rectangles — one per country, small ones grouped,
large ones split, extended offshore where there is coastline, plus index regions
like Niño 3.4. They live in `config/scenes.yaml`, listed by `tilegen scenes` and
drawn in `docs/escenas_borrador.png`. The same scene serves land and ocean
variables: each dataset fills the pixels where it has data.

## The idea

One Zarr store per (dataset, scene): a single `time × lat × lon` cube with the
dataset's full history. Reading 40 years for one point fetches only the chunks it
needs, straight from S3:

```python
import xarray as xr

ds = xr.open_zarr("s3://suyana-tiles/chirps/v2.0/peru.zarr")
series = ds.precip.sel(latitude=-3.75, longitude=-73.25, method="nearest")
series.sel(time=slice("1981-01-01", "2025-12-31")).plot()   # ~1 read per year
```

How it stays current:

- The cube is created once with the dataset's **full time axis** (metadata only,
  no space). Writing a day fills its slot, in any order; unwritten days are NaN.
- A **ledger** (one JSON per variable) records which days are written and which
  do not exist at the source, so re-runs skip straight to what is missing:
  **every run is idempotent and resumable**, and a daily cron keeps it current.

**Why not Airflow/Kedro?** One machine, one user, state in S3, cron for
orchestration. The stages are pure functions if an orchestrator is ever needed.

**Why keep our own copy** when ARCO-ERA5, the DestinE mirror and CHIRPS in Earth
Engine exist? We recompute nothing — values pass through untouched — but the
copy buys one uniform layout across all datasets (same bucket, scenes,
dimensions, one auth); independence from third-party mirrors that need tokens,
change, or go down, which a payout calculation must not depend on; and
reproducibility, since a trigger computed today must recompute identically in two
years, which frozen versioned data (`chirps/v2.0/...`) gives and a live service
does not. Cost is a few GB of S3 per scene. The limit: if **global** coverage or
many more variables are ever needed, reading ARCO-ERA5/EDH directly wins.

## S3 layout

```
s3://suyana-tiles/
  chirps/v2.0/peru.zarr/                    # cube: precip (time x lat x lon)
  chirps/v2.0/bolivia.zarr/                 # same dataset, another scene
  chirps/v2.0/_ledger/peru/precip.json      # which days are written
  era5/daily-stats/nino34.zarr/             # t2m_mean, t2m_max, precip_sum, ...
  chirps/v2.0/precip/s20w080/*.tif          # (cog format)
```

Current coverage per dataset/variable/scene: `docs/cobertura.html`.

## Install

```bash
cd ~/tile_generation_module && pip install -e .           # development

# from another environment (no clone needed)
python -m venv .venv && . .venv/bin/activate
pip install "git+https://github.com/dbrisaro/tile-generation-module.git@v0.4.0"
```

Install into its own venv rather than `~/.local`: the `s3fs`/`boto3` lower bounds
are strict on purpose and fight whatever is already in a shared environment. The
exact combination running in production is
`~/tilegen-prod/repo-requirements-lock.txt`.

**Reading the published cubes does not need tilegen** — `xarray` + `s3fs` is
enough.

Credentials: `~/.aws` or an instance role for S3; `~/.cdsapirc` for ERA5.

## Usage

```bash
tilegen datasets                     # configured datasets
tilegen scenes                       # the scene map (numbered)
tilegen init-bucket                  # create the (private) bucket if missing

tilegen plan -d chirps -x peru -s 2024-01-01 -e 2024-12-31   # downloads nothing
tilegen run  -d chirps -x peru -s 2024-01-01 -e 2024-12-31   # only what is missing
tilegen run  -d era5 -v t2m_mean -x nino34 -s 2024-01-01 -e 2024-01-31
tilegen run  -d chirps -x peru -x bolivia    # several scenes in one run
tilegen run  -d chirps -x peru               # no dates = latest available day
tilegen run  -d chirps -x peru --local-only  # no S3, writes under .work/output/

tilegen verify                     # what is in S3, and the gaps
tilegen chunks                     # how each cube is chunked, and read cost
tilegen chunks --stale             # only cubes that predate the current config
```

Also: `--overwrite`, `--workers N`, `--format cog` (uses `--bbox` instead of
`-x/--scene`).

### Long backfills

Run in yearly blocks, chronologically — each run is short and resumable, so a
failure just means relaunching:

```bash
for y in $(seq 1981 2025); do
  tilegen run -d chirps -x peru -s $y-01-01 -e $y-12-31 || break
done
```

**ERA5 has two routes.** The CDS is a queue that costs ~20 min per request
regardless of size, so what matters is the *number* of requests, not their size —
parallelism does not help. `cds_fetch_area` in `era5.yaml` therefore requests one
window covering all 21 scenes and clips each scene locally: one request per month
instead of one per month *per scene*. Without it, each scene requests its own
window (still the case for `--bbox` runs).

**Faster, no queue: the Earth Data Hub mirror.** With `--source era5_edh` reads
go straight to the DestinE Zarr mirror — orders of magnitude faster for
backfills. Needs an API key from https://earthdatahub.destine.eu in `~/.netrc`
(`machine api.earthdatahub.destine.eu` / `password <key>`) and the `tilegen`
conda environment (zarr>=3, Python 3.12).

```bash
for y in $(seq 1981 2026); do
  tilegen run -d era5 --source era5_edh -x peru -s $y-01-01 -e $y-12-31 || break
done
```

Mirror limits: no daily maxima, no wind gusts (`fg10_max` is CDS-only), and it
trails ~1 month behind the present — so the daily cron stays on CDS.

### Chunking

A Zarr read pulls whole chunks, so a sub-scene's full record costs
`days × chunk_lat × chunk_lon × 4` bytes — the time chunk does not enter into it.
So `spatial_chunk` in `config.yaml` (currently **32**) is the knob that decides
how cheap a small-area query is. It is a **cap**, not a size: a scene smaller
than it lands in a single chunk, which is what made ERA5 scenes (~85×89 px at
0.25°) unsplittable at the old value of 128.

Chunk shape is fixed when a store is created, so config changes only reach new
cubes. `tilegen rechunk` rewrites existing ones:

```bash
tilegen rechunk --dry-run              # what would change, and the cost
tilegen rechunk -d era5 -x peru        # one cube
tilegen rechunk -d era5                # a whole dataset
tilegen rechunk -d era5 -x peru --resume   # promote what an interrupted run left
```

It builds a copy beside the original and promotes it only after verifying no data
was lost, so an interrupted run leaves the cube intact plus a `{scene}.zarr.rechunk`
to resume from or delete. All-NaN stretches are skipped, keeping cubes sparse.

**Stop the cron first.** A rechunk and the daily update writing the same cube will
lose data — the write lock does not reach across processes.

## Automatic updates (cron)

`lag_days` in each dataset YAML declares the source's publication delay; with no
dates, `run` processes up to `today - lag_days`. A short backward range also
picks up days that were published late:

```cron
LOG=~/tile_generation_module/.work/cron.log
0  6 * * * tilegen run -d chirps -x peru -x bolivia -s $(date -d '-70 days' +\%F) >> $LOG 2>&1
30 6 * * * tilegen run -d era5   -x peru -x nino34  -s $(date -d '-16 days' +\%F) >> $LOG 2>&1
```

## Adding a dataset or scene

1. **Scene**: one entry in `config/scenes.yaml`, then regenerate the map with
   `python3 docs/make_scenes_map.py`.
2. **Daily GeoTIFFs over HTTP** (the common case): a `config/datasets/<name>.yaml`
   with `source: http_geotiff` and the URL template — no code. See `chirps.yaml`.
3. **Another API**: subclass `DataSource` in `tilegen/sources/` (implement
   `granules()` and `fetch()`, ~40 lines — see `era5.py`) and register it in
   `tilegen/sources/__init__.py`.

## Code layout

```
config -> tilegen/conf         convenience symlink (`config/...` paths still work)
tilegen/conf/config.yaml       bucket, zarr chunks, COG options
tilegen/conf/scenes.yaml       the scene map (21 named rectangles)
tilegen/conf/datasets/*.yaml   one YAML per dataset (URL/CDS, variables, dates, nodata)
tilegen/config.py              pydantic models + YAML loading
tilegen/sources/               per-source adapters (http_geotiff, era5_cds, era5_edh,
                               cfs_ncei, cfsr_gdex, cpc_psl)
tilegen/assets.py              normalizes any source to a DataArray (lat, lon, float32, NaN)
tilegen/zarrstore.py           the cube: creation, per-day writes, ledger, rechunk
tilegen/zarr_pipeline.py       plan -> fetch -> write (zarr format)
tilegen/grid.py                fixed MERIT-style tile grid (cog format)
tilegen/tiler.py               COG cutting (cog format)
tilegen/pipeline.py            plan -> fetch -> tile -> upload (cog format)
tilegen/s3io.py                S3: bucket, listings, uploads with retry
tilegen/cli.py                 tilegen commands
tests/                         grid, zarr writes, rechunk, EDH hourly aggregation,
                               CPC, ERA5 fetch window
```
