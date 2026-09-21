"""Cuándo una ausencia en la fuente se da por DEFINITIVA.

El ledger guarda en `missing` los días que la fuente no tiene, y `plan()` los
saltea para siempre. El problema es que "no existe" y "todavía no lo
publicaron" se ven idénticos en el momento del pedido: lo único que los separa
es el tiempo. Marcar un día reciente lo congela — el cron sigue corriendo, no
da error, y nunca levanta ese día aunque la fuente lo publique al día
siguiente. Eso fue lo que dejó a swvl1 clavada en 2026-06-30.

Acá se fija el horizonte (cuánto se espera antes de dar algo por perdido) y el
escape de `--retry-missing` para lo que ya quedó congelado.
"""
import datetime as dt

import pytest

from tilegen.config import DatasetCfg, VariableCfg
from tilegen.zarr_pipeline import ZarrPipeline

HOY = dt.date.today()


def _dcfg(**kw):
    base = dict(name="x", version="v1", source="cpc_psl", start=dt.date(2020, 1, 1),
                lag_days=6, variables={"a": VariableCfg(), "b": VariableCfg()})
    base.update(kw)
    return DatasetCfg(**base)


def _pipe(dcfg, **kw):
    """ZarrPipeline sin tocar S3 ni la fuente: sólo se ejercita la política."""
    p = object.__new__(ZarrPipeline)
    p.dcfg = dcfg
    p.overwrite = kw.get("overwrite", False)
    p.retry_missing = kw.get("retry_missing", False)
    return p


# ---------------------------------------------------------------- horizonte
def test_por_defecto_el_horizonte_sale_del_lag_de_la_fuente():
    p = _pipe(_dcfg(lag_days=6))
    assert p._horizonte("a") == 36          # lag_days + 30


def test_el_dataset_puede_fijar_su_propio_horizonte():
    p = _pipe(_dcfg(missing_after_days=120))
    assert p._horizonte("a") == 120


def test_una_variable_puede_pisar_el_horizonte_del_dataset():
    # el caso swvl1: el dataset habla del CDS (lag 6), pero la variable sale
    # por un mirror que actualiza una vez por mes
    dcfg = _dcfg(missing_after_days=40,
                 variables={"a": VariableCfg(missing_after_days=150),
                            "b": VariableCfg()})
    p = _pipe(dcfg)
    assert p._horizonte("a") == 150
    assert p._horizonte("b") == 40


# ------------------------------------------------- qué se anota y qué no
def test_un_dia_reciente_no_se_da_por_perdido():
    p = _pipe(_dcfg(missing_after_days=30))
    ayer = HOY - dt.timedelta(days=1)
    assert p._ausencias_definitivas("a", [ayer]) == []


def test_un_dia_viejo_si_se_anota_como_definitivo():
    p = _pipe(_dcfg(missing_after_days=30))
    viejo = HOY - dt.timedelta(days=400)
    assert p._ausencias_definitivas("a", [viejo]) == [viejo]


def test_en_el_mismo_pedido_se_separan_los_viejos_de_los_recientes():
    p = _pipe(_dcfg(missing_after_days=30))
    viejo, reciente = HOY - dt.timedelta(days=90), HOY - dt.timedelta(days=3)
    assert p._ausencias_definitivas("a", [viejo, reciente]) == [viejo]


def test_el_borde_del_horizonte_cuenta_como_definitivo():
    p = _pipe(_dcfg(missing_after_days=30))
    borde = HOY - dt.timedelta(days=30)
    assert p._ausencias_definitivas("a", [borde]) == [borde]


def test_horizonte_negativo_nunca_da_nada_por_perdido():
    # el caso cfs: la fuente viene 16 meses atrasada, ningún horizonte razonable
    # alcanza, así que se re-pregunta siempre (un 404 barato por granule)
    p = _pipe(_dcfg(missing_after_days=-1))
    viejisimo = dt.date(2015, 1, 1)
    assert p._ausencias_definitivas("a", [viejisimo]) == []


def test_el_swvl1_real_no_se_hubiera_congelado():
    """Regresión del caso concreto: el 2026-08-27 el cron pidió 2026-07-01.

    Con el horizonte por defecto de era5 (lag 6 -> 36 días) ese día tenía 57 y
    se marcaba definitivo. Con el override de swvl1 queda pendiente.
    """
    dcfg = _dcfg(lag_days=6, variables={"swvl1": VariableCfg(missing_after_days=150)})
    p = _pipe(dcfg)
    hace57 = HOY - dt.timedelta(days=57)
    assert p._ausencias_definitivas("swvl1", [hace57]) == []


# ------------------------------------------------------------ retry-missing
class _LedgerStore:
    def __init__(self, led):
        self._led = led

    def read_ledger(self, v):
        return self._led


def _plan_dates(retry, led):
    """Los días que plan() consideraría pendientes, con y sin --retry-missing."""
    dcfg = _dcfg(start=dt.date(2024, 1, 1), lag_days=0,
                 variables={"a": VariableCfg()})
    p = _pipe(dcfg, retry_missing=retry)
    p.store = _LedgerStore(led)
    p.source = type("S", (), {"granules": staticmethod(lambda vs, ds: list(ds))})()
    plan = ZarrPipeline.plan(p, ["a"], dt.date(2024, 1, 1), dt.date(2024, 1, 5))
    return [str(d) for d in plan.missing["a"]]


def test_sin_retry_missing_lo_marcado_se_saltea():
    led = {"written": ["2024-01-01"], "missing": ["2024-01-02", "2024-01-03"]}
    assert _plan_dates(False, led) == ["2024-01-04", "2024-01-05"]


def test_con_retry_missing_se_vuelve_a_pedir_lo_marcado():
    led = {"written": ["2024-01-01"], "missing": ["2024-01-02", "2024-01-03"]}
    assert _plan_dates(True, led) == ["2024-01-02", "2024-01-03",
                                      "2024-01-04", "2024-01-05"]


def test_retry_missing_no_vuelve_a_bajar_lo_ya_escrito():
    """La diferencia con --overwrite: lo escrito se respeta."""
    led = {"written": ["2024-01-01", "2024-01-04"], "missing": ["2024-01-02"]}
    assert "2024-01-01" not in _plan_dates(True, led)
    assert "2024-01-04" not in _plan_dates(True, led)
