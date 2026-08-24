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


def _strip(uri: str) -> str:
    """Drop the ``s3://`` scheme: fsspec's file ops want bucket-relative paths."""
    return uri[len("s3://"):] if uri.startswith("s3://") else uri


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
    def mapper(self, uri: str | None = None):
        # local paths need auto_mkdir under zarr 3 (its fsspec store no longer
        # creates parent directories on write; the s3 mapper is unaffected)
        uri = uri or self.uri
        kwargs = {} if uri.startswith("s3://") else {"auto_mkdir": True}
        return fsspec.get_mapper(uri, **kwargs)

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

    def ensure_variables(self, variables) -> None:
        """Allocate config variables the store predates, as unwritten NaN.

        ``create`` only allocates the variables the config had at creation time,
        and ``write_batch`` uses ``region="auto"``, which fills pre-allocated
        chunks but cannot add a variable. So a variable added to the YAML after
        a store was built is invisible to it and writes fail with "non
        pre-existing variables". This backfills the metadata for those.
        """
        import dask.array as dsa

        with xr.open_zarr(self.mapper(), consolidated=True) as ds:
            missing = [v for v in variables if v not in ds.data_vars]
            if not missing:
                return
            ny, nx = ds.sizes["latitude"], ds.sizes["longitude"]
            chunks = self._chunks(ny, nx)
            add = xr.Dataset(
                {v: (DIMS, dsa.full((ds.sizes["time"], ny, nx), np.nan,
                                    chunks=chunks, dtype="float32"),
                     {"units": self.dcfg.variables[v].units or ""})
                 for v in missing},
                coords={"time": ds["time"].values,
                        "latitude": ds["latitude"].values,
                        "longitude": ds["longitude"].values},
                # mode="a" rewrites the group attrs from what it is handed, so
                # carry the store's own attrs over or they would be wiped
                attrs=dict(ds.attrs),
            )
            encoding = {v: {"_FillValue": np.float32(np.nan)} for v in missing}
        add.to_zarr(self.mapper(), mode="a", compute=False, encoding=encoding,
                    consolidated=True, **_v2_kwargs())
        log.info("allocated %s in %s", ", ".join(missing), self.uri)

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

    # --- chunking ---------------------------------------------------------
    def chunk_info(self) -> dict:
        """Per-variable ``{shape, chunks}`` of the store as it exists on S3.

        Read straight out of the consolidated index, so it costs ONE request
        per store: the audit walks hundreds of cubes and opening each one with
        xarray would mean several round trips apiece.
        """
        meta = json.loads(self.fs.cat(f"{self.uri}/.zmetadata"))["metadata"]
        out = {}
        for key, m in meta.items():
            if not key.endswith("/.zarray"):
                continue
            name = key.rsplit("/", 1)[0]
            if name in DIMS:          # coords are chunked whole; not interesting
                continue
            out[name] = {"shape": tuple(m["shape"]), "chunks": tuple(m["chunks"])}
        return out

    def target_chunks(self) -> tuple[int, int, int] | None:
        """What the current config says this store's chunks should be."""
        info = self.chunk_info()
        if not info:
            return None
        _, ny, nx = next(iter(info.values()))["shape"]
        return self._chunks(ny, nx)

    def _move(self, src: str, dst: str) -> None:
        """Server-side copy then delete, instead of ``fs.mv``.

        s3fs's recursive ``mv`` copies and then deletes off its own listing
        cache, and the delete dies with "The specified key does not exist" on a
        store this size. Splitting the two and invalidating in between is both
        reliable and re-runnable.
        """
        src, dst = _strip(src), _strip(dst)
        self.fs.invalidate_cache()
        self.fs.cp(src, dst, recursive=True)
        self.fs.invalidate_cache()
        self.fs.rm(src, recursive=True)
        self.fs.invalidate_cache()

    def _cells(self, uri: str) -> dict:
        """Non-NaN cell count per variable — the yardstick a copy has to match."""
        with xr.open_zarr(self.mapper(uri), consolidated=True) as ds:
            return {v: int((~np.isnan(ds[v].data)).sum().compute()) for v in ds.data_vars}

    def promote(self, temp_suffix: str = ".rechunk", verify: bool = True,
                keep_old: bool = False) -> dict:
        """Verify an already-built copy and swap it in. Safe to re-run."""
        src_uri, tmp_uri = self.uri, self.uri + temp_suffix
        if not self.fs.exists(f"{_strip(tmp_uri)}/.zgroup"):
            raise RuntimeError(f"no hay copia para promover en {tmp_uri}")
        if verify:
            self._verify_copy(tmp_uri, self._cells(src_uri))
        if keep_old:
            self._move(src_uri, src_uri + ".old")
        else:
            self.fs.invalidate_cache()
            self.fs.rm(_strip(src_uri), recursive=True)
            self.fs.invalidate_cache()
        self._move(tmp_uri, src_uri)
        log.info("promoted %s", src_uri)
        return {"status": "promoted"}

    def rechunk(self, temp_suffix: str = ".rechunk", verify: bool = True,
                keep_old: bool = False, log_every: int = 20,
                resume: bool = False) -> dict:
        """Rewrite the store with the chunk shape the config asks for.

        Chunk shape is baked in at creation and nothing reshapes it in place,
        so this builds a second store beside the first and promotes it once it
        checks out. The original is only removed after the copy is verified.

        Two properties of the existing cubes have to survive:

        * **Sparsity.** Chunks that were never written do not exist as objects
          and read back as NaN. Copying blindly would materialize them — chirts
          alone has 27 empty years across 20 scenes — so all-NaN slabs are
          skipped instead of written.
        * **Zarr v2 on disk**, via ``_v2_kwargs()``, so both zarr 2.x and 3.x
          environments keep reading the result.

        The ledger lives outside the ``.zarr`` (``_ledger/{scene}/``) and is
        left untouched: a rechunk moves bytes, not bookkeeping.
        """
        import dask.array as dsa

        src_uri, tmp_uri = self.uri, self.uri + temp_suffix
        with xr.open_zarr(self.mapper(), consolidated=True) as ds:
            ny, nx = ds.sizes["latitude"], ds.sizes["longitude"]
            chunks = self._chunks(ny, nx)
            current = {v: tuple(ds[v].encoding["chunks"]) for v in ds.data_vars}
            if all(c == chunks for c in current.values()):
                return {"status": "skipped", "chunks": chunks}
            if self.fs.exists(f"{_strip(tmp_uri)}/.zgroup"):
                if resume:
                    log.info("reusing the copy already at %s", tmp_uri)
                    return {"chunks": chunks, "was": current,
                            **self.promote(temp_suffix, verify, keep_old)}
                raise RuntimeError(
                    f"{tmp_uri} ya existe — hay un rechunk a medio hacer. "
                    f"Corre con --resume para verificarlo y promoverlo, o borralo.")

            times = ds["time"].values
            variables = list(ds.data_vars)
            template = xr.Dataset(
                {v: (DIMS, dsa.full((len(times), ny, nx), np.nan,
                                    chunks=chunks, dtype="float32"),
                     dict(ds[v].attrs))
                 for v in variables},
                coords={"time": times, "latitude": ds["latitude"].values,
                        "longitude": ds["longitude"].values},
                attrs=dict(ds.attrs),
            )
            template.to_zarr(self.mapper(tmp_uri), compute=False, consolidated=True,
                             encoding={v: {"_FillValue": np.float32(np.nan)}
                                       for v in variables},
                             **_v2_kwargs())

            # Slabs are aligned to the TARGET time chunk so every write fills
            # whole chunks; an unaligned write would force zarr to read back
            # and re-compress each partially-touched chunk.
            step = chunks[0]
            stats = {"copied": 0, "skipped_empty": 0, "cells": {}}
            for v in variables:
                kept = 0
                for i0 in range(0, len(times), step):
                    slab = ds[v].isel(time=slice(i0, i0 + step)).load()
                    good = int(np.count_nonzero(~np.isnan(slab.values)))
                    if not good:
                        stats["skipped_empty"] += 1
                        continue
                    (slab.to_dataset(name=v)
                         .to_zarr(self.mapper(tmp_uri), region="auto", consolidated=False))
                    kept += good
                    stats["copied"] += 1
                    if stats["copied"] % log_every == 0:
                        log.info("  %s %s: %d/%d dias", self.scene, v,
                                 min(i0 + step, len(times)), len(times))
                stats["cells"][v] = kept

        if verify:
            # Counts gathered while copying, so this costs one read of the copy
            # rather than a second read of the original.
            self._verify_copy(tmp_uri, stats["cells"])
        self.promote(temp_suffix, verify=False, keep_old=keep_old)
        log.info("rechunked %s -> chunks %s", src_uri, chunks)
        return {"status": "done", "chunks": chunks, "was": current, **stats}

    def _verify_copy(self, tmp_uri: str, expected: dict) -> None:
        """Fail loudly before anything is deleted if the copy lost data."""
        with xr.open_zarr(self.mapper(tmp_uri), consolidated=True) as ds:
            for v, want in expected.items():
                got = int((~np.isnan(ds[v].data)).sum().compute())
                if got != want:
                    raise RuntimeError(
                        f"verificacion fallida en {tmp_uri}:{v} — "
                        f"{got} celdas con dato, se esperaban {want}. "
                        f"El store original NO se toco; borra el temporal y reintenta.")

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
