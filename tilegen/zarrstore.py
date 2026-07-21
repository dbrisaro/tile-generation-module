"""One Zarr store per (dataset, scene), laid out for long time series.

The store is created once with the *full* daily time axis of the dataset
(metadata only — no data chunks are written until real data arrives, and
chunks that were never written read back as NaN). Writing a date is then an
idempotent region-write at a fixed position, in any order. A small ledger
JSON per variable records which dates are filled and which are known to be
missing at the source, so re-runs skip straight to what is pending.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path

import fsspec
import numpy as np
import pandas as pd
import xarray as xr

log = logging.getLogger("tilegen.zarr")

DIMS = ("time", "latitude", "longitude")


def _v2_kwargs() -> dict:
    """Pin new stores to Zarr format v2 so every environment (zarr 2.x and
    3.x) can read and write them; no-op where zarr 2.x is the writer."""
    import zarr
    return {"zarr_format": 2} if int(zarr.__version__.split(".")[0]) >= 3 else {}


class ZarrStore:
    def __init__(self, gcfg, dcfg, scene: str, local_only: bool, out_dir: Path):
        self.gcfg, self.dcfg, self.scene = gcfg, dcfg, scene
        rel = f"{dcfg.name}/{dcfg.version}"
        if local_only:
            base = f"{out_dir}/{rel}"
        else:
            prefix = gcfg.s3.prefix.strip("/")
            base = f"s3://{gcfg.s3.bucket}" + (f"/{prefix}" if prefix else "") + f"/{rel}"
        self.uri = f"{base}/{scene}.zarr"
        self.ledger_base = f"{base}/_ledger/{scene}"
        self.fs = fsspec.get_mapper(self.uri).fs
        self._grid = None  # (lat, lon) cached on first align; never changes

    # --- store ------------------------------------------------------------
    def mapper(self):
        # local paths need auto_mkdir under zarr 3 (its fsspec store no longer
        # creates parent directories on write; the s3 mapper is unaffected)
        kwargs = {} if self.uri.startswith("s3://") else {"auto_mkdir": True}
        return fsspec.get_mapper(self.uri, **kwargs)

    def exists(self) -> bool:
        return self.fs.exists(f"{self.uri}/.zgroup")

    def _chunks(self, ny: int, nx: int) -> tuple[int, int, int]:
        z = self.gcfg.zarr
        override = getattr(self.dcfg, "chunks", None) or {}
        return (int(override.get("time", z.time_chunk)),
                min(ny, int(override.get("lat", z.spatial_chunk))),
                min(nx, int(override.get("lon", z.spatial_chunk))))

    def create(self, template: xr.DataArray) -> None:
        """Create the store (metadata + coords only) from a sample day."""
        import dask.array as dsa

        start = self.dcfg.start
        end = self.dcfg.end or (dt.date.today() + dt.timedelta(days=365))
        times = pd.date_range(start, end, freq="D")
        ny, nx = template.sizes["latitude"], template.sizes["longitude"]
        chunks = self._chunks(ny, nx)
        data_vars = {}
        encoding = {}
        for v, vcfg in self.dcfg.variables.items():
            data_vars[v] = (DIMS, dsa.full((len(times), ny, nx), np.nan,
                                           chunks=chunks, dtype="float32"),
                            {"units": vcfg.units or ""})
            encoding[v] = {"_FillValue": np.float32(np.nan)}
        ds = xr.Dataset(
            data_vars,
            coords={"time": times,
                    "latitude": template["latitude"].values,
                    "longitude": template["longitude"].values},
            attrs={"dataset": self.dcfg.name, "version": self.dcfg.version,
                   "scene": self.scene, "description": self.dcfg.description,
                   "created_by": "tilegen"},
        )
        ds.to_zarr(self.mapper(), compute=False, encoding=encoding, consolidated=True,
                   **_v2_kwargs())
        log.info("created %s  (%d days x %d x %d, chunks %s)",
                 self.uri, len(times), ny, nx, chunks)

    def ensure_covers(self, end_date: dt.date) -> None:
        """Extend the time axis (with unwritten NaN) if a date falls beyond it."""
        import dask.array as dsa

        with xr.open_zarr(self.mapper(), consolidated=True) as ds:
            last = pd.Timestamp(ds.time.values[-1]).date()
            if end_date <= last:
                return
            times = pd.date_range(last + dt.timedelta(days=1),
                                  end_date + dt.timedelta(days=365), freq="D")
            ny, nx = ds.sizes["latitude"], ds.sizes["longitude"]
            chunks = self._chunks(ny, nx)
            ext = xr.Dataset(
                {v: (DIMS, dsa.full((len(times), ny, nx), np.nan,
                                    chunks=chunks, dtype="float32"))
                 for v in ds.data_vars},
                coords={"time": times, "latitude": ds["latitude"].values,
                        "longitude": ds["longitude"].values},
            )
        ext.to_zarr(self.mapper(), append_dim="time", consolidated=True, **_v2_kwargs())
        log.info("extended %s through %s", self.uri, times[-1].date())

    def write(self, da: xr.DataArray, variable: str, d: dt.date) -> None:
        """Idempotent region-write of one day of one variable."""
        self.write_batch(da.expand_dims(time=[np.datetime64(d, "ns")]), variable)

    def write_batch(self, da: xr.DataArray, variable: str) -> None:
        """Idempotent region-write of many days of one variable at once.

        `da` carries a ``time`` dim whose values all fall on the store's axis.
        A region write only fills pre-allocated chunks — it never changes the
        store's structure — so the consolidated index is left untouched;
        re-consolidating on every write is what made the day-by-day path slow.
        ``region="auto"`` needs a contiguous slice along time, so the days are
        split into consecutive-date runs and each run is written in one call.
        """
        da = da.sortby("time")
        times = pd.DatetimeIndex(da["time"].values)
        gaps = np.diff(times.values).astype("timedelta64[D]") > np.timedelta64(1, "D")
        breaks = np.flatnonzero(gaps) + 1
        for run in np.split(np.arange(times.size), breaks):
            (da.isel(time=run).to_dataset(name=variable)
               .to_zarr(self.mapper(), region="auto", consolidated=False))

    def align(self, da: xr.DataArray) -> xr.DataArray:
        """Check the asset grid matches the store grid; snap coords exactly."""
        if self._grid is None:
            with xr.open_zarr(self.mapper(), consolidated=True) as ds:
                self._grid = (ds["latitude"].values, ds["longitude"].values)
        lat, lon = self._grid
        if da.sizes["latitude"] != lat.size or da.sizes["longitude"] != lon.size \
                or not np.allclose(da["latitude"], lat, atol=1e-6) \
                or not np.allclose(da["longitude"], lon, atol=1e-6):
            raise ValueError(f"asset grid does not match store grid {self.uri}")
        return da.assign_coords(latitude=lat, longitude=lon)

    # --- ledger -----------------------------------------------------------
    def _ledger_path(self, variable: str) -> str:
        return f"{self.ledger_base}/{variable}.json"

    def read_ledger(self, variable: str) -> dict:
        # single-request read + retries: a ranged/cached read can die mid-way
        # (PreconditionFailed / FileExpired) if a running pipeline rewrites
        # the ledger at that exact moment
        path = self._ledger_path(variable)
        for attempt in range(3):
            try:
                return json.loads(self.fs.cat(path))
            except FileNotFoundError:
                return {"written": [], "missing": []}
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))

    def update_ledger(self, variable: str, written=(), missing=()) -> None:
        led = self.read_ledger(variable)
        led["written"] = sorted(set(led["written"]) | {str(d) for d in written})
        led["missing"] = sorted((set(led["missing"]) | {str(d) for d in missing})
                                - set(led["written"]))
        with fsspec.open(self._ledger_path(variable), "w") as f:
            json.dump(led, f)
