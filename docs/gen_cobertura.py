"""Genera docs/cobertura.html: la matriz escena x variable de lo que hay en S3.

Complementa gen_catalog.py. Ese escribe el catálogo (qué datasets existen, con qué
rango); este dibuja la cobertura como matriz visual para compartir con el equipo:
una celda por (dataset, variable, escena), verde/amarillo/gris según los ledgers
REALES del bucket. Correr desde la raíz del repo:

    PYTHONNOUSERSITE=1 python docs/gen_cobertura.py

A diferencia de gen_catalog.py, acá se listan TODAS las combinaciones configuradas
(incluidas las que nunca se escribieron), porque el gris "sin datos" es justamente
lo que hay que ver.
"""
import datetime as dt
import html
from pathlib import Path

from tilegen.config import load_global, load_scenes, list_datasets, load_dataset
from tilegen.utils import daterange
from tilegen.zarrstore import ZarrStore

CONFIG_DIR = Path("config")
OUT = Path("docs/cobertura.html")
HOY = dt.date.today()
AHORA = dt.datetime.now()

# Días de gracia sobre el lag_days propio de cada fuente antes de llamarla atrasada.
GRACIA = 7

# Etiqueta corta de cada dataset para el encabezado de grupo. A mano porque las
# description: de los YAML son párrafos, no títulos de columna. Si falta una, cae
# a la primera oración de la description.
ETIQUETA = {
    "cfs":       "CFS operational analysis · NOAA NCEI · 0.20°",
    "cfsr":      "CFS reanalysis · NCAR GDEX · 0.31°",
    "chirps":    "CHIRPS v2.0 · UCSB CHC · 0.05°",
    "chirps-v3": "CHIRPS v3.0 · UCSB CHC · 0.05°",
    "chirts":    "CHIRTS-daily v1.0 · UCSB CHC · 0.05°",
    "cpc":       "CPC Global Temp V1.0 · NOAA PSL · 0.50°",
    "era5":      "ERA5 daily statistics · Copernicus · 0.25°",
    "era5-land": "ERA5-Land daily statistics · Copernicus · 0.10° (solo tierra)",
}

# Notas editoriales: el por qué de cada fuente. Se mantienen a mano — los ledgers
# dicen QUÉ falta, no por qué. Revisar al regenerar.
NOTAS = {
    "cfs": "La fuente no estaba muerta: NCEI la <b>mudó</b> en agosto 2026. El árbol viejo "
           "devuelve 404 hasta en el directorio raíz, pero deja un <code>readme.txt</code> que "
           "apunta al object store nuevo, con el mismo archivo y el mismo layout. Corregida la "
           "URL el 3-sep, se recuperaron los meses que se habían perdido y las tres variables "
           "cierran parejas en 2026-03-31. Lo que sigue marcado como «faltan en origen» ahora "
           "es cierto: NCEI mismo no tiene 2018-09, 2025-03, ni nada entre 2025-05 y hoy salvo "
           "un 2026-03 suelto (más 2016-01 y 2021-04, sólo viento). O sea el atraso es de "
           "ellos, no nuestro. Sigue siendo la única fuente de una sola escena: bolivia.",
    "cfsr": "El dataset está configurado desde junio 2026 pero nunca se corrió: sigue sin "
            "existir el prefijo <code>cfsr/</code> en el bucket. Es un archivo cerrado "
            "1979–2010, así que es un backfill de una sola vez.",
    "chirps": "La fuente más al día del catálogo: entra completa al cron diario (las 20 escenas "
              "terrestres en ~2 minutos, porque es HTTP/GeoTIFF y no pasa por ninguna cola).",
    "chirps-v3": "Backfill <b>terminado</b> el 11-sep-2026: 1981 al presente de la fuente en las "
                 "20 escenas terrestres. Había estado pausado desde el 23-jul para darle ancho de "
                 "banda a otras fuentes, y volvió a arrancar el 2-sep cuando <code>chirts</code> "
                 "terminó. <code>precip_sat</code> arranca en 2000 porque depende de IMERG, así que "
                 "los 19 años anteriores en blanco no son un hueco: esa serie no existe antes. "
                 "Desde el 21-sep entra al cron diario con la misma ventana de 70 días que v2. "
                 "Ojo con las escenas en amarillo: son huecos de 2 a 4 días sueltos, del orden de "
                 "días que UCSB nunca publicó, no de una descarga a medias.",
    "chirts": "Terminado el 2-sep-2026, y era el hueco más grande que tenía el catálogo: "
              "1983–2016 completo, <code>tmax</code> y <code>tmin</code>, en las 20 escenas "
              "terrestres y sin días sueltos. Es un archivo cerrado — no entra al cron diario "
              "porque no hay nada nuevo que traer.",
    "cpc": "Backfill terminado el 27-ago-2026: 1979 al presente en las 20 escenas terrestres y "
           "sin un solo hueco. Entra al cron diario <b>recién desde el 21-sep-2026</b>: el backfill "
           "había dejado la serie completa pero nadie pedía los días nuevos, así que estuvo tres "
           "semanas atrasándose un día por día sin que fallara nada. Publica con 3 días de retraso "
           "y se le pide una ventana de 45, que cubre cualquier corrida perdida. Es la única "
           "fuente de tmax/tmin que llega al presente: <code>chirts</code> tiene 10x más "
           "resolución pero termina en 2016. A cambio son 0.5°, y como interpola estaciones no "
           "tiene océano — en las escenas costeras una parte de los píxeles va a quedar en NaN "
           "siempre, y eso es la fuente siendo honesta, no un fallo de descarga. Se lee por "
           "OPeNDAP, que recorta del lado del servidor.",
    "era5": "El núcleo del pipeline y lo más sano que hay: <code>t2m_mean</code>, "
            "<code>t2m_max</code>, <code>t2m_min</code>, <code>precip_sum</code> y "
            "<code>ssrd_sum</code> están de 1981 en adelante, sin huecos interiores, en las 21 "
            "escenas, y desde el 19-ago entran todas al cron diario partidas en 3 grupos "
            "escalonados. El costo es el tiempo: cada escena espera 10–35 min por variable en la "
            "cola del CDS, así que un grupo de 7 escenas pasa las 4 horas. Las dos columnas "
            "amarillas son las que todavía no llegaron a ese estado. <code>swvl1</code> sólo "
            "existe en el mirror EDH, que se actualiza una vez por mes, así que siempre va a "
            "ir un par de meses atrás. Pero el 3-sep se descubrió que además estaba "
            "<b>congelada</b>: el cron le pedía a EDH días que todavía no había publicado y "
            "los anotaba como inexistentes, con lo cual no los volvía a pedir nunca. Corría "
            "sin errores y no avanzaba. Arreglado en el pipeline y recuperado a mano. <code>fg10_max</code> (ráfaga máxima) "
            "cerró su backfill el 4-sep-2026 y sí está en el cron diario. Arranca en <b>1995 por "
            "decisión</b>, no por una falla: el blanco 1981-1994 no se pidió nunca. Las tres escenas "
            "en amarillo son al revés — tienen días sueltos de 1981 de una prueba de humo, que es lo "
            "que hace que el hueco figure como interior. <code>sd_mean</code>/<code>rsn_mean</code> "
            "(nieve) se agregaron el 4-sep; en <code>patagonia</code> <code>sd_mean</code> todavía "
            "muestra una década sin cubrir.",
    "era5-land": "Existe por una sola razón: ERA5 no publica cobertura de nieve, y ésta es la "
                 "única fuente que la tiene. Va a 0.1° y solo tierra, contra los 0.25° de "
                 "<code>era5</code> — grillas distintas, por eso es un dataset aparte y no una "
                 "variable más. El backfill 1981–2026-07 cerró el 9-sep y entró al cron diario el "
                 "21-sep. Las 20 escenas figuran en amarillo por un hueco de agosto, no por el "
                 "borde: al 21-sep el CDS servía septiembre completo pero de agosto sólo el 10 y "
                 "el 11. Esos 29 días quedan anotados como <b>provisorios</b>, no como "
                 "inexistentes, así que cada corrida los vuelve a pedir y entran solos el día que "
                 "la fuente los publique. Por eso la ventana del cron es de 120 días y no de 16: "
                 "un día sólo se re-pregunta mientras siga cayendo dentro de la ventana.",
}


def miles(n):
    """16661 -> '16.661' (separador de miles castellano)."""
    return f"{n:,}".replace(",", ".")


# Desde cuándo QUEREMOS tener cada fuente, que no siempre es el `start:` del YAML.
# ERA5 declara 1940 porque es lo que Copernicus ofrece, pero el backfill acordado
# arranca en 1981 (para que empareje con CHIRPS). Sin esta distinción, medio siglo
# que nunca pensamos bajar aparecería como un hueco gigante en cada celda de era5.
# Solo afecta a la línea de tiempo y al eje; el estado completo/parcial no la usa.
OBJETIVO = {"era5": dt.date(1981, 1, 1)}


def desde(stem, dcfg):
    return max(dcfg.start, OBJETIVO.get(stem, dcfg.start))


def esperado(dcfg):
    """Última fecha que la fuente debería tener publicada hoy."""
    e = HOY - dt.timedelta(days=dcfg.lag_days)
    return min(e, dcfg.end) if dcfg.end else e


def serie_anual(w, missing, stem, dcfg):
    """Nivel de llenado por año, sobre el eje común AÑO0..AÑO1.

    Un caracter por año: 'f' lleno, 'p' a medias, 'e' vacío, 'x' fuera del rango
    que el dataset declara (no es un hueco: ese año no le corresponde).

    Los días que la fuente NO publicó (`missing`) cuentan como llenos: el cubo
    está tan completo como puede estar. Sin esto, CHIRPS aparecería agujereado
    en cada día que UCSB nunca sacó.
    """
    lo_ds, hi_ds = desde(stem, dcfg), esperado(dcfg)
    tengo = {}
    for iso in w:
        tengo[int(iso[:4])] = tengo.get(int(iso[:4]), 0) + 1
    for iso in missing:
        if lo_ds <= dt.date.fromisoformat(iso) <= hi_ds:
            tengo[int(iso[:4])] = tengo.get(int(iso[:4]), 0) + 1
    out = []
    for y in range(ANIO0, ANIO1 + 1):
        lo, hi = max(dt.date(y, 1, 1), lo_ds), min(dt.date(y, 12, 31), hi_ds)
        if lo > hi:
            out.append("x")
            continue
        frac = tengo.get(y, 0) / ((hi - lo).days + 1)
        out.append("f" if frac >= 0.995 else "e" if frac <= 0 else "p")
    return "".join(out)


def tramos(serie):
    """'xxffpppee' -> [(1983, 1990), ...]: rangos de años NO llenos, para el tooltip."""
    out, ini = [], None
    for i, ch in enumerate(serie + "f"):
        if ch in "pe" and ini is None:
            ini = ANIO0 + i
        elif ch not in "pe" and ini is not None:
            out.append((ini, ANIO0 + i - 1))
            ini = None
    return out


def cobertura(store, stem, dcfg, v):
    """None si no hay nada escrito; si no, el estado de esa celda."""
    led = store.read_ledger(v)
    w = led["written"]
    if not w:
        return None
    d0, d1 = dt.date.fromisoformat(w[0]), dt.date.fromisoformat(w[-1])
    huecos = len({str(d) for d in daterange(d0, d1)} - set(w) - set(led["missing"]))
    atraso = (esperado(dcfg) - d1).days
    serie = serie_anual(w, led["missing"], stem, dcfg)
    return dict(first=w[0], last=w[-1], n_days=len(w), huecos=huecos, atraso=atraso,
                serie=serie, tramos=tramos(serie),
                estado="completo" if not huecos and atraso <= GRACIA else "parcial")


# ---------------------------------------------------------------- relevamiento
gcfg = load_global(CONFIG_DIR)
scenes = load_scenes(CONFIG_DIR)

# Se indexa por el STEM del YAML, no por dcfg.name: chirps.yaml y chirps-v3.yaml
# declaran los dos `name: chirps` (se distinguen por version), así que usar el
# name fusionaría las dos columnas. ZarrStore igual resuelve la ruta con el dcfg.
datasets = [(s, load_dataset(CONFIG_DIR, s)) for s in list_datasets(CONFIG_DIR)]
# columnas: [(stem, dcfg, variable), ...] en orden de dataset y de YAML
cols = [(s, d, v) for s, d in datasets for v in d.variables]

# Eje temporal COMÚN a toda la matriz. Que cada celda use el mismo eje es el punto:
# así una franja vertical de la tabla es el mismo año en todas las escenas, y se
# puede leer de un vistazo si un hueco es viejo (backfill) o reciente (el cron).
ANIO0 = min(desde(s, d).year for s, d in datasets)
ANIO1 = max(esperado(d).year for _, d in datasets)

celdas = {}   # (stem, variable, escena) -> cov | None
for stem, dcfg in datasets:
    for scene in scenes:
        store = ZarrStore(gcfg, dcfg, scene, local_only=False, out_dir=None)
        existe = store.exists()
        for v in dcfg.variables:
            celdas[(stem, v, scene)] = cobertura(store, stem, dcfg, v) if existe else None
    print(f"  relevado {stem}")

vals = list(celdas.values())
n_tot = len(vals)
n_ok = sum(1 for c in vals if c and c["estado"] == "completo")
n_mid = sum(1 for c in vals if c and c["estado"] == "parcial")
n_nil = n_tot - n_ok - n_mid
dias = sum(c["n_days"] for c in vals if c)

# ------------------------------------------------------------------- matriz
GLIFO = {"completo": "✓", "parcial": "◐", "vacio": "·"}
TINTA = {"f": "var(--ok)", "p": "var(--mid)", "e": "var(--gap)", "x": "transparent"}


def gradiente(serie):
    """La serie por año -> un linear-gradient de topes duros, un tramo por año.

    Se comprimen los años consecutivos del mismo color en un solo par de topes:
    una celda típica son 2 o 3 tramos, no 48. Sin esto el HTML pesa ~4x.
    """
    n, paradas, i = len(serie), [], 0
    while i < n:
        j = i
        while j + 1 < n and serie[j + 1] == serie[i]:
            j += 1
        paradas.append(f"{TINTA[serie[i]]} {i / n:.4%} {(j + 1) / n:.4%}")
        i = j + 1
    return "linear-gradient(90deg," + ",".join(paradas) + ")"

filas = []
for scene, scfg in scenes.items():
    tds = []
    prev_ds = None
    for stem, dcfg, v in cols:
        cov = celdas[(stem, v, scene)]
        estado = cov["estado"] if cov else "vacio"
        tip = [f"{stem} · {v} — {scene}"]
        if cov:
            tip.append(f"{cov['first']} → {cov['last']}")
            tip.append(f"{miles(cov['n_days'])} días escritos")
            tip.append(f"esperado hasta {esperado(dcfg)}")
            if cov["huecos"]:
                tip.append(f"{cov['huecos']} días sueltos sin escribir")
            if cov["atraso"] > 0:
                tip.append(f"{cov['atraso']} días de atraso")
            if cov["tramos"]:
                tip.append("años sin completar: " + ", ".join(
                    str(a) if a == b else f"{a}–{b}" for a, b in cov["tramos"]))
        else:
            tip.append("Sin datos en S3")
        gs = " gs" if stem != prev_ds else ""   # línea divisoria entre datasets
        prev_ds = stem
        # Fuera del rango del dataset la barra va transparente, no gris: que un año
        # "no le corresponda" a la fuente no es lo mismo que faltar.
        serie = cov["serie"] if cov else serie_anual([], [], stem, dcfg)
        tds.append(
            f'<td class="c c-{estado}{gs}" tabindex="0" data-tip="{html.escape(chr(10).join(tip))}">'
            f'<span class="g" aria-hidden="true">{GLIFO[estado]}</span>'
            f'<span class="f">{cov["last"] if cov else "—"}</span>'
            f'<span class="tl" style="background-image:{gradiente(serie)}"></span>'
            f'<span class="sr">{estado}</span></td>')
    filas.append(
        f'<tr><th class="esc" scope="row"><span class="esc-n">{scene}</span>'
        f'<span class="esc-t">{scfg.type}</span></th>' + "".join(tds) + "</tr>")

def etiqueta(stem, dcfg):
    return ETIQUETA.get(stem) or dcfg.description.split(".")[0]

th_grp = "".join(
    f'<th class="grp" colspan="{len(d.variables)}" scope="colgroup">'
    f'<span class="grp-n">{s}</span>'
    f'<span class="grp-d">{html.escape(etiqueta(s, d))}</span></th>' for s, d in datasets)
th_var = "".join(f'<th class="var" scope="col"><span>{v}</span></th>' for _, _, v in cols)
notas = "".join(
    f'<div class="nota"><h3>{s}</h3><p class="nota-d">{html.escape(etiqueta(s, d))}</p>'
    f'<p>{NOTAS.get(s, "")}</p></div>' for s, d in datasets)

CSS = (Path("docs/cobertura.css").read_text() if Path("docs/cobertura.css").exists()
       else None)
assert CSS, "falta docs/cobertura.css (el estilo de la página, separado del generador)"

OUT.write_text(f"""<title>Cobertura de cubos climáticos</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>
{CSS}</style>

<div class="wrap">
  <header>
    <p class="eyebrow">suyana · s3://{gcfg.s3.bucket}</p>
    <h1>Cobertura de cubos climáticos</h1>
    <p class="sub">Qué hay escrito en cada cubo Zarr, escena por escena y variable por
    variable. El estado sale de los ledgers reales del bucket, no de la configuración.</p>
    <p class="stamp">Leído {AHORA:%Y-%m-%d %H:%M} · {n_tot} combinaciones · {len(scenes)} escenas × {len(cols)} variables</p>
  </header>

  <section class="tiles">
    <div class="tile"><span class="l"><span class="chip chip-ok"></span>Completo</span>
      <span class="n">{n_ok}</span><span class="p">{n_ok / n_tot:.0%} · sin huecos y al día</span></div>
    <div class="tile"><span class="l"><span class="chip chip-mid"></span>A medias</span>
      <span class="n">{n_mid}</span><span class="p">{n_mid / n_tot:.0%} · con huecos o atrasado</span></div>
    <div class="tile"><span class="l"><span class="chip chip-nil"></span>Sin datos</span>
      <span class="n">{n_nil}</span><span class="p">{n_nil / n_tot:.0%} · nunca se escribió</span></div>
    <div class="tile"><span class="l">Días escritos</span>
      <span class="n">{miles(dias)}</span><span class="p">sumando escena × variable</span></div>
  </section>

  <div class="bar">
    <div class="leg">
      <div><span class="key key-ok">✓</span><span><b>Completo</b> — sin días sueltos y al día respecto de su retraso de publicación</span></div>
      <div><span class="key key-mid">◐</span><span><b>A medias</b> — hay datos, pero con huecos o atrasados</span></div>
      <div><span class="key key-nil">·</span><span><b>Sin datos</b> — no existe el cubo para esa escena</span></div>
    </div>
    <label class="toggle"><input type="checkbox" id="tg"> Mostrar última fecha</label>
  </div>

  <div class="tl-leg">
    <p>La barrita de cada celda es una <b>línea de tiempo de {ANIO0} a {ANIO1}</b>, un tramo por
    año, con el mismo eje en toda la tabla. Sirve para distinguir lo que el color solo no dice:
    un hueco <b>a la izquierda</b> es backfill pendiente; uno <b>a la derecha</b> es el cron
    atrasado. Los años que la fuente no cubre van en blanco, no en gris.</p>
    <div class="ruler" aria-hidden="true">
      <span class="tl tl-demo" style="background-image:{gradiente('x' * 6 + 'e' * 10 + 'p' * 3 + 'f' * (ANIO1 - ANIO0 - 18))}"></span>
      <span class="ru"><b>{ANIO0}</b><b>{(ANIO0 + ANIO1) // 2}</b><b>{ANIO1}</b></span>
    </div>
  </div>

  <div class="scroll">
    <table id="m">
      <thead>
        <tr><th class="esc" scope="col"><span class="esc-n">Escena</span></th>{th_grp}</tr>
        <tr><th class="esc" scope="col"><span class="esc-t">{len(scenes)} zonas</span></th>{th_var}</tr>
      </thead>
      <tbody>{"".join(filas)}</tbody>
    </table>
  </div>

  <div class="sec-h">
    <h2>Por qué cada fuente está como está</h2>
    <p>El amarillo casi nunca significa que algo falló: significa un backfill todavía en
    marcha, una fuente que publica con retraso, o una que se murió en el origen.</p>
  </div>
  <section class="notas">{notas}</section>

  <footer>
    Estado calculado contra los ledgers <code>_ledger/&lt;escena&gt;/&lt;variable&gt;.json</code> de cada cubo.<br>
    «Al día» = la última fecha escrita está dentro de los {GRACIA} días de gracia sobre el retraso
    de publicación propio de cada fuente.<br>
    La línea de tiempo se mide contra lo que <b>queremos</b> tener, no contra todo lo que la fuente
    ofrece: ERA5 llega hasta 1940 en origen, pero acá se mide desde 1981 para emparejar con CHIRPS.<br>
    Regenerar con <code>python docs/gen_cobertura.py</code>.
  </footer>
</div>

<div id="tip" role="status"></div>

<script>
const tg = document.getElementById('tg'), m = document.getElementById('m');
tg.addEventListener('change', () => m.classList.toggle('fechas', tg.checked));

const tip = document.getElementById('tip');
function show(el) {{
  tip.textContent = el.dataset.tip;
  tip.classList.add('on');
  const r = el.getBoundingClientRect(), t = tip.getBoundingClientRect();
  let x = r.left + r.width / 2 - t.width / 2;
  x = Math.max(8, Math.min(x, innerWidth - t.width - 8));
  let y = r.top - t.height - 9;
  if (y < 8) y = r.bottom + 9;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}}
function hide() {{ tip.classList.remove('on'); }}
for (const c of m.querySelectorAll('td.c')) {{
  c.addEventListener('mouseenter', () => show(c));
  c.addEventListener('mouseleave', hide);
  c.addEventListener('focus', () => show(c));
  c.addEventListener('blur', hide);
}}
</script>""", encoding="utf-8")

print(f"escrito: {OUT}")
print(f"combinaciones={n_tot}  completo={n_ok}  parcial={n_mid}  vacio={n_nil}  "
      f"dias_escritos={dias}")
