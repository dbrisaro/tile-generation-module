# tile_generation_module (`tilegen`)

Data pipeline that downloads climate datasets (ERA5, CHIRPS, CHIRTS, CPC,
extensible to others) and publishes them to S3 as **per-scene Zarr cubes
optimized for long time series** (main format), or as per-date
**cloud-optimized GeoTIFF (COG)** mosaics (alternative format, for maps).

**Scenes** are a fixed map of named rectangles — one per country (small ones
grouped, large ones split into regions), extended offshore where there is
coastline, plus index regions such as Niño 3.4. They live in
`config/scenes.yaml` and are drawn in `docs/escenas_borrador.png` (regenerate
with `python3 docs/make_scenes_map.py`). The same scene serves land and ocean
variables: each dataset fills the pixels where it has data.

## The idea

One Zarr store per (dataset, scene): a single `time × lat × lon` cube holding
the dataset's full history, chunked internally at ~1 year × 128×128 pixels.
Reading 40 years for one point or one area fetches only the chunks it needs,
straight from S3:

```python
import xarray as xr

ds = xr.open_zarr("s3://suyana-tiles/chirps/v2.0/peru.zarr")
series = ds.precip.sel(latitude=-3.75, longitude=-73.25, method="nearest")
series.sel(time=slice("1981-01-01", "2025-12-31")).plot()   # ~1 read per year
```

How it stays current:

- The cube is created once with the dataset's **full time axis** (metadata
  only — it takes no space). Writing a day fills its slot, in any order.
  Unwritten days read as NaN.
- A **ledger** (one JSON per variable) records which days are written and which
  do not exist at the source. Re-runs skip straight to what is missing: **every
  run is idempotent and resumable**, and a daily cron keeps it up to date.
- **Why not Airflow/Kedro?** Everything runs on one machine, for one user.
  State lives in S3, cron does the orchestration. If an orchestrator is ever
  needed, the stages are already pure functions.

**Why keep our own copy?** Cloud analysis-ready copies of some of this data
already exist (Google's ARCO-ERA5, the DestinE mirror, CHIRPS in Earth Engine).
We recompute nothing — values pass through untouched, and we only download the
scenes and variables we use. The copy still pays for itself: one uniform layout
across all datasets (same bucket, same scenes, same dimensions, one
authentication); independence from third-party mirrors that require tokens,
change, or go down, which a payout calculation must not depend on;
reproducibility, since a parametric trigger computed today must recompute
identically in two years, and frozen versioned data (`chirps/v2.0/...`) gives
that while a live external service does not; and a trivial duplication cost —
a few GB of S3 per scene. The limit: if **global** coverage or many more
variables are ever needed, mirroring stops making sense and reading
ARCO-ERA5/EDH directly wins.

## S3 layout

```
s3://suyana-tiles/
  chirps/v2.0/peru.zarr/                    # cube: precip (time x lat x lon)
  chirps/v2.0/bolivia.zarr/                 # same dataset, another scene
  chirps/v2.0/_ledger/peru/precip.json      # which days are written
  era5/daily-stats/nino34.zarr/             # t2m_mean, t2m_max, precip_sum
  chirps/v2.0/precip/s20w080/*.tif          # (cog format, with --format cog)
```

21 scenes, defined in `config/scenes.yaml` — see `tilegen scenes` or the map in
`docs/`. Current coverage per dataset/variable/scene: `docs/cobertura.html`.

## Install

```bash
cd ~/tile_generation_module && pip install -e .           # development

# from another environment (no clone needed)
python -m venv .venv && . .venv/bin/activate
pip install "git+https://github.com/dbrisaro/tile-generation-module.git@v0.2.0"
```

Install into its own venv rather than `~/.local`: the `s3fs`/`boto3` lower
bounds are strict on purpose and can fight whatever is already in a shared
environment. The exact combination running in production is
`~/tilegen-prod/repo-requirements-lock.txt`.

**Reading the published cubes does not need tilegen** — `xarray` + `s3fs` is
enough: `xr.open_zarr("s3://suyana-tiles/era5/daily-stats/peru.zarr")`.

Credentials: `~/.aws` or an instance role for S3; `~/.cdsapirc` for ERA5.

## Usage

```bash
tilegen datasets                     # configured datasets
tilegen scenes                       # the scene map (numbered)
tilegen init-bucket                  # create the (private) bucket if missing

# see what it would do, downloading nothing
tilegen plan -d chirps -x peru -s 2024-01-01 -e 2024-12-31

# download and write to the cube (idempotent: only what is missing)
tilegen run -d chirps -x peru -s 2024-01-01 -e 2024-12-31
tilegen run -d era5 -v t2m_mean -x nino34 -s 2024-01-01 -e 2024-01-31
tilegen run -d chirps -x peru -x bolivia     # several scenes in one run
tilegen run -d chirps -x peru                # no dates = latest available day

# dry run without touching S3 (writes under .work/output/)
tilegen run -d chirps -x peru -s 2024-01-15 -e 2024-01-18 --local-only

# coverage and gaps
tilegen verify                     # catalog of everything in S3
tilegen verify -d chirps -x peru   # or scoped to one dataset/scene

# chunk layout
tilegen chunks                     # how each cube is chunked, and what a sub-scene read costs
tilegen chunks --stale             # only the cubes that predate the current config
```

Useful options: `--overwrite`, `--workers N` (parallel downloads), `--format
cog` (per-date GeoTIFF mosaic instead of a cube, with `--bbox` instead of
`-x/--scene`).

### Long backfills

Run in yearly blocks, chronologically — each run is short and resumable, so a
failure just means relaunching and continuing where it stopped:

```bash
for y in $(seq 1981 2025); do
  tilegen run -d chirps -x peru -s $y-01-01 -e $y-12-31 || break
done
```

For ERA5 the scene matters: only that spatial window is requested from
Copernicus. Requests go month by month and serially (its queue penalizes
parallelism).

**Fast ERA5, no queue (Earth Data Hub mirror)**: with `--source era5_edh`,
reads go straight to the DestinE Zarr mirror instead of the CDS queue — orders
of magnitude faster for backfills. Requires (1) an API key from
https://earthdatahub.destine.eu in `~/.netrc` (`machine
api.earthdatahub.destine.eu` / `password <key>`), and (2) the `tilegen` conda
environment (zarr>=3, Python 3.12): `conda activate tilegen`.

```bash
for y in $(seq 1981 2026); do
  tilegen run -d era5 --source era5_edh -x peru -s $y-01-01 -e $y-12-31 || break
done
```

Mirror limitations: no daily maxima (`t2m_max` still comes from CDS) and it
trails ~1 month behind the present (the daily cron update stays on CDS).

### Chunking

A Zarr read pulls whole chunks, so reading one pixel costs the entire chunk
holding it: a sub-scene's full record costs `days × chunk_lat × chunk_lon × 4`
bytes — note the time chunk does not enter into it. `spatial_chunk` in
`config.yaml` is therefore the knob that decides how cheap a small-area query
is. It is a **cap**, not a size: a scene smaller than it lands in a single
chunk, which is what made ERA5 scenes (~85×89 px at 0.25°) unsplittable at the
old value of 128.

Chunk shape is fixed when a store is created, so changing the config only
reaches new cubes. `tilegen rechunk` rewrites the existing ones:

```bash
tilegen rechunk --dry-run              # what would change, and the cost
tilegen rechunk -d era5 -x peru        # one cube
tilegen rechunk -d era5                # a whole dataset
tilegen rechunk -d era5 -x peru --resume   # promote a copy an interrupted run left
```

It builds a copy beside the original and promotes it only after verifying that
no data was lost, so an interrupted run leaves the cube intact plus a
`{scene}.zarr.rechunk` to resume from or delete. All-NaN stretches are skipped
rather than written, which keeps the cubes sparse.

**Stop the cron first.** A rechunk and the daily update writing the same cube
will lose data — the pipeline's write lock does not reach across processes.

## Automatic updates (cron)

`lag_days` in each YAML declares the source's publication delay; with no dates,
`run` processes up to `today - lag_days`. A short backward range also fills in
days published late:

```cron
0 6 * * * tilegen run -d chirps -x peru -x bolivia -s $(date -d '-70 days' +\%F) >> ~/tile_generation_module/.work/cron.log 2>&1
30 6 * * * tilegen run -d era5 -x peru -x nino34 -s $(date -d '-16 days' +\%F) >> ~/tile_generation_module/.work/cron.log 2>&1
```

## Adding a dataset or scene

1. **Scene**: one entry in `config/scenes.yaml` (then regenerate the map with
   `python3 docs/make_scenes_map.py`).
2. **Dataset published as daily GeoTIFFs over HTTP** (the common case): create
   `config/datasets/<name>.yaml` with `source: http_geotiff` and the URL
   template — no code. See `chirps.yaml`.
3. **Dataset with another API**: subclass `DataSource` in `tilegen/sources/`
   (implement `granules()` and `fetch()`, ~40 lines — see `era5.py`) and
   register it in `tilegen/sources/__init__.py`.

## Code layout

```
config -> tilegen/conf         convenience symlink (`config/...` paths still work)
tilegen/conf/config.yaml       bucket, zarr chunks, COG options
tilegen/conf/scenes.yaml       the scene map (21 named rectangles)
tilegen/conf/datasets/*.yaml   one YAML per dataset (URL/CDS, variables, dates, nodata)
tilegen/config.py              pydantic models + YAML loading
tilegen/sources/               per-source adapters (http_geotiff, era5_cds, era5_edh)
tilegen/assets.py              normalizes any source to a DataArray (lat, lon, float32, NaN)
tilegen/zarrstore.py           the cube: creation, per-day writes, ledger
tilegen/zarr_pipeline.py       plan -> fetch -> write (zarr format)
tilegen/grid.py                fixed MERIT-style tile grid (cog format)
tilegen/tiler.py               COG cutting (cog format)
tilegen/pipeline.py            plan -> fetch -> tile -> upload (cog format)
tilegen/s3io.py                S3: bucket, listings, uploads with retry
tilegen/cli.py                 tilegen commands
tests/                         grid, EDH hourly aggregation
```
