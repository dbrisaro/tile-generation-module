"""Rechunk tests for ZarrStore.

Chunk shape is fixed when a store is created, so changing config.yaml only
reaches new cubes; `rechunk` is what moves an existing one. What has to hold:
the data comes out bit-identical, days never written stay NaN, the sparsity is
preserved (all-NaN slabs are not materialized), and a copy that lost data is
caught BEFORE the original is deleted.
"""
import datetime as dt

import numpy as np
import pytest
import xarray as xr

from tilegen.config import DatasetCfg, GlobalCfg, S3Cfg, VariableCfg, ZarrCfg
from tilegen.zarrstore import ZarrStore

LAT = np.arange(8, dtype="float64")
LON = np.arange(10, dtype="float64")
START = dt.date(2024, 1, 1)
END = dt.date(2024, 1, 12)
VARIABLE = "t2m"
WRITTEN = (0, 1, 2, 9)          # days 3..8 and 10..11 stay empty on purpose


def _value(idx: int) -> np.ndarray:
    y, x = np.meshgrid(np.arange(LAT.size), np.arange(LON.size), indexing="ij")
    return (idx * 1000 + y * 10 + x).astype("float32")


def _day(idx: int) -> xr.DataArray:
    d = START + dt.timedelta(days=idx)
    return xr.DataArray(
        _value(idx)[None],
        dims=("time", "latitude", "longitude"),
        coords={"time": [np.datetime64(d, "ns")], "latitude": LAT, "longitude": LON},
    )


def _store(tmp_path, spatial_chunk, time_chunk=3) -> ZarrStore:
    gcfg = GlobalCfg(s3=S3Cfg(bucket="unused"),
                     zarr=ZarrCfg(time_chunk=time_chunk, spatial_chunk=spatial_chunk))
    dcfg = DatasetCfg(name="testds", version="v1", source="http_geotiff",
                      start=START, end=END,
                      variables={VARIABLE: VariableCfg(units="K")})
    return ZarrStore(gcfg, dcfg, scene="scene00", local_only=True, out_dir=tmp_path)


def _populated(tmp_path, spatial_chunk=16):
    """A store whose spatial chunk covers the whole grid — the ERA5 pathology."""
    store = _store(tmp_path, spatial_chunk)
    template = xr.DataArray(_value(0), dims=("latitude", "longitude"),
                            coords={"latitude": LAT, "longitude": LON})
    store.create(template)
    for idx in WRITTEN:
        store.write(_day(idx).isel(time=0), VARIABLE, START + dt.timedelta(days=idx))
    return store


def test_starts_unsplit_spatially(tmp_path):
    """The bug this feature exists for: a cap above the grid size swallows it."""
    store = _populated(tmp_path, spatial_chunk=16)
    info = store.chunk_info()
    assert info[VARIABLE]["chunks"] == (3, LAT.size, LON.size)


def test_rechunk_preserves_values_and_nans(tmp_path):
    store = _populated(tmp_path, spatial_chunk=16)
    with xr.open_zarr(store.mapper(), consolidated=True) as ds:
        before = ds[VARIABLE].values.copy()

    store.gcfg.zarr.spatial_chunk = 4          # what the new config would say
    result = store.rechunk()

    assert result["status"] == "done"
    assert result["chunks"] == (3, 4, 4)
    with xr.open_zarr(store.mapper(), consolidated=True) as ds:
        assert ds[VARIABLE].encoding["chunks"] == (3, 4, 4)
        np.testing.assert_array_equal(ds[VARIABLE].values, before)
        for idx in WRITTEN:                     # data survives exactly
            np.testing.assert_array_equal(ds[VARIABLE].isel(time=idx).values, _value(idx))
        for idx in set(range(12)) - set(WRITTEN):   # and gaps stay gaps
            assert np.isnan(ds[VARIABLE].isel(time=idx).values).all()
        assert ds.attrs["dataset"] == "testds"      # group attrs carried over
        assert ds[VARIABLE].attrs["units"] == "K"


def test_rechunk_keeps_the_store_sparse(tmp_path):
    """All-NaN slabs must not be written: chirts has 27 empty years per scene."""
    store = _populated(tmp_path, spatial_chunk=16)
    store.gcfg.zarr.spatial_chunk = 4
    result = store.rechunk()

    # 12 days / time_chunk 3 = 4 slabs; days 3-8 make one wholly empty slab
    assert result["skipped_empty"] >= 1
    root = store.uri.replace("file://", "")
    chunk_files = [p for p in __import__("pathlib").Path(root, VARIABLE).iterdir()
                   if not p.name.startswith(".")]
    dense = 4 * (LAT.size // 4 + 1) * (LON.size // 4 + 1)
    assert len(chunk_files) < dense, "se materializaron chunks vacios"


def test_rechunk_is_a_noop_when_already_correct(tmp_path):
    store = _populated(tmp_path, spatial_chunk=4)
    assert store.rechunk()["status"] == "skipped"


def _halfway(tmp_path):
    """A store plus the copy an interrupted run would have left behind."""
    store = _populated(tmp_path, spatial_chunk=16)
    store.gcfg.zarr.spatial_chunk = 4
    store._promote_orig = store.promote
    store.promote = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("corte de luz"))
    with pytest.raises(RuntimeError, match="corte de luz"):
        store.rechunk()
    store.promote = store._promote_orig
    return store


def test_rechunk_refuses_a_leftover_temp(tmp_path):
    store = _halfway(tmp_path)
    with pytest.raises(RuntimeError, match="rechunk a medio hacer"):
        store.rechunk()


def test_resume_promotes_the_leftover_copy(tmp_path):
    """Un corte en el último paso no debe costar volver a copiar todo."""
    store = _halfway(tmp_path)
    assert __import__("pathlib").Path(store.uri + ".rechunk").exists()

    result = store.rechunk(resume=True)

    assert result["status"] == "promoted"
    assert not __import__("pathlib").Path(store.uri + ".rechunk").exists()
    with xr.open_zarr(store.mapper(), consolidated=True) as ds:
        assert ds[VARIABLE].encoding["chunks"] == (3, 4, 4)
        for idx in WRITTEN:
            np.testing.assert_array_equal(ds[VARIABLE].isel(time=idx).values, _value(idx))
        for idx in set(range(12)) - set(WRITTEN):
            assert np.isnan(ds[VARIABLE].isel(time=idx).values).all()


def test_failed_verification_leaves_the_original_alone(tmp_path):
    """The whole point of verifying before promoting."""
    store = _populated(tmp_path, spatial_chunk=16)
    store.gcfg.zarr.spatial_chunk = 4
    store._verify_copy = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("verificacion fallida"))

    with pytest.raises(RuntimeError, match="verificacion fallida"):
        store.rechunk()

    with xr.open_zarr(store.mapper(), consolidated=True) as ds:
        assert ds[VARIABLE].encoding["chunks"] == (3, LAT.size, LON.size)
        for idx in WRITTEN:
            np.testing.assert_array_equal(ds[VARIABLE].isel(time=idx).values, _value(idx))


def test_keep_old_leaves_a_copy(tmp_path):
    store = _populated(tmp_path, spatial_chunk=16)
    store.gcfg.zarr.spatial_chunk = 4
    store.rechunk(keep_old=True)
    assert __import__("pathlib").Path(store.uri + ".old").exists()
