"""Pipeline for Zarr output: plan (ledger) -> fetch -> standardize -> region-write.

Downloads run in parallel (where the source allows it); writes to the store
are serialized with a lock because two dates in the same year share chunks.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import xarray as xr

from .assets import asset_to_dataarray
from .config import DatasetCfg, GlobalCfg
from .sources import SOURCES
from .sources.base import Granule
from .utils import daterange
from .zarrstore import ZarrStore

log = logging.getLogger("tilegen.pipeline")


@dataclass
class ZarrPlan:
    variables: list[str]
    start: dt.date
    end: dt.date
    missing: dict[str, list]     # variable -> dates to produce
    granules: list[Granule]

    @property
    def summary(self) -> str:
        days = (self.end - self.start).days + 1
        todo = sum(len(v) for v in self.missing.values())
        return (f"{len(self.variables)} variable(s) x {days} day(s): "
                f"{todo} day(s) to write, {len(self.granules)} granule(s) to fetch")


class ZarrPipeline:
    def __init__(self, gcfg: GlobalCfg, dcfg: DatasetCfg, scene: str,
                 bbox, local_only: bool = False, overwrite: bool = False,
                 workers: int | None = None, keep_local: bool | None = None,
                 retry_missing: bool = False):
        self.gcfg, self.dcfg, self.scene = gcfg, dcfg, scene
        self.bbox = tuple(bbox)
        self.overwrite = overwrite
        self.retry_missing = retry_missing
        self.workers = workers or gcfg.runtime.workers
        self.keep_local = gcfg.runtime.keep_local if keep_local is None else keep_local
        self.workdir = Path(gcfg.runtime.workdir)
        self.store = ZarrStore(gcfg, dcfg, scene, local_only, self.workdir / "output")
        self.source = SOURCES[dcfg.source](dcfg, self.workdir / "downloads" / dcfg.name)
        if hasattr(self.source, "area"):
            self.source.area = self.bbox
        self._wlock = threading.Lock()

    def _horizonte(self, variable: str) -> int:
        """Días que tienen que pasar antes de dar una ausencia por definitiva.

        Override por variable > valor del dataset > lag_days + 30. Ver el
        comentario de `missing_after_days` en config.py.
        """
        vcfg = self.dcfg.variables[variable]
        for valor in (getattr(vcfg, "missing_after_days", None),
                      self.dcfg.missing_after_days):
            if valor is not None:
                return valor
        return self.dcfg.lag_days + 30

    def _ausencias_definitivas(self, variable: str, dates) -> list:
        """De los días que la fuente no dio, los que ya se pueden dar por perdidos.

        Un día reciente que no está puede ser simplemente la fuente yendo
        atrasada. Anotarlo como definitivo lo congela: `plan()` lo saltea para
        siempre y no lo levanta ni cuando la fuente lo publica.
        """
        dates = list(dates)
        h = self._horizonte(variable)
        if h < 0:                       # la fuente viene tan atrasada que nada es definitivo
            firmes = []
        else:
            corte = dt.date.today() - dt.timedelta(days=h)
            firmes = [d for d in dates if d <= corte]
        provisorias = len(dates) - len(firmes)
        if provisorias:
            log.info("%s: %d día(s) sin publicar todavía, se vuelven a pedir en la "
                     "próxima corrida (horizonte %s)", variable, provisorias,
                     "ninguno: nunca se dan por definitivos" if h < 0 else f"{h} días")
        return firmes

    def clamp_dates(self, start, end):
        last = dt.date.today() - dt.timedelta(days=self.dcfg.lag_days)
        if self.dcfg.end:
            last = min(last, self.dcfg.end)
        end = min(end or last, last)
        start = max(start or end, self.dcfg.start)
        if start > end:
            raise ValueError(f"empty date range after clamping: {start} > {end}")
        return start, end

    def plan(self, variables=None, start=None, end=None) -> ZarrPlan:
        variables = list(variables) if variables else list(self.dcfg.variables)
        unknown = set(variables) - set(self.dcfg.variables)
        if unknown:
            raise ValueError(f"unknown variable(s) {unknown} — "
                             f"dataset has: {', '.join(self.dcfg.variables)}")
        start, end = self.clamp_dates(start, end)
        dates = list(daterange(start, end))
        missing, granules = {}, []
        for v in variables:
            if self.overwrite:
                skip = set()
            else:
                led = self.store.read_ledger(v)
                # `missing` es "la fuente no lo tiene". Con --retry-missing se
                # vuelve a preguntar: sirve cuando la fuente se puso al día, o
                # cuando la marca quedó de una clasificación equivocada.
                skip = set(led["written"])
                if not self.retry_missing:
                    skip |= set(led["missing"])
            pending = [d for d in dates if str(d) not in skip]
            missing[v] = pending
            if pending:
                granules += self.source.granules([v], pending)
        return ZarrPlan(variables, start, end, missing, granules)

    def run(self, plan: ZarrPlan) -> dict:
        t0 = time.time()
        stats = {"written": 0, "missing_at_source": 0, "errors": 0}
        errors: list[str] = []
        lock = threading.Lock()

        with self._wlock:
            if not self.store.exists():
                self._create_store(plan)
            else:
                # the store may predate a variable added to the YAML since
                self.store.ensure_variables(plan.variables)
            self.store.ensure_covers(plan.end)

        def process(granule: Granule):
            try:
                try:
                    assets = self.source.fetch(granule)
                except FileNotFoundError as exc:
                    self.store.update_ledger(
                        granule.variable,
                        missing=self._ausencias_definitivas(granule.variable,
                                                            granule.dates))
                    with lock:
                        stats["missing_at_source"] += len(granule.dates)
                    log.warning("not available at source: %s (%s)", granule.key, exc)
                    return
                written, das = [], []
                for asset in assets:
                    da = asset_to_dataarray(asset, self.dcfg, self.bbox)
                    das.append(da.expand_dims(time=[np.datetime64(asset.date, "ns")]))
                    written.append(asset.date)
                if das:
                    batch = xr.concat(das, dim="time")
                    with self._wlock:
                        batch = self.store.align(batch)
                        self.store.write_batch(batch, granule.variable)
                fetched = {a.date for a in assets}
                self.store.update_ledger(
                    granule.variable, written=written,
                    missing=self._ausencias_definitivas(
                        granule.variable, set(granule.dates) - fetched))
                # con shared_downloads el archivo cubre TODAS las escenas: borrarlo
                # acá obligaría a la escena siguiente a volver a pedirlo al CDS,
                # que es justamente el costo que la ventana común viene a evitar.
                if not self.keep_local and not self.source.shared_downloads:
                    for p in {a.path for a in assets}:
                        p.unlink(missing_ok=True)
                with lock:
                    stats["written"] += len(written)
                log.info("done %s (%d day(s))", granule.key, len(written))
            except Exception as exc:
                with lock:
                    stats["errors"] += 1
                    errors.append(f"{granule.key}: {exc}")
                log.error("failed %s: %s", granule.key, exc)

        max_workers = self.workers if self.source.parallel_fetch else 1
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(process, plan.granules))

        summary = {
            "dataset": self.dcfg.name, "scene": self.scene,
            "store": self.store.uri,
            "variables": plan.variables,
            "start": str(plan.start), "end": str(plan.end),
            "stats": stats, "errors": errors[:20],
            "duration_s": round(time.time() - t0, 1),
            "finished": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        return summary

    def _create_store(self, plan: ZarrPlan):
        """Fetch one sample granule to learn the grid, then create the store."""
        sample = plan.granules[0]
        log.info("initializing store from %s ...", sample.key)
        assets = self.source.fetch(sample)
        template = asset_to_dataarray(assets[0], self.dcfg, self.bbox)
        self.store.create(template)
