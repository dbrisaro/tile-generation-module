"""CFS operational analysis via NOAA NCEI (~0.2° global).

NCEI publishes the CFS operational analysis as monthly global GRIB2 time
series, one file per source field:

    {base_url}/{year}/{YYYYMM}/{source_file}.gdas.{YYYYMM}.grib2

The files hold 6-hourly analysis (plus short forecast steps); this source
keeps the analysis (step=0) and aggregates to a daily MEAN. A granule is one
(variable, month): the monthly global GRIB2 is downloaded once, the analysis
is cropped to the scene bbox BEFORE any compute (a global 0.2° field is
~5 GB/variable — cropping first keeps peak memory in the MBs, not GBs),
daily-averaged, and cached as a small per-month NetCDF; per-day Assets index
into it. The heavy GRIB is deleted once no requested variable still needs it.

Per-variable config: ``source_file`` (the NCEI field name, e.g. tmp2m,
wnd10m) and ``grib_variable`` (the variable inside that file, e.g. t2m, u10,
v10). One source file may carry several output variables — e.g. wnd10m
yields u10 and v10, stored separately as u10m and v10m (downloaded once).
"""
from __future__ import annotations

import logging
import subprocess
import threading
from collections import defaultdict

from ..utils import nc_lock
from .base import Asset, DataSource, Granule

log = logging.getLogger("tilegen.cfs")


class CfsNceiSource(DataSource):
    parallel_fetch = True
    area = None  # (minx, miny, maxx, maxy), set by the pipeline

    def __init__(self, cfg, workdir):
        super().__init__(cfg, workdir)
        self._grib_locks = {}            # one lock per GRIB file (dedup downloads)
        self._pending = defaultdict(set)  # (source_file,y,m) -> vars still to produce
        self._guard = threading.Lock()

    # -- helpers ---------------------------------------------------------
    def _prep(self, da):
        """step=0 (analysis), lon 0..360 -> -180..180, crop to bbox. Lazy."""
        if "step" in da.dims:
            da = da.isel(step=0)         # analysis, not forecast
        da = da.assign_coords(
            longitude=(((da.longitude + 180) % 360) - 180)).sortby("longitude")
        if self.area is not None:
            minx, miny, maxx, maxy = self.area
            da = da.sel(longitude=slice(minx, maxx),
                        latitude=slice(maxy, miny))   # latitude descending
        return da

    def _grib_lock(self, name):
        with self._guard:
            return self._grib_locks.setdefault(name, threading.Lock())

    def _download_grib(self, source_file, y, m):
        ym = f"{y}{m:02d}"
        dst = self.workdir / f"{source_file}.gdas.{ym}.grib2"
        with self._grib_lock(dst.name):          # sibling var reuses, never re-downloads
            if not dst.exists():
                url = f"{self.cfg.base_url}/{y}/{ym}/{source_file}.gdas.{ym}.grib2"
                tmp = dst.with_name(dst.name + ".part")
                log.info("downloading %s", url)
                r = subprocess.run(["curl", "-sS", "-f", "-o", str(tmp), url])
                if r.returncode != 0 or not tmp.exists():
                    tmp.unlink(missing_ok=True)
                    raise FileNotFoundError(url)
                tmp.rename(dst)
                log.info("downloaded %s (%.0f MB)", dst.name, dst.stat().st_size / 1e6)
        return dst

    def _release_grib(self, grib, v, source_file, y, m):
        """Mark this var's month done; drop the shared GRIB when no sibling needs it."""
        with self._guard:
            self._pending[(source_file, y, m)].discard(v)
            if not self._pending[(source_file, y, m)]:
                grib.unlink(missing_ok=True)

    # -- DataSource API --------------------------------------------------
    def granules(self, variables, dates):
        out = []
        for v in variables:
            source_file = self.cfg.variables[v].source_file
            by_month = defaultdict(list)
            for d in dates:
                by_month[(d.year, d.month)].append(d)
            for (y, m), ds_ in sorted(by_month.items()):
                self._pending[(source_file, y, m)].add(v)
                out.append(Granule(f"{self.cfg.name}:{v}:{y}-{m:02d}", v, sorted(ds_)))
        return out

    def fetch(self, granule):
        import xarray as xr

        v = granule.variable
        vcfg = self.cfg.variables[v]
        source_file, gvar = vcfg.source_file, vcfg.grib_variable
        y, m = granule.dates[0].year, granule.dates[0].month
        tag = "" if self.area is None else "_" + "_".join(str(int(x)) for x in self.area)
        target = self.workdir / f"cfs_{v}_{y}{m:02d}{tag}.nc"

        grib = self.workdir / f"{source_file}.gdas.{y}{m:02d}.grib2"
        if not target.exists():
            grib = self._download_grib(source_file, y, m)
            with nc_lock:  # cfgrib/eccodes C layer is not thread-safe
                ds = xr.open_dataset(grib, engine="cfgrib",
                                     backend_kwargs={"indexpath": ""},
                                     chunks={"time": 1})
                daily = (self._prep(ds[gvar]).resample(time="1D").mean()
                         .astype("float32").rename(v).load())
                ds.close()
            tmp = target.with_suffix(".part.nc")
            with nc_lock:
                daily.to_netcdf(tmp)
            tmp.rename(target)
        self._release_grib(grib, v, source_file, y, m)

        wanted = set(granule.dates)
        assets = []
        with nc_lock, xr.open_dataset(target) as ds:
            for i, t in enumerate(ds["time"].values):
                d = t.astype("datetime64[D]").astype(object)
                if d in wanted:
                    assets.append(Asset(variable=v, date=d, path=target, time_index=i))
        return assets
