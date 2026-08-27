"""Ventana compartida del source de ERA5/CDS (``cds_fetch_area``).

Lo que se fija acá es lo que hace viable un backfill por CDS: que todas las
escenas apunten al MISMO archivo descargado, así se hace un pedido por mes en
vez de uno por mes y por escena (~380 contra ~8.000 en un backfill largo).

Los dos riesgos son:
  1. que el nombre del archivo siga variando por escena -> el caché no pega y no
     se ahorra ningún pedido, que era todo el punto;
  2. que ``cds_fetch_area`` se cuele en el pedido cuando NO está configurada, y
     un backfill por escena termine bajando el planeta entero sin querer.
"""
import datetime as dt

from tilegen.sources.era5 import Era5CdsSource


class _Var:
    cds_variable = "10m_wind_gust_since_previous_post_processing"
    daily_statistic = "daily_maximum"
    units = "m s-1"


class _Cfg:
    """DatasetCfg mínima: sólo lo que mira el source."""
    name = "era5"
    cds_dataset = "derived-era5-single-levels-daily-statistics"

    def __init__(self, cds_fetch_area=None):
        self.cds_fetch_area = cds_fetch_area
        self.variables = {"fg10_max": _Var()}


VENTANA = [-170, -57, -32, 35]
PERU = (-82, -19, -68, 0)
BOLIVIA = (-70, -23, -57, -9)


def _src(tmp_path, area_config, escena):
    s = Era5CdsSource(_Cfg(area_config), tmp_path)
    s.area = escena          # lo que setea el pipeline, una por escena
    return s


def _target(src, y=1995, m=6):
    """El path que fetch() usaría, sin bajar nada."""
    area = src.fetch_area
    tag = "" if area is None else "_" + "_".join(str(int(x)) for x in area)
    return src.workdir / f"era5_fg10_max_{y}{m:02d}{tag}.nc"


def test_ventana_compartida_da_un_solo_archivo_para_todas_las_escenas(tmp_path):
    """El ahorro de pedidos depende de que el nombre NO dependa de la escena."""
    peru = _src(tmp_path, VENTANA, PERU)
    bolivia = _src(tmp_path, VENTANA, BOLIVIA)
    assert _target(peru) == _target(bolivia)
    # y la ventana pedida es la común, no la de la escena
    assert peru.fetch_area == tuple(VENTANA)
    assert bolivia.fetch_area == tuple(VENTANA)


def test_sin_ventana_compartida_cada_escena_baja_lo_suyo(tmp_path):
    """El comportamiento viejo tiene que seguir intacto para el cron diario."""
    peru = _src(tmp_path, None, PERU)
    bolivia = _src(tmp_path, None, BOLIVIA)
    assert _target(peru) != _target(bolivia)
    assert peru.fetch_area == PERU
    assert bolivia.fetch_area == BOLIVIA


def test_sin_ventana_compartida_no_se_pide_global_por_accidente(tmp_path):
    """Sin bbox de escena Y sin ventana común, area queda None = global.

    Es el contrato viejo (pedido global), pero conviene que quede explícito:
    si alguna vez `area` se deja de setear, el pedido se vuelve global y caro.
    """
    s = Era5CdsSource(_Cfg(None), tmp_path)
    assert s.fetch_area is None


def test_shared_downloads_sigue_a_la_config(tmp_path):
    """El pipeline usa esta bandera para NO borrar el archivo entre escenas.

    Si diera False con la ventana configurada, cada escena volvería a pedir el
    mes al CDS y el cambio no serviría para nada.
    """
    assert _src(tmp_path, VENTANA, PERU).shared_downloads is True
    assert _src(tmp_path, None, PERU).shared_downloads is False


def test_la_ventana_configurada_cubre_las_escenas_reales(tmp_path):
    """Si una escena se sale de la ventana, su recorte saldría vacío."""
    from tilegen.config import load_scenes
    from pathlib import Path
    escenas = load_scenes(Path("tilegen/conf"))
    minx, miny, maxx, maxy = VENTANA
    for nombre, cfg in escenas.items():
        sminx, sminy, smaxx, smaxy = cfg.bbox
        assert minx <= sminx and smaxx <= maxx, f"{nombre} se sale en longitud"
        assert miny <= sminy and smaxy <= maxy, f"{nombre} se sale en latitud"


def test_un_granulo_por_mes_no_por_dia(tmp_path):
    """El agrupado mensual es lo que mantiene la cuenta de pedidos baja."""
    src = _src(tmp_path, VENTANA, PERU)
    dias = [dt.date(1995, 6, 1) + dt.timedelta(days=i) for i in range(90)]
    gs = src.granules(["fg10_max"], dias)
    assert len(gs) == 3, [g.key for g in gs]
    assert [g.key for g in gs] == [
        "era5:fg10_max:1995-06", "era5:fg10_max:1995-07", "era5:fg10_max:1995-08"]
