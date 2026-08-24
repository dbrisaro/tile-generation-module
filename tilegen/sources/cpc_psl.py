"""CPC Global Daily Temperature via NOAA PSL (OPeNDAP).

NOAA PSL publishes CPC global tmax/tmin as one NetCDF per variable per year,
1979 to present, on a 0.5 deg grid. A granule here is one (variable, year).

Read over OPeNDAP, not plain HTTPS, and that choice is the whole point: the
THREDDS server applies the bbox before sending anything, so a year of one
scene is ~1.7 MB instead of the ~380 MB the global file weighs. Downloading
whole years would be 21 scenes x 48 years x 2 variables of mostly-discarded
ocean.

Grid quirks this source has to absorb (measured against the live files):

- longitude runs 0..360 (0.25 .. 359.75), not -180..180
- latitude DESCENDS (89.75 .. -89.75)
- CPC interpolates station data, so it has no ocean: a coastal scene comes
  back with a large NaN fraction, and that is the source being honest rather
  than a fetch going wrong.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict

from ..utils import nc_lock
from .base import Asset, DataSource, Granule

log = logging.getLogger("tilegen.cpc")


class CpcPslSource(DataSource):
    parallel_fetch = True
    area = None  # (minx, miny, maxx, maxy), set by the pipeline

    _ds_cache = {}
    _ds_lock = threading.Lock()

    def _open(self, url):
        import xarray as xr

        with self._ds_lock:
            if url not in self._ds_cache:
                log.info("opening CPC %s", url)
                with nc_lock:
                    self._ds_cache[url] = xr.open_dataset(url)
            return self._ds_cache[url]

    def _url(self, v: str, year: int) -> str:
        vcfg = self.cfg.variables[v]
        stem = getattr(vcfg, "source_file", None) or v
        return f"{self.cfg.base_url.rstrip('/')}/{stem}.{year}.nc"

    def _crop(self, da):
        """Same shape as the EDH crop: convert the bbox, never the data.

        Rewriting the coordinate would force a full-globe read before the
        subset, which is exactly what OPeNDAP is here to avoid.
        """
        if self.area is None:
            return da
        minx, miny, maxx, maxy = self.area
        lon = next(c for c in ("longitude", "lon") if c in da.coords)
        lat = next(c for c in ("latitude", "lat") if c in da.coords)
        if float(da[lon].max()) > 180:  # CPC is on 0..360
            if minx < 0 <= maxx:
                raise ValueError("bbox crossing lon 0 not supported for CPC")
            minx, maxx = minx % 360, maxx % 360
        lat_desc = float(da[lat][0]) > float(da[lat][-1])
        return da.sel({lon: slice(minx, maxx),
                       lat: slice(maxy, miny) if lat_desc else slice(miny, maxy)})

    def granules(self, variables, dates):
        out = []
        for v in variables:
            by_year = defaultdict(list)
            for d in dates:
                by_year[d.year].append(d)
            for y, ds_ in sorted(by_year.items()):
                out.append(Granule(f"{self.cfg.name}:{v}:{y}", v, sorted(ds_)))
        return out

    def _read_year(self, granule):
        import pandas as pd

        v = granule.variable
        year = granule.dates[0].year
        url = self._url(v, year)
        try:
            ds = self._open(url)
        except OSError as e:
            # A year the archive does not carry yet resolves to a 404 that
            # netCDF/DAP surfaces as a generic OSError.
            raise FileNotFoundError(f"{url} not available ({e})") from e

        stem = getattr(self.cfg.variables[v], "source_file", None) or v
        name = stem if stem in ds else next(iter(ds.data_vars))
        da = ds[name]
        tdim = next(d for d in ("valid_time", "time") if d in da.dims)
        tvals = pd.DatetimeIndex(ds[tdim].values)
        wanted = pd.DatetimeIndex([pd.Timestamp(d) for d in granule.dates])
        present = wanted.intersection(tvals)
        if present.empty:
            raise FileNotFoundError(
                f"dates not yet in CPC {year} file (ends {tvals[-1].date()})")
        da = self._crop(da.sel({tdim: present}))
        if da.sizes.get(next(c for c in ("longitude", "lon") if c in da.coords), 0) == 0:
            raise ValueError(f"bbox {self.area} falls outside the CPC grid")
        with nc_lock:
            da = da.load()
        log.info("CPC read %s (%d day(s), %s px)", granule.key,
                 da.sizes[tdim], "x".join(str(s) for s in da.shape[1:]))
        return da.rename({tdim: "time"}).astype("float32")

    def fetch(self, granule):
        import xarray as xr

        v = granule.variable
        y = granule.dates[0].year
        tag = "" if self.area is None else "_" + "_".join(str(int(x)) for x in self.area)
        target = self.workdir / f"cpc_{v}_{y}{tag}.nc"
        if not target.exists():
            da = self._read_year(granule)
            tmp = target.with_suffix(".part.nc")
            with nc_lock:
                da.to_netcdf(tmp)
            tmp.rename(target)

        wanted = set(granule.dates)
        assets = []
        with nc_lock, xr.open_dataset(target) as ds:
            for i, t in enumerate(ds["time"].values):
                d = t.astype("datetime64[D]").astype(object)
                if d in wanted:
                    assets.append(Asset(variable=v, date=d, path=target, time_index=i))
        return assets
