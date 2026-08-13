"""Write-path tests for ZarrStore.

Exercise the region-write machinery against a real local Zarr store: a single
day, a contiguous multi-day batch, a non-contiguous batch (which must split
into two runs), out-of-order input, and untouched days reading back as NaN.
These lock in the batch/round-trip behaviour behind the day-by-day speed fix.
"""
import datetime as dt

import numpy as np
import pytest
import xarray as xr

from tilegen.config import (DatasetCfg, GlobalCfg, S3Cfg, VariableCfg, ZarrCfg)
from tilegen.zarrstore import ZarrStore

LAT = np.array([-2.0, -1.0, 0.0, 1.0], dtype="float64")
LON = np.array([10.0, 11.0, 12.0, 13.0, 14.0], dtype="float64")
START = dt.date(2024, 1, 1)
END = dt.date(2024, 1, 10)
VARIABLE = "t2m"


def _value(idx: int) -> np.ndarray:
    """Deterministic, per-day distinct field on the (lat, lon) grid."""
    y, x = np.meshgrid(np.arange(LAT.size), np.arange(LON.size), indexing="ij")
    return (idx * 1000 + y * 10 + x).astype("float32")


def _day(idx: int) -> xr.DataArray:
    """One day as a (time, lat, lon) DataArray on the store grid."""
    d = START + dt.timedelta(days=idx)
    return xr.DataArray(
        _value(idx)[None],
        dims=("time", "latitude", "longitude"),
        coords={"time": [np.datetime64(d, "ns")], "latitude": LAT, "longitude": LON},
    )


def _make_store(tmp_path) -> ZarrStore:
    gcfg = GlobalCfg(s3=S3Cfg(bucket="unused"), zarr=ZarrCfg(time_chunk=3, spatial_chunk=2))
    dcfg = DatasetCfg(
        name="testds", version="v1", source="http_geotiff",
        start=START, end=END, variables={VARIABLE: VariableCfg(units="K")},
    )
    store = ZarrStore(gcfg, dcfg, scene="scene00", local_only=True, out_dir=tmp_path)
    template = xr.DataArray(
        _value(0), dims=("latitude", "longitude"),
        coords={"latitude": LAT, "longitude": LON},
    )
    store.create(template)
    return store


def _reopen(store) -> xr.Dataset:
    return xr.open_zarr(store.mapper(), consolidated=True)


def test_single_and_batch_roundtrip(tmp_path):
    store = _make_store(tmp_path)

    store.write(_day(5).isel(time=0), VARIABLE, START + dt.timedelta(days=5))
    store.write_batch(xr.concat([_day(0), _day(1), _day(2)], dim="time"), VARIABLE)

    with _reopen(store) as ds:
        for idx in (0, 1, 2, 5):
            got = ds[VARIABLE].isel(time=idx).values
            np.testing.assert_array_equal(got, _value(idx))
        # days never written stay NaN
        for idx in (3, 4, 6, 7, 8, 9):
            assert np.isnan(ds[VARIABLE].isel(time=idx).values).all()


def test_non_contiguous_batch_splits_into_runs(tmp_path):
    store = _make_store(tmp_path)
    # gap at index 8: region="auto" cannot write this as one slice
    store.write_batch(xr.concat([_day(7), _day(9)], dim="time"), VARIABLE)

    with _reopen(store) as ds:
        np.testing.assert_array_equal(ds[VARIABLE].isel(time=7).values, _value(7))
        np.testing.assert_array_equal(ds[VARIABLE].isel(time=9).values, _value(9))
        assert np.isnan(ds[VARIABLE].isel(time=8).values).all()


def test_out_of_order_batch(tmp_path):
    store = _make_store(tmp_path)
    store.write_batch(xr.concat([_day(2), _day(0), _day(1)], dim="time"), VARIABLE)

    with _reopen(store) as ds:
        for idx in (0, 1, 2):
            np.testing.assert_array_equal(ds[VARIABLE].isel(time=idx).values, _value(idx))


def test_align_caches_grid_and_checks_mismatch(tmp_path):
    store = _make_store(tmp_path)
    assert store._grid is None

    aligned = store.align(_day(0))
    np.testing.assert_array_equal(aligned["latitude"].values, LAT)
    np.testing.assert_array_equal(aligned["longitude"].values, LON)
    assert store._grid is not None  # cached after first call

    bad = xr.DataArray(
        np.zeros((1, LAT.size, LON.size + 1), dtype="float32"),
        dims=("time", "latitude", "longitude"),
        coords={"time": [np.datetime64(START, "ns")],
                "latitude": LAT, "longitude": np.arange(LON.size + 1.0)},
    )
    with pytest.raises(ValueError):
        store.align(bad)


def test_ensure_variables_adds_without_touching_existing_data(tmp_path):
    """A variable added to the YAML after a store was built must be allocated
    into it, leaving already-written data, the group attrs and the ledger alone.
    """
    store = _make_store(tmp_path)
    store.write_batch(xr.concat([_day(0), _day(1)], dim="time"), VARIABLE)
    store.update_ledger(VARIABLE, written=[START, START + dt.timedelta(days=1)])
    with _reopen(store) as ds:
        attrs_before = dict(ds.attrs)
    assert attrs_before  # create() stamped dataset/version/scene/...

    # a plain region-write of an unknown variable is what fails today
    new = _day(0).rename("ssrd_sum").to_dataset()
    with pytest.raises(ValueError, match="non-pre-existing"):
        new.to_zarr(store.mapper(), region="auto", consolidated=False)

    store.dcfg.variables["ssrd_sum"] = VariableCfg(units="J m-2")
    store.ensure_variables(["ssrd_sum"])
    store.ensure_variables(["ssrd_sum"])  # idempotent

    with _reopen(store) as ds:
        assert ds["ssrd_sum"].shape == ds[VARIABLE].shape
        assert ds["ssrd_sum"].attrs["units"] == "J m-2"
        assert np.isnan(ds["ssrd_sum"].values).all()   # allocated, unwritten
        np.testing.assert_array_equal(ds[VARIABLE].isel(time=0).values, _value(0))
        np.testing.assert_array_equal(ds[VARIABLE].isel(time=1).values, _value(1))
        assert dict(ds.attrs) == attrs_before
    assert store.read_ledger(VARIABLE)["written"] == ["2024-01-01", "2024-01-02"]

    # and the new variable is writable afterwards
    store.write(_day(3).isel(time=0), "ssrd_sum", START + dt.timedelta(days=3))
    with _reopen(store) as ds:
        np.testing.assert_array_equal(ds["ssrd_sum"].isel(time=3).values, _value(3))


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
