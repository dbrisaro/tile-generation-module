"""``product_type`` en el pedido al CDS: fijo antes, ahora sale del YAML.

Por qué importa: los dos derived que usamos no tienen la misma forma de pedido.

  derived-era5-single-levels-daily-statistics  -> SÍ tiene product_type
      (hay reanalysis y ensemble, hay que elegir)
  derived-era5-land-daily-statistics           -> NO lo tiene
      (ERA5-Land no tiene ensemble; mandar el campo rompe el pedido)

Estaba hardcodeado ``"product_type": "reanalysis"``, así que ERA5-Land no podía
salir por CDS y quedaba atado al mirror EDH — que publica una vez por mes y va
~2 meses atrás. Es exactamente el pozo en el que se congeló swvl1.

De paso se fija que el archivo cacheado lleve el nombre del dataset: los dos
comparten workdir y una variable homónima se pisaría entre datasets.
"""
import datetime as dt
import sys
import types

import pytest

from tilegen.sources.base import Granule
from tilegen.sources.era5 import Era5CdsSource


class _Stop(Exception):
    """Corta fetch() apenas se armó el pedido, sin bajar nada."""


class _Var:
    cds_variable = "snow_cover"
    daily_statistic = "daily_mean"
    units = "%"


class _Cfg:
    def __init__(self, name, cds_dataset, product_type=None):
        self.name = name
        self.cds_dataset = cds_dataset
        self.cds_fetch_area = None
        self.variables = {"snowc_mean": _Var()}
        if product_type is not None:
            self.cds_product_type = product_type


SINGLE = _Cfg("era5", "derived-era5-single-levels-daily-statistics", "reanalysis")
LAND = _Cfg("era5-land", "derived-era5-land-daily-statistics")


@pytest.fixture
def pedido(monkeypatch):
    """Intercepta el request que se le arma al CDS, sin llamar al CDS."""
    visto = {}

    class _Client:
        def __init__(self, quiet=True):
            pass

        def retrieve(self, dataset, request, target):
            visto["dataset"] = dataset
            visto["request"] = request
            visto["target"] = target
            raise _Stop

    mod = types.ModuleType("cdsapi")
    mod.Client = _Client
    monkeypatch.setitem(sys.modules, "cdsapi", mod)
    return visto


def _fetch(cfg, tmp_path, pedido):
    src = Era5CdsSource(cfg, tmp_path)
    g = Granule(f"{cfg.name}:snowc_mean:2025-07", "snowc_mean", [dt.date(2025, 7, 1)])
    with pytest.raises(_Stop):
        src.fetch(g)
    return pedido


def test_single_levels_manda_product_type(tmp_path, pedido):
    """El comportamiento de siempre: el cron diario de era5 no puede cambiar."""
    p = _fetch(SINGLE, tmp_path, pedido)
    assert p["request"]["product_type"] == "reanalysis"
    assert p["dataset"] == "derived-era5-single-levels-daily-statistics"


def test_era5_land_no_manda_product_type(tmp_path, pedido):
    """Si el campo se cuela, el CDS de ERA5-Land rechaza el pedido."""
    p = _fetch(LAND, tmp_path, pedido)
    assert "product_type" not in p["request"]
    assert p["dataset"] == "derived-era5-land-daily-statistics"


def test_el_resto_del_pedido_no_cambia(tmp_path, pedido):
    p = _fetch(LAND, tmp_path, pedido)
    r = p["request"]
    assert r["variable"] == ["snow_cover"]
    assert r["daily_statistic"] == "daily_mean"
    assert r["time_zone"] == "utc+00:00"
    assert r["frequency"] == "1_hourly"
    assert r["year"] == ["2025"] and r["month"] == ["07"] and r["day"] == ["01"]


def test_el_cache_no_se_pisa_entre_datasets(tmp_path, pedido):
    """era5 y era5-land comparten workdir; el nombre lleva el dataset."""
    uno = _fetch(SINGLE, tmp_path, pedido)["target"]
    otro = _fetch(LAND, tmp_path, pedido)["target"]
    assert uno != otro
    assert uno.endswith("era5_snowc_mean_202507.nc")
    assert otro.endswith("era5-land_snowc_mean_202507.nc")


def test_la_config_real_de_cada_dataset_es_la_correcta():
    """Lo que se fija arriba tiene que coincidir con los YAML de verdad."""
    from pathlib import Path

    from tilegen.config import load_dataset

    conf = Path("tilegen/conf")
    assert getattr(load_dataset(conf, "era5"), "cds_product_type", None) == "reanalysis"
    assert getattr(load_dataset(conf, "era5-land"), "cds_product_type", None) is None
