"""ERA5 via the Earth Data Hub Zarr mirror (DestinE, earthdatahub.destine.eu).

The mirror serves ERA5 as ready-to-read Zarr stores, so there is no request
queue: a granule here is one (variable, year) slice read directly over HTTPS.
Requires an EDH API key in ~/.netrc:

    machine api.earthdatahub.destine.eu
    password <API key>

Two stores are used, chosen per variable in the dataset YAML:

- ``edh_store`` (daily statistics): daily means of instantaneous variables
  only. Mapped with ``edh_variable`` (e.g. t2m_mean -> t2m).
- ``edh_hourly_store`` (hourly): full hourly fields, aggregated locally to
  daily values. Mapped with ``edh_hourly_variable`` + ``edh_hourly_statistic``
  ("max" or "sum"). Aggregation windows follow the CDS daily-statistics
  convention (validated against CDS output, diffs at packing precision):
  max over hours 00-23 UTC of day D; sums cover 01:00 D .. 00:00 D+1 UTC
  (ERA5 accumulations are stamped at the end of the hour). Days without all
  24 hours in the store are dropped rather than written incomplete.

Variables with neither mapping are skipped with a warning; fetch those via
the era5_cds source instead. The mirror lags behind real time by up to a
month (updated monthly), so near-real-time updates also stay on era5_cds.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict

from ..utils import nc_lock
from .base import Asset, DataSource, Granule

log = logging.getLogger("tilegen.edh")


class Era5EdhSource(DataSource):
    parallel_fetch = True
    area = None  # (minx, miny, maxx, maxy), set by the pipeline

    _ds_cache = {}
    _ds_lock = threading.Lock()

    def _open(self, url):
        import xarray as xr

        with self._ds_lock:
            if url not in self._ds_cache:
                log.info("opening EDH store %s", url)
                self._ds_cache[url] = xr.open_dataset(
                    url, engine="zarr", chunks={},
                    storage_options={"client_kwargs": {"trust_env": True}},
                )
            return self._ds_cache[url]

    def _mapping(self, v):
        """('daily', edh_var, None) | ('hourly', edh_var, stat) | None."""
        vcfg = self.cfg.variables[v]
        if getattr(vcfg, "edh_variable", None):
            return "daily", vcfg.edh_variable, None
        if getattr(vcfg, "edh_hourly_variable", None):
            stat = getattr(vcfg, "edh_hourly_statistic", None)
            if stat not in ("max", "sum", "mean"):
                raise ValueError(
                    f"{v}: edh_hourly_statistic must be 'max', 'sum' or 'mean'")
            return "hourly", vcfg.edh_hourly_variable, stat
        return None

    def _crop(self, da):
        if self.area is None:
            return da
        minx, miny, maxx, maxy = self.area
        lon = next(c for c in ("longitude", "lon") if c in da.coords)
        lat = next(c for c in ("latitude", "lat") if c in da.coords)
        if float(da[lon].max()) > 180:  # store uses 0..360
            if minx < 0 <= maxx:
                raise ValueError("bbox crossing lon 0 not supported for EDH")
            minx, maxx = minx % 360, maxx % 360
        lat_desc = float(da[lat][0]) > float(da[lat][-1])
        return da.sel({lon: slice(minx, maxx),
                       lat: slice(maxy, miny) if lat_desc else slice(miny, maxy)})

    def granules(self, variables, dates):
        out = []
        for v in variables:
            if self._mapping(v) is None:
                log.warning("%s has no edh_variable / edh_hourly_variable mapping "
                            "— skipping (fetch it with the era5_cds source)", v)
                continue
            by_year = defaultdict(list)
            for d in dates:
                by_year[d.year].append(d)
            for y, ds_ in sorted(by_year.items()):
                out.append(Granule(f"{self.cfg.name}:{v}:{y}", v, sorted(ds_)))
        return out

    def _read_daily(self, granule, evar):
        import pandas as pd

        ds = self._open(self.cfg.edh_store)
        if evar not in ds:
            raise FileNotFoundError(f"{evar} not in EDH store")
        da = ds[evar]
        tdim = next(d for d in ("valid_time", "time") if d in da.dims)
        tvals = pd.DatetimeIndex(ds[tdim].values)
        wanted = pd.DatetimeIndex([pd.Timestamp(d) for d in granule.dates])
        present = wanted.intersection(tvals)
        if present.empty:
            raise FileNotFoundError(f"dates not yet in EDH store (ends {tvals[-1].date()})")
        da = self._crop(da.sel({tdim: present}))
        log.info("EDH read %s (%d day(s), %s px)", granule.key,
                 da.sizes[tdim], "x".join(str(s) for s in da.shape[1:]))
        return da.rename({tdim: "time"}).astype("float32")

    def _read_hourly(self, granule, evar, stat):
        import pandas as pd

        ds = self._open(self.cfg.edh_hourly_store)
        if evar not in ds:
            raise FileNotFoundError(f"{evar} not in EDH hourly store")
        da = ds[evar]
        tdim = next(d for d in ("valid_time", "time") if d in da.dims)
        start = pd.Timestamp(granule.dates[0])
        end = pd.Timestamp(granule.dates[-1])
        if stat == "sum":   # day D = hours 01:00 D .. 00:00 D+1 (end-of-hour stamps)
            tsel = slice(start + pd.Timedelta(hours=1), end + pd.Timedelta(days=1))
        else:               # day D = hours 00:00 .. 23:00 of D
            tsel = slice(start, end + pd.Timedelta(hours=23))
        da = self._crop(da.sel({tdim: tsel}))
        if da.sizes[tdim] == 0:
            tvals = pd.DatetimeIndex(ds[tdim].values)
            raise FileNotFoundError(
                f"dates not yet in EDH hourly store (ends {tvals[-1]})")
        log.info("EDH hourly read %s (%d hour(s), %s px)", granule.key,
                 da.sizes[tdim], "x".join(str(s) for s in da.shape[1:]))
        with nc_lock:  # la capa C de netCDF/HDF5 no es thread-safe
            da = da.load()
        t = pd.DatetimeIndex(da[tdim].values)
        label = (t - pd.Timedelta(hours=1)).floor("D") if stat == "sum" else t.floor("D")
        da = da.assign_coords({tdim: label}).rename({tdim: "time"})
        if stat == "max":
            agg = da.groupby("time").max()
        elif stat == "mean":
            agg = da.groupby("time").mean()
        else:  # sum (acumulados: ventana 01:00 D .. 00:00 D+1, ver arriba)
            agg = da.groupby("time").sum()
        full = [d for d, n in label.value_counts().items() if n == 24]
        if len(full) < agg.sizes["time"]:
            log.warning("%s: dropping %d day(s) without all 24 hours in the store",
                        granule.key, agg.sizes["time"] - len(full))
        return agg.sel(time=sorted(full)).astype("float32")

    def fetch(self, granule):
        v = granule.variable
        kind, evar, stat = self._mapping(v)
        y = granule.dates[0].year
        tag = "" if self.area is None else "_" + "_".join(str(int(x)) for x in self.area)
        target = self.workdir / f"edh_{v}_{y}{tag}.nc"
        if not target.exists():
            if kind == "daily":
                da = self._read_daily(granule, evar)
            else:
                da = self._read_hourly(granule, evar, stat)
            tmp = target.with_suffix(".part.nc")
            with nc_lock:
                da.to_netcdf(tmp)
            tmp.rename(target)

        import xarray as xr

        wanted = set(granule.dates)
        assets = []
        with nc_lock, xr.open_dataset(target) as ds:
            for i, t in enumerate(ds["time"].values):
                d = t.astype("datetime64[D]").astype(object)
                if d in wanted:
                    assets.append(Asset(variable=v, date=d, path=target, time_index=i))
        return assets
