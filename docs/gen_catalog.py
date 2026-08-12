"""Genera un catálogo de lo que hay en s3://suyana-tiles para compartir con el equipo.

Lee la config del módulo + la cobertura REAL (ledgers) de cada store en S3 y escribe,
en docs/:  catalog.csv (tabla plana / dataframe), catalog.yaml (estructurado),
CATALOG.md (resumen legible). Correr desde la raíz del repo:

    PYTHONNOUSERSITE=1 python docs/gen_catalog.py
"""
import csv, datetime as dt
from collections import OrderedDict
from pathlib import Path

import xarray as xr
import yaml

from tilegen.config import load_global, load_scenes, list_datasets, load_dataset
from tilegen.utils import daterange
from tilegen.zarrstore import ZarrStore

CONFIG_DIR = Path("config")
OUT = Path("docs")
HOY = dt.date.today()

gcfg = load_global(CONFIG_DIR)
scenes = load_scenes(CONFIG_DIR)
bucket = f"s3://{gcfg.s3.bucket}" + (f"/{gcfg.s3.prefix}" if gcfg.s3.prefix else "")


def resolucion(dcfg):
    for name in scenes:
        st = ZarrStore(gcfg, dcfg, name, local_only=False, out_dir=None)
        if st.exists():
            with xr.open_zarr(st.mapper(), consolidated=True) as ds:
                return round(abs(float(ds.latitude[1] - ds.latitude[0])), 3)
    return None


def cobertura(store, dcfg, v):
    led = store.read_ledger(v)
    w = led["written"]
    if not w:
        return None
    d0, d1 = dt.date.fromisoformat(w[0]), dt.date.fromisoformat(w[-1])
    gaps = {str(d) for d in daterange(d0, d1)} - set(w) - set(led["missing"])
    atraso = (HOY - dt.timedelta(days=dcfg.lag_days) - d1).days
    return dict(first=w[0], last=w[-1], n_days=len(w), gaps=len(gaps),
                status="completo" if not gaps and atraso <= 90 else "parcial")


rows, cat = [], OrderedDict()
for dsname in list_datasets(CONFIG_DIR):
    dcfg = load_dataset(CONFIG_DIR, dsname)
    res = resolucion(dcfg)
    presentes = OrderedDict()
    for name, scfg in scenes.items():
        store = ZarrStore(gcfg, dcfg, name, local_only=False, out_dir=None)
        if not store.exists():
            continue
        vcov = OrderedDict()
        for v in dcfg.variables:
            cov = cobertura(store, dcfg, v)
            if cov is None:
                continue
            vcov[v] = cov
            rows.append(OrderedDict(
                dataset=dsname, version=dcfg.version, variable=v,
                units=dcfg.variables[v].units or "", scene=name, bbox=scfg.bbox,
                resolution_deg=res, first_date=cov["first"], last_date=cov["last"],
                n_days=cov["n_days"], gaps=cov["gaps"], status=cov["status"],
                s3_zarr=f"{bucket}/{dsname}/{dcfg.version}/{name}.zarr"))
        if vcov:
            presentes[name] = dict(bbox=scfg.bbox, variables=vcov)
    if presentes:
        cat[dsname] = OrderedDict(
            version=dcfg.version, description=dcfg.description, source=dcfg.source,
            temporal=dcfg.temporal, configured_range=f"{dcfg.start} -> {dcfg.end or 'hoy'}",
            resolution_deg=res,
            variables={v: (dcfg.variables[v].units or "") for v in dcfg.variables},
            s3_path_template=f"{bucket}/{dsname}/{dcfg.version}/<scene>.zarr",
            scenes=presentes)

# --- CSV (dataframe) ---
with open(OUT / "catalog.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    for r in rows:
        r = dict(r); r["bbox"] = ",".join(str(x) for x in r["bbox"])
        w.writerow(r)

# --- YAML (estructurado) ---
def _plain(o):
    if isinstance(o, dict): return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [_plain(x) for x in o]
    return o
with open(OUT / "catalog.yaml", "w") as f:
    f.write(f"# Catálogo de datos en {bucket} — generado {HOY}\n")
    yaml.safe_dump(_plain(cat), f, allow_unicode=True, sort_keys=False, width=100)

# --- Markdown (resumen legible) ---
def span(ds):
    firsts = [c["first"] for s in ds["scenes"].values() for c in s["variables"].values()]
    lasts = [c["last"] for s in ds["scenes"].values() for c in s["variables"].values()]
    return (min(firsts), max(lasts)) if firsts else ("-", "-")

with open(OUT / "CATALOG.md", "w") as f:
    f.write(f"# Catálogo de datos climáticos — `{bucket}`\n\n")
    f.write(f"_Generado {HOY}. Cobertura leída de los ledgers reales en S3 "
            f"(no sólo de la config)._\n\n")
    f.write("Todos los datasets son **Zarr diarios** sobre EPSG:4326, partidos por "
            "escena (zona geográfica). Detalle completo por escena en "
            "[`catalog.csv`](catalog.csv) y [`catalog.yaml`](catalog.yaml).\n\n")
    f.write("## Cómo abrir un store\n\n```python\nimport xarray as xr\n"
            f"ds = xr.open_zarr(\"{bucket}/era5/daily-stats/peru.zarr\", "
            "consolidated=True)\nds[\"t2m_mean\"].sel(time=\"2024-01-15\").plot()\n```\n\n")
    f.write("## Datasets\n\n")
    f.write("| Dataset | Versión | Resolución | Rango con datos | Variables | Escenas |\n")
    f.write("|---|---|---|---|---|---|\n")
    for name, ds in cat.items():
        lo, hi = span(ds)
        vlist = ", ".join(f"`{v}`" for v in ds["variables"])
        f.write(f"| **{name}** | {ds['version']} | {ds['resolution_deg']}° | {lo} → {hi} "
                f"| {vlist} | {len(ds['scenes'])} |\n")
    f.write("\n")
    for name, ds in cat.items():
        f.write(f"### {name} — {ds['description']}\n\n")
        f.write(f"- **Fuente:** `{ds['source']}` · **Resolución:** {ds['resolution_deg']}° "
                f"· **Rango configurado:** {ds['configured_range']}\n")
        f.write(f"- **Variables:** " +
                ", ".join(f"`{v}` ({u or 's/u'})" for v, u in ds["variables"].items()) + "\n")
        f.write(f"- **Ruta S3:** `{ds['s3_path_template']}`\n")
        f.write(f"- **Escenas con datos ({len(ds['scenes'])}):** " +
                ", ".join(ds["scenes"]) + "\n\n")

print(f"escrito: {OUT}/catalog.csv, {OUT}/catalog.yaml, {OUT}/CATALOG.md")
print(f"datasets={list(cat)}  filas={len(rows)}")
