"""Orchestration: plan (what is missing) -> fetch -> tile -> upload.

There is no external state store: the S3 archive itself is the state. An
output key that already exists is skipped, so any run can be interrupted
and re-launched safely (idempotent), and a daily cron run just fills in
whatever is new.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .config import DatasetCfg, GlobalCfg
from .grid import Tile, TileGrid
from .s3io import S3Store
from .sources import SOURCES
from .sources.base import Granule
from .tiler import tile_asset
from .utils import daterange

log = logging.getLogger("tilegen.pipeline")


@dataclass
class Plan:
    variables: list[str]
    start: dt.date
    end: dt.date
    tiles: list[Tile]
    granules: list[Granule]
    n_expected: int   # upper bound: variables x dates x tiles (ocean tiles are skipped later)
    n_existing: int   # objects already under the variable prefixes

    @property
    def summary(self) -> str:
        days = (self.end - self.start).days + 1
        return (f"{len(self.variables)} variable(s) x {days} day(s) x {len(self.tiles)} tile(s) "
                f"= {self.n_expected} tiles max; {self.n_existing} objects already present; "
                f"{len(self.granules)} granule(s) to fetch")


class Pipeline:
    def __init__(self, gcfg: GlobalCfg, dcfg: DatasetCfg, bbox=None,
                 local_only: bool = False, overwrite: bool = False,
                 workers: int | None = None, keep_local: bool | None = None):
        self.gcfg, self.dcfg = gcfg, dcfg
        self.grid = TileGrid(dcfg.tile_size_deg or gcfg.grid.tile_size_deg)
        self.bbox = self.grid.snap_bbox(bbox) if bbox else None
        self.tiles = (self.grid.tiles_for_bbox(*self.bbox) if self.bbox
                      else self.grid.all_tiles())
        self.local_only = local_only
        self.overwrite = overwrite
        self.workers = workers or gcfg.runtime.workers
        self.keep_local = gcfg.runtime.keep_local if keep_local is None else keep_local
        self.workdir = Path(gcfg.runtime.workdir)
        self.out_dir = self.workdir / "output"      # destination in --local-only mode
        self.staging = self.workdir / "staging"     # tiles waiting for upload
        self.store = None if local_only else S3Store(gcfg.s3.bucket, gcfg.s3.region,
                                                     gcfg.s3.prefix)
        self.source = SOURCES[dcfg.source](dcfg, self.workdir / "downloads" / dcfg.name)
        if self.bbox and hasattr(self.source, "area"):
            self.source.area = self.bbox
        self._existing: set[str] = set()
        self._empty: dict[tuple, set[str]] = {}  # (variable, date) -> empty tile ids

    # --- keys -----------------------------------------------------------
    def key(self, variable: str, d: dt.date, tile: Tile) -> str:
        return (f"{self.dcfg.name}/{self.dcfg.version}/{variable}/{tile.id}/"
                f"{self.dcfg.name}_{variable}_{d:%Y%m%d}_{tile.id}.tif")

    def _have(self, variable: str, d: dt.date, tile: Tile) -> bool:
        """True if this tile is already in the archive or known to be empty."""
        return (self.key(variable, d, tile) in self._existing
                or tile.id in self._empty.get((variable, d), ()))

    def var_prefix(self, variable: str) -> str:
        return f"{self.dcfg.name}/{self.dcfg.version}/{variable}/"

    def _existing_keys(self, variables) -> set[str]:
        if self.overwrite:
            return set()
        if self.local_only:
            if not self.out_dir.exists():
                return set()
            return {str(p.relative_to(self.out_dir)) for p in self.out_dir.rglob("*.tif")}
        existing: set[str] = set()
        for v in variables:
            existing |= set(self.store.list_keys(self.var_prefix(v)))
        return existing

    # --- empty-tile markers ----------------------------------------------
    # A tile with no data (open ocean, outside the product's extent) is never
    # uploaded. Without a record of that, every re-run would re-download the
    # granule just to rediscover it. A small JSON per (variable, date) under
    # _empty/ lists the tile ids known to be empty.
    def _empty_key(self, variable: str, d: dt.date) -> str:
        return f"{self.dcfg.name}/{self.dcfg.version}/_empty/{variable}/{d:%Y%m%d}.json"

    def _load_empty(self, variables, dates) -> dict[tuple, set[str]]:
        out: dict[tuple, set[str]] = {}
        if self.overwrite:
            return out
        if self.local_only:
            for v in variables:
                for d in dates:
                    p = self.out_dir / self._empty_key(v, d)
                    if p.exists():
                        out[(v, d)] = set(json.loads(p.read_text()))
            return out
        for v in variables:
            have = set(self.store.list_keys(f"{self.dcfg.name}/{self.dcfg.version}/_empty/{v}/"))
            for d in dates:
                k = self._empty_key(v, d)
                if k in have:
                    out[(v, d)] = set(self.store.get_json(k) or [])
        return out

    def _save_empty(self, variable: str, d: dt.date, ids: set[str]) -> None:
        if not ids:
            return
        merged = sorted(self._empty.get((variable, d), set()) | ids)
        key = self._empty_key(variable, d)
        if self.local_only:
            p = self.out_dir / key
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(merged))
        else:
            self.store.put_json(key, merged)
        self._empty[(variable, d)] = set(merged)

    def clamp_dates(self, start, end):
        """Clamp the requested range to what the dataset can actually provide."""
        last = dt.date.today() - dt.timedelta(days=self.dcfg.lag_days)
        if self.dcfg.end:
            last = min(last, self.dcfg.end)
        end = min(end or last, last)
        start = max(start or end, self.dcfg.start)
        if start > end:
            raise ValueError(f"empty date range after clamping: {start} > {end}")
        return start, end

    # --- plan -----------------------------------------------------------
    def plan(self, variables=None, start=None, end=None) -> Plan:
        variables = list(variables) if variables else list(self.dcfg.variables)
        unknown = set(variables) - set(self.dcfg.variables)
        if unknown:
            raise ValueError(f"unknown variable(s) {unknown} — "
                             f"dataset has: {', '.join(self.dcfg.variables)}")
        start, end = self.clamp_dates(start, end)
        dates = list(daterange(start, end))
        self._existing = self._existing_keys(variables)
        self._empty = self._load_empty(variables, dates)
        granules: list[Granule] = []
        for v in variables:
            pending = [d for d in dates
                       if any(not self._have(v, d, t) for t in self.tiles)]
            if pending:
                granules += self.source.granules([v], pending)
        return Plan(variables, start, end, self.tiles, granules,
                    n_expected=len(variables) * len(dates) * len(self.tiles),
                    n_existing=len(self._existing))

    # --- run ------------------------------------------------------------
    def run(self, plan: Plan) -> dict:
        t0 = time.time()
        self.staging.mkdir(parents=True, exist_ok=True)
        stats = {"uploaded": 0, "skipped_existing": 0, "skipped_empty": 0,
                 "missing_at_source": 0, "errors": 0}
        errors: list[str] = []
        lock = threading.Lock()

        def process(granule: Granule):
            try:
                try:
                    assets = self.source.fetch(granule)
                except FileNotFoundError as exc:
                    with lock:
                        stats["missing_at_source"] += 1
                    log.warning("not available at source: %s (%s)", granule.key, exc)
                    return
                for asset in assets:
                    todo = [t for t in self.tiles
                            if self.overwrite or not self._have(asset.variable, asset.date, t)]
                    produced = tile_asset(
                        asset, todo, self.dcfg, self.gcfg.cog,
                        lambda t: self.staging / Path(self.key(asset.variable, asset.date, t)).name)
                    for tile, path in produced:
                        key = self.key(asset.variable, asset.date, tile)
                        if self.local_only:
                            dst = self.out_dir / key
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(path), dst)
                        else:
                            vcfg = self.dcfg.variables[asset.variable]
                            self.store.upload(path, key, metadata={
                                "dataset": self.dcfg.name, "version": self.dcfg.version,
                                "variable": asset.variable, "date": asset.date.isoformat(),
                                "tile": tile.id, "units": vcfg.units or "",
                            })
                            path.unlink()
                    produced_ids = {t.id for t, _ in produced}
                    self._save_empty(asset.variable, asset.date,
                                     {t.id for t in todo} - produced_ids)
                    with lock:
                        stats["uploaded"] += len(produced)
                        stats["skipped_existing"] += len(self.tiles) - len(todo)
                        stats["skipped_empty"] += len(todo) - len(produced)
                if not self.keep_local:
                    for p in {a.path for a in assets}:
                        p.unlink(missing_ok=True)
                log.info("done %s", granule.key)
            except Exception as exc:
                with lock:
                    stats["errors"] += 1
                    errors.append(f"{granule.key}: {exc}")
                log.error("failed %s: %s", granule.key, exc)

        max_workers = self.workers if self.source.parallel_fetch else 1
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(process, plan.granules))

        summary = {
            "dataset": self.dcfg.name,
            "variables": plan.variables,
            "start": str(plan.start), "end": str(plan.end),
            "bbox": self.bbox, "grid_tiles": len(self.tiles),
            "stats": stats, "errors": errors[:20],
            "duration_s": round(time.time() - t0, 1),
            "finished": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        if self.store is not None and plan.granules:
            ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self.store.put_json(f"{self.dcfg.name}/_runs/{ts}.json", summary)
        return summary
