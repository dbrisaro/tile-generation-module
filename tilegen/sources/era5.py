"""ERA5 via the Copernicus CDS API (requires ~/.cdsapirc).

Uses the daily-statistics derived dataset so no local hourly aggregation is
needed. One CDS request per (variable, month) — the CDS queue penalizes many
small requests, so monthly batching is the sweet spot. Requests are
sequential (parallel_fetch = False) to be polite with the queue.

If the pipeline runs with a bbox, it sets ``self.area`` (tile-aligned) and
only that window is requested from CDS — much faster than global.

VENTANA COMPARTIDA (``cds_fetch_area`` en el YAML del dataset)
-------------------------------------------------------------
Con ``area`` por escena, un backfill largo hace un pedido por (variable, mes) y
**por escena**: 380 meses x 21 escenas = ~8.000 pedidos. La cola del CDS tarda
~20 min por pedido haga lo que haga, así que el costo está en la CANTIDAD de
pedidos y no en su tamaño — ningún paralelismo lo arregla (medido el 26-ago-2026
con fg10_max: 3 gránulos/hora, ~110 días proyectados).

Con ``cds_fetch_area`` se pide UNA ventana que cubre todas las escenas y el
recorte a cada escena se hace local, en ``asset_to_dataarray``. Son 380 pedidos
en vez de 8.000. El archivo se cachea por nombre (el tag lleva la ventana), así
que la primera escena lo baja y las otras 20 lo leen del disco. Para que exista
cuando llegan, ``shared_downloads`` le dice al pipeline que no lo borre.
"""
from __future__ import annotations

import logging
import shutil
import zipfile

from ..utils import month_groups
from .base import Asset, DataSource, Granule

log = logging.getLogger("tilegen.era5")


def _ensure_netcdf(path):
    """CDS sometimes wraps the NetCDF in a zip; unwrap it in place."""
    with open(path, "rb") as f:
        if f.read(2) != b"PK":
            return
    extracted = path.with_suffix(".extracted.nc")
    with zipfile.ZipFile(path) as z:
        member = next(n for n in z.namelist() if n.endswith(".nc"))
        with z.open(member) as src, open(extracted, "wb") as dst:
            shutil.copyfileobj(src, dst)
    path.unlink()
    extracted.rename(path)


class Era5CdsSource(DataSource):
    parallel_fetch = False
    area = None  # (minx, miny, maxx, maxy), set by the pipeline when using --bbox

    @property
    def fetch_area(self):
        """La ventana que se le pide al CDS: la común si está configurada."""
        shared = self.cfg.cds_fetch_area
        return tuple(shared) if shared else self.area

    @property
    def shared_downloads(self):
        return self.cfg.cds_fetch_area is not None

    def granules(self, variables, dates):
        out = []
        for v in variables:
            for (y, m), ds_ in sorted(month_groups(dates).items()):
                out.append(Granule(f"{self.cfg.name}:{v}:{y}-{m:02d}", v, sorted(ds_)))
        return out

    def fetch(self, granule):
        import cdsapi

        v = granule.variable
        vcfg = self.cfg.variables[v]
        y, m = granule.dates[0].year, granule.dates[0].month
        area = self.fetch_area
        # el tag lleva la ventana -> con cds_fetch_area todas las escenas apuntan
        # al MISMO archivo, así que la primera lo baja y el resto lo cachea.
        tag = "" if area is None else "_" + "_".join(str(int(x)) for x in area)
        target = self.workdir / f"era5_{v}_{y}{m:02d}{tag}.nc"
        if not target.exists():
            request = {
                "product_type": "reanalysis",
                "variable": [vcfg.cds_variable],
                "daily_statistic": vcfg.daily_statistic,
                "time_zone": "utc+00:00",
                "frequency": "1_hourly",
                "year": [str(y)],
                "month": [f"{m:02d}"],
                "day": [f"{d.day:02d}" for d in granule.dates],
            }
            if area is not None:
                minx, miny, maxx, maxy = area
                request["area"] = [maxy, minx, miny, maxx]  # N, W, S, E
            log.info("CDS request %s (may sit in the queue for a while)", granule.key)
            cdsapi.Client(quiet=True).retrieve(self.cfg.cds_dataset, request, str(target))
            _ensure_netcdf(target)
        return self._assets(granule, target)

    def _assets(self, granule, target):
        import xarray as xr

        wanted = set(granule.dates)
        assets = []
        with xr.open_dataset(target) as ds:
            tdim = "valid_time" if "valid_time" in ds else "time"
            for i, t in enumerate(ds[tdim].values):
                d = t.astype("datetime64[D]").astype(object)
                if d in wanted:
                    assets.append(Asset(variable=granule.variable, date=d,
                                        path=target, time_index=i))
        return assets
