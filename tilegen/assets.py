"""Convert a fetched asset (GeoTIFF or NetCDF) into a standardized DataArray:
dims (latitude, longitude), float32, NaN as nodata, clipped to a bbox.
This is the common currency between the sources and the Zarr writer.
"""
from __future__ import annotations

import numpy as np
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr

from .tiler import _prepare
from .utils import nc_lock


def asset_to_dataarray(asset, dcfg, bbox) -> xr.DataArray:
    if asset.path.suffix in (".nc", ".nc4"):
        with nc_lock, xr.open_dataset(asset.path) as ds:
            da = _prepare(ds, asset).load()
        nd = None  # NetCDF sources carry NaN already
    else:
        da = rioxarray.open_rasterio(asset.path).squeeze("band", drop=True)
        nd = dcfg.nodata if dcfg.nodata is not None else da.rio.nodata
    da = da.rio.clip_box(*bbox)
    da = da.astype("float32")
    if nd is not None and not np.isnan(nd):
        da = da.where(da != np.float32(nd))
    da = da.rename({da.rio.x_dim: "longitude", da.rio.y_dim: "latitude"})
    return da.reset_coords(drop=True)
