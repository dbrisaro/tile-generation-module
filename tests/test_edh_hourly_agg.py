"""Agregación horaria -> diaria del source EDH.

Lo que se protege acá es la ventana de agregación, que es donde es fácil
equivocarse por una hora: max/min/mean toman las 24 horas del día D, y sum
toma 01:00 D .. 00:00 D+1 (las acumulaciones de ERA5 vienen estampadas al
final de la hora). Los días incompletos se descartan en vez de escribirse.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from tilegen.sources.base import Granule
from tilegen.sources.edh import Era5EdhSource


class _Cfg:
    """DatasetCfg mínima: sólo lo que mira el source."""
    edh_hourly_store = "memoria://horario"
    edh_store = "memoria://diario"

    def __init__(self, variables):
        self.variables = variables


class _Var:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __getattr__(self, _):
        return None


def _source(tmp_path, variables, ds):
    src = Era5EdhSource(_Cfg(variables), tmp_path)
    src.area = None                      # sin recorte: la grilla del test ya es chica
    src._open = lambda url: ds           # nunca sale a la red
    return src


def _horario(dias=3, desde="2024-03-01", valores=None):
    """Cubo horario sintético (time, lat, lon) con 24 h por día."""
    t = pd.date_range(desde, periods=dias * 24, freq="h")
    lat = np.array([10.0, 9.0])
    lon = np.array([-70.0, -69.0])
    if valores is None:
        # cada hora vale su índice horario, así el min/max del día es evidente
        valores = np.tile(np.arange(24, dtype="float32")[:, None, None], (dias, 2, 2))
    return xr.Dataset(
        {"t2m": (("valid_time", "latitude", "longitude"), valores)},
        coords={"valid_time": t, "latitude": lat, "longitude": lon},
    )


def _granule(dias=3, desde="2024-03-01"):
    d0 = dt.date.fromisoformat(desde)
    return Granule(key="test", variable="t2m_x",
                   dates=[d0 + dt.timedelta(days=i) for i in range(dias)])


# --- el estadístico nuevo -------------------------------------------------

def test_min_toma_la_hora_mas_baja_del_dia(tmp_path):
    ds = _horario()
    src = _source(tmp_path, {"t2m_min": _Var(edh_hourly_variable="t2m",
                                             edh_hourly_statistic="min")}, ds)
    out = src._read_hourly(_granule(), "t2m", "min")
    assert out.sizes["time"] == 3
    # las horas valen 0..23 -> la mínima diaria es 0
    assert np.allclose(out.values, 0.0)


def test_min_y_max_delimitan_el_dia_correcto(tmp_path):
    """Un pico en una hora concreta no debe filtrarse al día vecino."""
    vals = np.zeros((3 * 24, 2, 2), dtype="float32")
    vals[24 + 5] = -8.0     # 05:00 del día 2: la helada
    vals[24 + 14] = 31.0    # 14:00 del día 2: el pico de calor
    ds = _horario(valores=vals)
    src = _source(tmp_path, {}, ds)

    mins = src._read_hourly(_granule(), "t2m", "min").values
    maxs = src._read_hourly(_granule(), "t2m", "max").values
    assert np.allclose(mins[1], -8.0) and np.allclose(maxs[1], 31.0)
    # los días 1 y 3 quedan intactos
    for i in (0, 2):
        assert np.allclose(mins[i], 0.0) and np.allclose(maxs[i], 0.0)


def test_min_descarta_dias_sin_las_24_horas(tmp_path):
    ds = _horario().isel(valid_time=slice(0, 24 + 10))   # día 2 truncado
    src = _source(tmp_path, {}, ds)
    out = src._read_hourly(_granule(dias=2), "t2m", "min")
    assert out.sizes["time"] == 1
    assert str(out.time.values[0])[:10] == "2024-03-01"


# --- que el estadístico nuevo esté aceptado en la validación --------------

def test_mapping_acepta_min(tmp_path):
    src = _source(tmp_path, {"t2m_min": _Var(edh_hourly_variable="t2m",
                                             edh_hourly_statistic="min")}, None)
    assert src._mapping("t2m_min") == ("hourly", "t2m", "min")


def test_mapping_rechaza_un_estadistico_desconocido(tmp_path):
    src = _source(tmp_path, {"raro": _Var(edh_hourly_variable="t2m",
                                          edh_hourly_statistic="mediana")}, None)
    with pytest.raises(ValueError, match="edh_hourly_statistic"):
        src._mapping("raro")


# --- control: las ventanas que ya existían no se movieron ----------------

def test_sum_sigue_usando_la_ventana_corrida(tmp_path):
    """sum toma 01:00 D .. 00:00 D+1, así que necesita la hora 0 del día siguiente."""
    ds = _horario(dias=4)
    src = _source(tmp_path, {}, ds)
    out = src._read_hourly(_granule(dias=3), "t2m", "sum")
    # horas 1..23 del día D más la hora 0 del día D+1 = 1+2+...+23 + 0 = 276
    assert np.allclose(out.values, 276.0)


def test_mean_promedia_las_24_horas(tmp_path):
    ds = _horario()
    src = _source(tmp_path, {}, ds)
    out = src._read_hourly(_granule(), "t2m", "mean")
    assert np.allclose(out.values, np.arange(24).mean())
