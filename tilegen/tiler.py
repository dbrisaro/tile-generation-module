"""Cut a source raster (GeoTIFF or NetCDF) into fixed-grid Cloud-Optimized GeoTIFFs.

Tiles that fall completely outside the source extent, or that contain only
nodata (open ocean for land-only products), are skipped — they are simply
absent from S3, which keeps the archive compact.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window, from_bounds

log = logging.getLogger("tilegen.tiler")


def _is_empty(data: np.ndarray, nodata) -> bool:
    if nodata is not None and not np.isnan(nodata):
        return bool(np.all(data == nodata))
    return bool(np.all(~np.isfinite(data)))


def write_cog(path: Path, data: np.ndarray, transform, nodata, cog) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(
        driver="COG",
        compress=cog.compress,
        blocksize=cog.blocksize,
        predictor="YES",
        overview_resampling=cog.overview_resampling,
        num_threads="ALL_CPUS",
        width=data.shape[1],
        height=data.shape[0],
        count=1,
        dtype=data.dtype.name,
        crs="EPSG:4326",
        transform=transform,
        nodata=nodata,
    )
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def tile_asset(asset, tiles, dcfg, cog, out_path_fn) -> list[tuple]:
    """Cut one asset (a single variable at a single date) into COG tiles.

    ``out_path_fn(tile) -> Path`` decides where each tile file is written.
    Returns [(tile, path), ...] for the tiles actually produced.
    """
    if asset.path.suffix in (".nc", ".nc4"):
        return _tile_netcdf(asset, tiles, cog, out_path_fn)
    return _tile_geotiff(asset, tiles, dcfg.nodata, cog, out_path_fn)


def _tile_geotiff(asset, tiles, nodata, cog, out_path_fn):
    produced = []
    with rasterio.open(asset.path) as src:
        nd = nodata if nodata is not None else src.nodata
        b = src.bounds
        for tile in tiles:
            if (tile.maxx <= b.left or tile.minx >= b.right
                    or tile.maxy <= b.bottom or tile.miny >= b.top):
                continue
            w = from_bounds(*tile.bounds, transform=src.transform)
            w = Window(round(w.col_off), round(w.row_off), round(w.width), round(w.height))
            data = src.read(asset.band, window=w, boundless=True,
                            fill_value=nd if nd is not None else 0)
            if _is_empty(data, nd):
                continue
            out = out_path_fn(tile)
            write_cog(out, data, src.window_transform(w), nd, cog)
            produced.append((tile, out))
    return produced


def _tile_netcdf(asset, tiles, cog, out_path_fn):
    import rioxarray  # noqa: F401  (registers the .rio accessor)
    import xarray as xr
    from rioxarray.exceptions import NoDataInBounds

    produced = []
    with xr.open_dataset(asset.path) as ds:
        da = _prepare(ds, asset)
        for tile in tiles:
            try:
                sub = da.rio.clip_box(*tile.bounds)
            except NoDataInBounds:
                continue
            data = sub.values.astype("float32")
            if _is_empty(data, np.nan):
                continue
            out = out_path_fn(tile)
            write_cog(out, data, sub.rio.transform(), float("nan"), cog)
            produced.append((tile, out))
    return produced


def _prepare(ds, asset):
    """Select the data variable and time step; normalize lons to [-180, 180]."""
    spatial_y = {"latitude", "lat", "y"}
    spatial_x = {"longitude", "lon", "x"}
    var = next(v for v in ds.data_vars
               if spatial_y & set(ds[v].dims) and spatial_x & set(ds[v].dims))
    da = ds[var]
    tdim = next((d for d in ("valid_time", "time") if d in da.dims), None)
    if tdim is not None:
        da = da.isel({tdim: asset.time_index})
    lon = next(c for c in ("longitude", "lon", "x") if c in da.coords)
    lat = next(c for c in ("latitude", "lat", "y") if c in da.coords)
    if float(da[lon].max()) > 180:
        da = da.assign_coords({lon: ((da[lon] + 180) % 360) - 180}).sortby(lon)
    da = da.rio.set_spatial_dims(x_dim=lon, y_dim=lat)
    return da.rio.write_crs("EPSG:4326")
