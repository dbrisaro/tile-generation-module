"""El recorte del mirror EDH no puede quedarse CORTO respecto del bbox.

El bug que fija esto: ``.sel(slice(...))`` compara contra el CENTRO de la celda,
y en una grilla de 0.1° los centros no son exactos en binario. En el store real
de ERA5-Land el centro que debería ser 283.0 (= -77°) está guardado como
282.99999999999585 — arrastre de sumar 0.1 unas 2830 veces. Es menor que 283.0,
así que ``slice(283.0, 304.0)`` lo dejaba afuera.

Consecuencia: la MISMA escena salía con 2 columnas y 1 fila menos por EDH que
por CDS, y el writer rechazaba el segundo write con "asset grid does not match
store grid". O sea que cada variable quedaba casada con la fuente que hubiera
creado el cubo: si el histórico entraba por EDH, el cron por CDS no podía
actualizarlo nunca — la trampa de swvl1, pero silenciosa.

A 0.25° no pasaba: esos centros SÍ son exactos en binario (0.25 = 2^-2). Por eso
era5 nunca lo mostró y era5-land sí.

El contrato que se fija acá es "cubrir, sin pasarse de una celda": el recorte
fino lo hace clip_box() en assets.py, que va por los BORDES de la celda.
"""
import numpy as np
import pytest
import xarray as xr

from tilegen.sources.edh import Era5EdhSource


class _Cfg:
    name = "era5-land"
    variables = {}


def _grid(lon0, lat0, paso, nlon, nlat, ruido=0.0):
    """Grilla con lat DESCENDENTE, como los stores de ERA5."""
    lon = np.array([lon0 + i * paso for i in range(nlon)]) + ruido
    lat = np.array([lat0 - i * paso for i in range(nlat)]) + ruido
    return xr.DataArray(
        np.zeros((nlat, nlon), dtype="float32"),
        coords={"latitude": lat, "longitude": lon},
        dims=("latitude", "longitude"))


def _crop(da, area, tmp_path):
    s = Era5EdhSource(_Cfg(), tmp_path)
    s.area = area
    return s._crop(da)


# el bbox real de patagonia, que es donde apareció
BBOX = (-77.0, -57.0, -56.0, -40.0)
RUIDO = -4.2e-12   # el arrastre medido en el store de ERA5-Land


def test_grilla_de_una_decima_con_ruido_igual_cubre_el_bbox(tmp_path):
    """La regresión: -77.0 guardado como -77.000000000004 no puede caerse."""
    da = _grid(-80.0, -35.0, 0.1, 300, 300, ruido=RUIDO)
    out = _crop(da, BBOX, tmp_path)
    assert out.longitude.min() <= -77.0, "se comió el borde oeste"
    assert out.longitude.max() >= -56.0, "se comió el borde este"
    assert out.latitude.min() <= -57.0, "se comió el borde sur"
    assert out.latitude.max() >= -40.0, "se comió el borde norte"


def test_sin_el_colchon_el_borde_se_perdia(tmp_path):
    """Deja explícito que el ruido es el culpable, no otra cosa."""
    da = _grid(-80.0, -35.0, 0.1, 300, 300, ruido=RUIDO)
    crudo = da.sel(longitude=slice(-77.0, -56.0))   # lo que hacía antes
    assert crudo.longitude.min() > -77.0            # el borde oeste NO está
    assert _crop(da, BBOX, tmp_path).longitude.min() <= -77.0


def test_no_se_pasa_de_una_celda(tmp_path):
    """Cubrir de más es barato, pero no gratis: se baja y se descarta."""
    da = _grid(-80.0, -35.0, 0.1, 300, 300, ruido=RUIDO)
    out = _crop(da, BBOX, tmp_path)
    assert out.longitude.min() >= -77.0 - 0.1 - 1e-9
    assert out.latitude.max() <= -40.0 + 0.1 + 1e-9


def test_un_cuarto_de_grado_sigue_cubriendo(tmp_path):
    """era5 va a 0.25° y no tenía el problema: no puede empeorar."""
    da = _grid(-80.0, -35.0, 0.25, 120, 120)
    out = _crop(da, BBOX, tmp_path)
    assert out.longitude.min() <= -77.0 and out.longitude.max() >= -56.0
    assert out.latitude.min() <= -57.0 and out.latitude.max() >= -40.0


def test_el_colchon_no_se_sale_de_la_grilla(tmp_path):
    """Con el bbox pegado al borde del store, el colchón no puede dar la vuelta.

    En 0..360 pedir -0.1 no devuelve la última columna: devuelve vacío.
    """
    da = _grid(0.0, 90.0, 0.1, 3600, 1472)          # el store real, en 0..360
    out = _crop(da, (-170.0, -57.0, -32.0, 35.0), tmp_path)
    assert out.longitude.size > 0 and out.latitude.size > 0
    assert float(out.longitude.min()) >= 0.0
    assert float(out.latitude.min()) >= float(da.latitude.min())


def test_bbox_que_cruza_lon_0_sigue_rechazado(tmp_path):
    """El contrato viejo no cambia: el wrap no está soportado."""
    da = _grid(0.0, 90.0, 0.1, 3600, 1472)
    with pytest.raises(ValueError, match="crossing lon 0"):
        _crop(da, (-10.0, -20.0, 10.0, 20.0), tmp_path)
