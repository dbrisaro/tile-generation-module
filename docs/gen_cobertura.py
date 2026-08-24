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
    "era5":      "ERA5 daily statistics · Copernicus · 0.25°",
}

# Notas editoriales: el por qué de cada fuente. Se mantienen a mano — los ledgers
# dicen QUÉ falta, no por qué. Revisar al regenerar.
NOTAS = {
    "cfs": "NCEI retiró la ruta de esta fuente en agosto 2026: el árbol "
           "<code>operational-analysis/time-series/</code> devuelve 404. Sólo hay datos de "
           "Bolivia y quedaron congelados. Además hay 450 días marcados como «faltan en "
           "origen» que son una misclasificación vieja y hay que limpiar antes de reintentar.",
    "cfsr": "El dataset está configurado desde junio 2026 pero nunca se corrió: no existe el "
            "prefijo <code>cfsr/</code> en el bucket. Es un archivo cerrado 1979–2010, así que "
            "es un backfill de una sola vez.",
    "chirps": "La fuente más al día del catálogo: entra completa al cron diario (las 20 escenas "
              "terrestres en ~2 minutos, porque es HTTP/GeoTIFF y no pasa por ninguna cola).",
    "chirps-v3": "Backfill pausado a propósito el 23-jul para darle ancho de banda a CFS. Llegó "
                 "hasta noviembre de 1992 de 46 años. <code>precip_sat</code> arranca en 2000, "
                 "así que todavía no entró en juego.",
    "chirts": "Backfill pausado el 22-jul. Llegó hasta 1989 de un archivo que termina en 2016 — "
              "es el hueco más grande del catálogo: faltan 27 años en 20 escenas.",
    "era5": "El núcleo del pipeline y lo más sano que hay: 1981 en adelante, sin huecos "
            "interiores, y desde el 19-ago las 21 escenas entran en el cron diario, partidas en "
            "3 grupos escalonados. El costo es el tiempo: cada escena espera 10–35 min por "
            "variable en la cola del CDS, así que un grupo de 7 escenas pasa las 4 horas. "
            "<code>swvl1</code> depende de las actualizaciones mensuales del mirror EDH, así que "
            "siempre va a ir un mes atrás.",
}


def miles(n):
    """16661 -> '16.661' (separador de miles castellano)."""
    return f"{n:,}".replace(",", ".")


def esperado(dcfg):
    """Última fecha que la fuente debería tener publicada hoy."""
    e = HOY - dt.timedelta(days=dcfg.lag_days)
    return min(e, dcfg.end) if dcfg.end else e


def cobertura(store, dcfg, v):
    """None si no hay nada escrito; si no, el estado de esa celda."""
    led = store.read_ledger(v)
    w = led["written"]
    if not w:
        return None
    d0, d1 = dt.date.fromisoformat(w[0]), dt.date.fromisoformat(w[-1])
    huecos = len({str(d) for d in daterange(d0, d1)} - set(w) - set(led["missing"]))
    atraso = (esperado(dcfg) - d1).days
    return dict(first=w[0], last=w[-1], n_days=len(w), huecos=huecos, atraso=atraso,
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

celdas = {}   # (stem, variable, escena) -> cov | None
for stem, dcfg in datasets:
    for scene in scenes:
        store = ZarrStore(gcfg, dcfg, scene, local_only=False, out_dir=None)
        existe = store.exists()
        for v in dcfg.variables:
            celdas[(stem, v, scene)] = cobertura(store, dcfg, v) if existe else None
    print(f"  relevado {stem}")

vals = list(celdas.values())
n_tot = len(vals)
n_ok = sum(1 for c in vals if c and c["estado"] == "completo")
n_mid = sum(1 for c in vals if c and c["estado"] == "parcial")
n_nil = n_tot - n_ok - n_mid
dias = sum(c["n_days"] for c in vals if c)

# ------------------------------------------------------------------- matriz
GLIFO = {"completo": "✓", "parcial": "◐", "vacio": "·"}

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
        else:
            tip.append("Sin datos en S3")
        gs = " gs" if stem != prev_ds else ""   # línea divisoria entre datasets
        prev_ds = stem
        tds.append(
            f'<td class="c c-{estado}{gs}" tabindex="0" data-tip="{html.escape(chr(10).join(tip))}">'
            f'<span class="g" aria-hidden="true">{GLIFO[estado]}</span>'
            f'<span class="f">{cov["last"] if cov else "—"}</span>'
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
    <p>El amarillo casi nunca significa que algo falló: significa un backfill pausado a
    propósito, una fuente que publica con retraso, o una que se murió en el origen.</p>
  </div>
  <section class="notas">{notas}</section>

  <footer>
    Estado calculado contra los ledgers <code>_ledger/&lt;escena&gt;/&lt;variable&gt;.json</code> de cada cubo.<br>
    «Al día» = la última fecha escrita está dentro de los {GRACIA} días de gracia sobre el retraso
    de publicación propio de cada fuente.<br>
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
