"""CFSR (Climate Forecast System Reanalysis) hourly time series via NCAR GDEX.

The reanalysis proper covers 1979-01 to 2010-12 and is the historical companion
to the CFSv2 operational analysis (``cfs``, 2011+). GDEX serves it as monthly
single-field GRIB2, one file per field, with no authentication:

    {base_url}/{year}/{source_file}.gdas.{YYYYMM}.grb2

Same naming as the NCEI operational-analysis time series, so this subclasses
``CfsNceiSource`` and only replaces the URL layout and the daily aggregation.
Two things differ from the operational product and neither is cosmetic:

- **Grid.** CFSR is 1152x576 (~0.3125deg gaussian); the operational analysis is
  1760x880 (~0.2045deg). They cannot share a Zarr store, hence a separate
  dataset (``cfsr``) rather than extending ``cfs`` backwards.
- **No analysis step.** The operational files carry ``step`` 0..6 h, so step=0
  is the analysis. CFSR files carry ``step`` 1..6 h only — there is no step 0,
  and ``isel(step=0)`` would silently pick the +1 h forecast (4 samples/day at
  01/07/13/19 UTC) instead. So the daily value here is the mean over the full
  24 hours: the 4 cycles/day x 6 forecast hours tile the month exactly
  (124 x 6 = 744 h in a 31-day month), no hour duplicated or missing.

Days are labelled by the same window the ERA5 sums use (see sources/edh.py):
01:00 D .. 00:00 D+1 UTC. That keeps every month self-contained — day 1 is
complete without also downloading the previous month, whose last cycle would
otherwise be needed for hour 00:00.

A month of one field is ~400-550 MB and holds 744 global fields; decoding is
the expensive part, so all requested variables sharing a file (u10m and v10m
both live in wnd10m) are decoded in a single pass over it.
"""
from __future__ import annotations

import logging
import threading

from ..utils import nc_lock
from .base import Asset
from .cfs import CfsNceiSource

log = logging.getLogger("tilegen.cfsr")

HOURS_PER_DAY = 24


class CfsrGdexSource(CfsNceiSource):
    grib_suffix = "grb2"

    def __init__(self, cfg, workdir):
        super().__init__(cfg, workdir)
        self._wanted: set[str] = set()     # variables this run actually asked for
        self._decode_locks = {}            # one lock per (source_file, y, m)

    def _grib_url(self, source_file, y, m):
        """GDEX is flat under the year — no YYYYMM directory level."""
        return f"{self.cfg.base_url}/{y}/{self._grib_name(source_file, y, m)}"

    def granules(self, variables, dates):
        # The pipeline calls this once per variable; remember the union so a
        # decode pass knows which siblings are worth producing.
        self._wanted.update(variables)
        return super().granules(variables, dates)

    def _decode_lock(self, source_file, y, m):
        with self._guard:
            return self._decode_locks.setdefault((source_file, y, m), threading.Lock())

    def _siblings(self, source_file):
        """Requested variables that live in this GRIB file."""
        return [v for v in self._wanted
                if self.cfg.variables[v].source_file == source_file]

    # -- aggregation -----------------------------------------------------
    def _daily(self, da):
        """(time cycles, step 1..6 h) -> daily mean over the 24 hourly values.

        Flattens to a real hourly axis via valid_time = time + step, then
        averages per day using the 01:00 D .. 00:00 D+1 UTC window. Any day not
        carrying its full 24 hours is dropped rather than written as a partial
        mean (should only ever happen if the source file itself is short).
        """
        import numpy as np
        import pandas as pd
        import xarray as xr

        da = da.transpose("time", "step", "latitude", "longitude")
        vt = pd.DatetimeIndex(
            (da["time"].values[:, None] + da["step"].values[None, :]).ravel())
        flat = da.data.reshape((vt.size,) + da.shape[2:])
        hourly = xr.DataArray(
            flat, dims=("time", "latitude", "longitude"),
            coords={"time": vt, "latitude": da["latitude"], "longitude": da["longitude"]},
        ).sortby("time")

        label = (pd.DatetimeIndex(hourly["time"].values)
                 - pd.Timedelta(hours=1)).floor("D")
        hourly = hourly.assign_coords(time=label)
        daily = hourly.groupby("time").mean()
        full = sorted(d for d, n in label.value_counts().items() if n == HOURS_PER_DAY)
        if len(full) < daily.sizes["time"]:
            log.warning("dropping %d day(s) without all %d hours",
                         daily.sizes["time"] - len(full), HOURS_PER_DAY)
        return daily.sel(time=full).astype("float32")

    def _decode(self, grib, source_file, y, m):
        """Decode the month once, writing a daily NetCDF per requested sibling."""
        import xarray as xr

        todo = [(v, self._nc_target(v, y, m)) for v in self._siblings(source_file)]
        todo = [(v, t) for v, t in todo if not t.exists()]
        if not todo:
            return
        log.info("decoding %s -> %s", grib.name, ", ".join(v for v, _ in todo))
        with nc_lock:  # cfgrib/eccodes C layer is not thread-safe
            ds = xr.open_dataset(grib, engine="cfgrib",
                                 backend_kwargs={"indexpath": ""},
                                 chunks={"time": 1})
            try:
                for v, target in todo:
                    gvar = self.cfg.variables[v].grib_variable
                    # crop BEFORE computing: a global 0.31deg hourly month is
                    # ~2 GB per field, a scene window is a few MB
                    daily = self._daily(self._crop_lonlat(ds[gvar])).rename(v).load()
                    tmp = target.with_suffix(".part.nc")
                    daily.to_netcdf(tmp)
                    tmp.rename(target)
            finally:
                ds.close()

    # -- DataSource API --------------------------------------------------
    def fetch(self, granule):
        import xarray as xr

        v = granule.variable
        vcfg = self.cfg.variables[v]
        source_file = vcfg.source_file
        y, m = granule.dates[0].year, granule.dates[0].month
        target = self._nc_target(v, y, m)

        grib = self.workdir / self._grib_name(source_file, y, m)
        if not target.exists():
            grib = self._download_grib(source_file, y, m)
            # siblings share one decode; whoever gets here second finds its
            # NetCDF already written and falls through
            with self._decode_lock(source_file, y, m):
                self._decode(grib, source_file, y, m)
        self._release_grib(grib, v, source_file, y, m)

        wanted = set(granule.dates)
        assets = []
        with nc_lock, xr.open_dataset(target) as ds:
            for i, t in enumerate(ds["time"].values):
                d = t.astype("datetime64[D]").astype(object)
                if d in wanted:
                    assets.append(Asset(variable=v, date=d, path=target, time_index=i))
        return assets
