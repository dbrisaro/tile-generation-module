"""Source de CPC Global Temperature (NOAA PSL).

El riesgo real de esta fuente no es la descarga, es la grilla: CPC viene con
longitud 0..360 y latitud DESCENDENTE. Un recorte mal orientado no falla, sino
que devuelve un cubo vacío o el pedazo equivocado del planeta — así que eso es
lo que se fija acá, junto con el agrupado por año y el salteo de años que la
fuente todavía no publicó.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from tilegen.sources.base import Granule
from tilegen.sources.cpc_psl import CpcPslSource


class _Cfg:
    """DatasetCfg mínima: sólo lo que mira el source."""
    name = "cpc"
    base_url = "memoria://cpc"

    def __init__(self, variables):
        self.variables = variables


class _Var:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __getattr__(self, _):
        return None


def _cpc(dias=5, desde="2024-01-01"):
    """Cubo sintético con la geometría real de CPC: lon 0..360, lat descendente."""
    t = pd.date_range(desde, periods=dias, freq="D")
    lat = np.array([10.0, 5.0, 0.0, -5.0, -10.0])        # descendente, como la fuente
    lon = np.array([280.0, 285.0, 290.0, 295.0])          # 0..360, como la fuente
    # cada celda vale lon*100 + lat, para poder ubicar el recorte sin ambigüedad
    vals = np.broadcast_to(lon[None, None, :] * 100 + lat[None, :, None],
                           (dias, lat.size, lon.size)).astype("float32")
    return xr.Dataset({"tmax": (("time", "lat", "lon"), vals.copy())},
                      coords={"time": t, "lat": lat, "lon": lon})


def _source(tmp_path, ds, area=None):
    src = CpcPslSource(_Cfg({"tmax": _Var(source_file="tmax", units="degC")}), tmp_path)
    src.area = area
    src._open = lambda url: ds            # nunca sale a la red
    return src


def _granule(dias=5, desde="2024-01-01", variable="tmax"):
    d0 = dt.date.fromisoformat(desde)
    return Granule(key="test", variable=variable,
                   dates=[d0 + dt.timedelta(days=i) for i in range(dias)])


def test_granules_are_one_per_variable_and_year(tmp_path):
    """Un archivo por año: 731 días tienen que dar 2 granules, no 731."""
    src = _source(tmp_path, _cpc())
    dates = [dt.date(2023, 1, 1) + dt.timedelta(days=i) for i in range(731)]
    gs = src.granules(["tmax"], dates)
    assert len(gs) == 2
    assert [g.dates[0].year for g in gs] == [2023, 2024]
    assert len(gs[0].dates) == 365 and len(gs[1].dates) == 366


def test_crop_converts_a_negative_bbox_to_0_360(tmp_path):
    """El bbox de las escenas está en -180..180; la fuente en 0..360."""
    src = _source(tmp_path, _cpc(), area=(-80.0, -6.0, -70.0, 6.0))
    da = src._read_year(_granule())
    assert float(da.lon.min()) == 280.0 and float(da.lon.max()) == 290.0
    # latitud descendente: el slice tiene que ir de maxy a miny, no al revés
    assert list(da.lat.values) == [5.0, 0.0, -5.0]
    assert da.sizes["time"] == 5


def test_crop_keeps_descending_latitude(tmp_path):
    src = _source(tmp_path, _cpc(), area=(-80.0, -11.0, -65.0, 11.0))
    da = src._read_year(_granule())
    lats = list(da.lat.values)
    assert lats == sorted(lats, reverse=True), "se perdió el orden descendente"


def test_values_land_where_they_should(tmp_path):
    """Un recorte mal orientado devuelve datos, pero los equivocados."""
    src = _source(tmp_path, _cpc(), area=(-80.0, -6.0, -70.0, 6.0))
    da = src._read_year(_granule())
    esperado = 285.0 * 100 + 0.0          # lon 285, lat 0
    assert float(da.sel(lat=0.0, lon=285.0).isel(time=0)) == esperado


def test_bbox_outside_the_grid_is_an_error(tmp_path):
    src = _source(tmp_path, _cpc(), area=(100.0, -6.0, 110.0, 6.0))
    with pytest.raises(ValueError, match="outside the CPC grid"):
        src._read_year(_granule())


def test_dates_not_yet_published_raise_filenotfound(tmp_path):
    """El pipeline traduce esto a 'missing' en el ledger y sigue."""
    src = _source(tmp_path, _cpc(dias=3, desde="2024-01-01"))
    with pytest.raises(FileNotFoundError, match="not yet in CPC"):
        src._read_year(_granule(dias=2, desde="2024-06-01"))


def test_a_missing_year_file_raises_filenotfound(tmp_path):
    src = _source(tmp_path, _cpc())

    def _boom(url):
        raise OSError("404 NetCDF: file not found")

    src._open = _boom
    with pytest.raises(FileNotFoundError, match="not available"):
        src._read_year(_granule())


def test_fetch_caches_the_year_and_indexes_the_days(tmp_path):
    """fetch devuelve un Asset por día, apuntando al mismo NetCDF cacheado."""
    src = _source(tmp_path, _cpc(dias=5), area=(-80.0, -6.0, -70.0, 6.0))
    assets = src.fetch(_granule(dias=5))
    assert len(assets) == 5
    assert [a.time_index for a in assets] == [0, 1, 2, 3, 4]
    assert len({a.path for a in assets}) == 1
    assert assets[0].path.exists()
    assert [a.date for a in assets] == [dt.date(2024, 1, 1) + dt.timedelta(days=i)
                                        for i in range(5)]


def test_fetch_only_returns_the_days_asked_for(tmp_path):
    src = _source(tmp_path, _cpc(dias=5), area=(-80.0, -6.0, -70.0, 6.0))
    g = Granule(key="test", variable="tmax",
                dates=[dt.date(2024, 1, 2), dt.date(2024, 1, 4)])
    assets = src.fetch(g)
    assert [a.date for a in assets] == [dt.date(2024, 1, 2), dt.date(2024, 1, 4)]


def test_url_uses_source_file_and_year(tmp_path):
    src = _source(tmp_path, _cpc())
    assert src._url("tmax", 2024) == "memoria://cpc/tmax.2024.nc"
