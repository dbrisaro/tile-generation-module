"""Command line interface.

    tilegen datasets
    tilegen plan -d chirps -s 2024-01-01 -e 2024-01-31 --bbox -82 -19 -68 1
    tilegen run  -d chirps ...            # same options as plan
    tilegen verify -d chirps
    tilegen init-bucket
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import click

from .config import list_datasets, load_dataset, load_global, load_scenes
from .pipeline import Pipeline
from .s3io import S3Store
from .utils import setup_logging
from .zarr_pipeline import ZarrPipeline

# Dentro del paquete a propósito: así el default funciona igual en un install
# editable y en uno normal (antes apuntaba a ../config, que no existe dentro de
# site-packages y dejaba al CLI sin poder listar ni un dataset).
CONFIG_DIR = Path(__file__).resolve().parent / "conf"


@click.group()
@click.option("--config-dir", type=click.Path(exists=True, path_type=Path),
              default=CONFIG_DIR, show_default=True)
@click.pass_context
def cli(ctx, config_dir):
    """Download climate datasets and publish them to S3 as tiled COGs."""
    setup_logging()
    ctx.obj = config_dir


@cli.command()
@click.pass_obj
def datasets(config_dir):
    """List configured datasets."""
    for name in list_datasets(config_dir):
        d = load_dataset(config_dir, name)
        click.echo(f"{name:10s} {d.version:14s} {d.temporal:7s} "
                   f"{d.start} -> {d.end or 'present'}  vars: {', '.join(d.variables)}")
        if d.description:
            click.echo(f"{'':10s} {d.description}")


def _common(f):
    f = click.option("--source", default=None,
                     help="Override the dataset's source (e.g. era5_edh for the "
                          "Earth Data Hub mirror instead of the CDS queue).")(f)
    f = click.option("--workers", type=int, default=None,
                     help="Parallel granules (default from config.yaml).")(f)
    f = click.option("--overwrite", is_flag=True, help="Regenerate data that already exists.")(f)
    f = click.option("--local-only", is_flag=True,
                     help="Write under the workdir instead of S3 (for testing).")(f)
    f = click.option("--format", "fmt", type=click.Choice(["zarr", "cog"]), default=None,
                     help="Output format (default from config.yaml).")(f)
    f = click.option("-x", "--scene", "scene_names", multiple=True,
                     help="Scene(s) to process (see 'tilegen scenes'); repeatable.")(f)
    f = click.option("--bbox", nargs=4, type=float, default=None,
                     metavar="MINX MINY MAXX MAXY",
                     help="cog only: lon/lat bbox, snapped outward to tile boundaries.")(f)
    f = click.option("-e", "--end", type=click.DateTime(["%Y-%m-%d"]), default=None)(f)
    f = click.option("-s", "--start", type=click.DateTime(["%Y-%m-%d"]), default=None)(f)
    f = click.option("-v", "--variable", "variables", multiple=True,
                     help="Restrict to these variables (default: all).")(f)
    f = click.option("-d", "--dataset", required=True)(f)
    return f


def _resolve_scenes(config_dir, scene_names):
    scenes = load_scenes(config_dir)
    if not scene_names:
        raise click.UsageError(
            "falta -x/--scene (se puede repetir). Ver 'tilegen scenes'.")
    unknown = [s for s in scene_names if s not in scenes]
    if unknown:
        raise click.UsageError(
            f"escena(s) desconocida(s): {', '.join(unknown)}. Ver 'tilegen scenes'.")
    return [(name, scenes[name]) for name in scene_names]


def _build(config_dir, dataset, bbox, scene_names, fmt, local_only, overwrite,
           workers, keep_local=None, source=None):
    """Return a list of pipelines: one per scene (zarr) or a single one (cog)."""
    gcfg = load_global(config_dir)
    dcfg = load_dataset(config_dir, dataset)
    if source:
        from .sources import SOURCES
        if source not in SOURCES:
            raise click.UsageError(
                f"unknown source {source!r} — available: {', '.join(SOURCES)}")
        dcfg.source = source
    fmt = fmt or gcfg.output.format
    if fmt == "zarr":
        return [ZarrPipeline(gcfg, dcfg, name, scfg.bbox,
                             local_only=local_only, overwrite=overwrite,
                             workers=workers, keep_local=keep_local)
                for name, scfg in _resolve_scenes(config_dir, scene_names)]
    return [Pipeline(gcfg, dcfg, bbox=bbox or None, local_only=local_only,
                     overwrite=overwrite, workers=workers, keep_local=keep_local)]


@cli.command()
@click.pass_obj
def scenes(config_dir):
    """List the scene map (see docs/escenas_borrador.png)."""
    for name, s in sorted(load_scenes(config_dir).items(), key=lambda kv: kv[1].num):
        click.echo(f"{s.num:3d}  {name:22s} {s.type:7s} "
                   f"[{s.bbox[0]:.0f}, {s.bbox[1]:.0f}, {s.bbox[2]:.0f}, {s.bbox[3]:.0f}]"
                   + (f"  {s.description}" if s.description else ""))


@cli.command()
@_common
@click.pass_obj
def plan(config_dir, dataset, variables, start, end, bbox, scene_names, fmt,
         local_only, overwrite, workers, source):
    """Show what a run would do, without downloading anything."""
    for p in _build(config_dir, dataset, bbox, scene_names, fmt,
                    local_only, overwrite, workers, source=source):
        pl = p.plan(variables or None, start.date() if start else None,
                    end.date() if end else None)
        tag = f"[{p.scene}] " if hasattr(p, "scene") else ""
        click.echo(tag + pl.summary)
        for g in pl.granules[:8]:
            click.echo(f"  {g.key}")
        if len(pl.granules) > 8:
            click.echo(f"  ... and {len(pl.granules) - 8} more")


@cli.command()
@_common
@click.option("--keep-local", is_flag=True, help="Keep downloaded source files in the workdir.")
@click.pass_obj
def run(config_dir, dataset, variables, start, end, bbox, scene_names, fmt,
        local_only, overwrite, workers, keep_local, source):
    """Fetch, process and upload everything missing in the requested range."""
    failed = 0
    for p in _build(config_dir, dataset, bbox, scene_names, fmt, local_only,
                    overwrite, workers, keep_local=True if keep_local else None,
                    source=source):
        pl = p.plan(variables or None, start.date() if start else None,
                    end.date() if end else None)
        tag = f"[{p.scene}] " if hasattr(p, "scene") else ""
        click.echo(tag + pl.summary)
        if not pl.granules:
            click.echo(tag + "nothing to do")
            continue
        summary = p.run(pl)
        click.echo(json.dumps(summary, indent=2, default=str))
        failed += summary["stats"]["errors"]
    if failed:
        raise SystemExit(1)


@cli.command()
@click.option("-d", "--dataset", default=None,
              help="Restrict to one dataset (default: all configured).")
@click.option("-v", "--variable", "variables", multiple=True)
@click.option("-x", "--scene", "scene_names", multiple=True,
              help="Restrict to these scenes (default: every scene with data in S3).")
@click.option("--format", "fmt", type=click.Choice(["zarr", "cog"]), default=None)
@click.pass_obj
def verify(config_dir, dataset, variables, scene_names, fmt):
    """Summarize what is in S3: coverage per variable, gaps.

    With no options it walks every configured dataset and every scene that
    has data in the bucket — the "what do we actually have" catalog.
    """
    gcfg = load_global(config_dir)
    if (fmt or gcfg.output.format) == "zarr":
        from .zarrstore import ZarrStore
        from .utils import daterange
        import datetime as dt
        def _rangos(dias):
            """Comprime días ISO consecutivos: ['1982-01-01', ..] -> '1982-01-01 → 1984-12-31, ...'"""
            grupos, run, prev = [], [], None
            for s in dias:
                d = dt.date.fromisoformat(s)
                if prev is None or (d - prev).days != 1:
                    if run:
                        grupos.append(run)
                    run = []
                run.append(s)
                prev = d
            if run:
                grupos.append(run)
            partes = [g[0] if len(g) == 1 else f"{g[0]} → {g[-1]}" for g in grupos]
            return ", ".join(partes[:3]) + ("…" if len(partes) > 3 else "")

        hoy = dt.date.today()
        pendientes = []  # (texto, comando sugerido)
        for dsname in ([dataset] if dataset else list_datasets(config_dir)):
            dcfg = load_dataset(config_dir, dsname)
            if scene_names:
                names = [n for n, _ in _resolve_scenes(config_dir, scene_names)]
            else:  # discover scenes with data from the ledgers in S3
                probe = ZarrStore(gcfg, dcfg, "_", local_only=False, out_dir=None)
                ledger_root = probe.ledger_base.rsplit("/", 1)[0]
                names = sorted({p.rsplit("/", 2)[1]
                                for p in probe.fs.glob(f"{ledger_root}/*/*.json")})
            click.echo()
            click.secho(f"{dsname}/{dcfg.version}", bold=True)
            if not names:
                click.secho("  (nada en S3 todavía)", dim=True)
                continue
            vs = list(variables or dcfg.variables)
            ancho = max(len(v) for v in vs)
            # para huecos históricos grandes conviene el mirror EDH, no la cola del CDS
            edh = " --source era5_edh" if getattr(dcfg, "edh_store", None) else ""
            for name in names:
                store = ZarrStore(gcfg, dcfg, name, local_only=False, out_dir=None)
                click.echo(f"  {name}  " + click.style(f"({store.uri})", dim=True))
                for v in vs:
                    led = store.read_ledger(v)
                    w = led["written"]
                    etiqueta = f"    {v:<{ancho}}  "
                    donde = f"{dsname} · {name} · {v}"
                    if not w:
                        click.echo(etiqueta + click.style("✗ sin datos", fg="red"))
                        pendientes.append((f"{donde}: sin datos",
                                           f"tilegen run -d {dsname}{edh} -v {v} -x {name} "
                                           f"(backfill completo: ver README)"))
                        continue
                    d0, d1 = dt.date.fromisoformat(w[0]), dt.date.fromisoformat(w[-1])
                    expected = {str(d) for d in daterange(d0, d1)}
                    gaps = sorted(expected - set(w) - set(led["missing"]))
                    publicado = hoy - dt.timedelta(days=dcfg.lag_days)
                    atraso = (publicado - d1).days
                    estados, notas = [], []
                    if gaps:
                        estados.append(f"faltan {len(gaps)} días en el medio")
                        notas.append(f"huecos: {_rangos(gaps)}")
                        pendientes.append((f"{donde}: faltan {len(gaps)} días",
                                           f"tilegen run -d {dsname}{edh} -v {v} -x {name} "
                                           f"-s {gaps[0]} -e {gaps[-1]}"))
                    if atraso > 90:
                        # no es un atraso del cron: falta el grueso del histórico
                        estados.append(f"incompleto: llega solo hasta {d1}")
                        pendientes.append((f"{donde}: incompleto, llega solo hasta {d1}",
                                           f"tilegen run -d {dsname}{edh} -v {v} -x {name} "
                                           f"-s {d1 + dt.timedelta(days=1)}"))
                    elif atraso > 3:
                        estados.append(f"atrasado {atraso} días")
                        notas.append(f"la fuente ya publica hasta ~{publicado}")
                        pendientes.append((f"{donde}: atrasado {atraso} días",
                                           f"tilegen run -d {dsname} -v {v} -x {name} "
                                           f"-s {d1 + dt.timedelta(days=1)}"))
                    if estados:
                        linea = click.style("⚠ " + " · ".join(estados), fg="yellow")
                    else:
                        linea = click.style("✔ completo y al día", fg="green")
                    linea += f"  {w[0]} → {w[-1]}  ({len(w)} días)"
                    if notas:
                        linea += click.style("  · " + " · ".join(notas), fg="yellow")
                    if led["missing"]:
                        linea += click.style(
                            f"  · {len(led['missing'])} días no existen en la fuente (ok)", dim=True)
                    click.echo(etiqueta + linea)
        click.echo()
        if pendientes:
            click.secho(f"⚠ {len(pendientes)} pendiente(s):", fg="yellow", bold=True)
            for texto, comando in pendientes:
                click.echo(f"  · {texto}")
                click.secho(f"      → {comando}", dim=True)
        else:
            click.secho("✔ todo completo y al día", fg="green", bold=True)
        return
    if not dataset:
        raise click.UsageError("--format cog requires -d/--dataset")
    dcfg = load_dataset(config_dir, dataset)
    store = S3Store(gcfg.s3.bucket, gcfg.s3.region, gcfg.s3.prefix)
    for v in (variables or list(dcfg.variables)):
        keys = list(store.list_keys(f"{dcfg.name}/{dcfg.version}/{v}/"))
        if not keys:
            click.echo(f"{v}: no objects")
            continue
        per_date = Counter(Path(k).stem.split("_")[-2] for k in keys)
        counts = sorted(per_date.values())
        click.echo(f"{v}: {len(keys)} tiles, {len(per_date)} dates "
                   f"({min(per_date)} -> {max(per_date)}), "
                   f"tiles/date {counts[0]}-{counts[-1]}")
        full = counts[-1]
        gaps = sorted(d for d, c in per_date.items() if c < full)
        if gaps:
            click.echo(f"   dates with fewer tiles than usual: {', '.join(gaps[:10])}"
                       + (" ..." if len(gaps) > 10 else ""))


@cli.command()
@click.option("-d", "--dataset", default=None,
              help="Restrict to one dataset (default: all configured).")
@click.option("--format", "fmt", type=click.Choice(["zarr", "cog"]), default=None)
@click.pass_obj
def catalog(config_dir, dataset, fmt):
    """Compact roll-up of every dataset/variable: config + real S3 coverage.

    One line per (dataset, variable): the configured range and grid resolution,
    then how many of the configured scenes are complete / partial / empty in S3,
    and the span of dates actually present. The high-level companion to
    'verify', which lists the per-scene detail.
    """
    import datetime as dt

    from .utils import daterange
    from .zarrstore import ZarrStore

    gcfg = load_global(config_dir)
    if (fmt or gcfg.output.format) != "zarr":
        raise click.UsageError("catalog sólo soporta el formato zarr")
    hoy = dt.date.today()
    all_scenes = load_scenes(config_dir)

    def _resolucion(dcfg):
        """Deriva la resolución (grados) del primer store que exista."""
        import xarray as xr
        for name in all_scenes:
            store = ZarrStore(gcfg, dcfg, name, local_only=False, out_dir=None)
            if store.exists():
                with xr.open_zarr(store.mapper(), consolidated=True) as ds:
                    return f"{abs(float(ds.latitude[1] - ds.latitude[0])):.2f}°"
        return "?"

    def _estado_escena(store, dcfg, v):
        """Clasifica una (escena, variable) en ok | parcial | vacío (ver verify)."""
        led = store.read_ledger(v)
        w = led["written"]
        if not w:
            return "vacio", None, None
        d0, d1 = dt.date.fromisoformat(w[0]), dt.date.fromisoformat(w[-1])
        gaps = {str(d) for d in daterange(d0, d1)} - set(w) - set(led["missing"])
        atraso = (hoy - dt.timedelta(days=dcfg.lag_days) - d1).days
        return ("ok" if not gaps and atraso <= 90 else "parcial"), w[0], w[-1]

    click.echo()
    click.secho("Leyenda: ✔ completo y al día · ⚠ parcial/atrasado · ✗ sin datos "
                "(nino34 es índice oceánico: sin CHIRPS/CHIRTS)", dim=True)
    for dsname in ([dataset] if dataset else list_datasets(config_dir)):
        dcfg = load_dataset(config_dir, dsname)
        rango = f"{dcfg.start} → {dcfg.end or 'hoy'}"
        click.echo()
        click.secho(f"{dsname}/{dcfg.version}  ·  {rango}  ·  {_resolucion(dcfg)}", bold=True)
        if dcfg.description:
            click.secho(f"  {dcfg.description}", dim=True)
        ancho = max(len(v) for v in dcfg.variables)
        for v in dcfg.variables:
            c, lo, hi = Counter(), None, None
            for name, scfg in all_scenes.items():
                store = ZarrStore(gcfg, dcfg, name, local_only=False, out_dir=None)
                st, a, b = _estado_escena(store, dcfg, v)
                # una escena índice oceánica sin store no aplica a este dataset
                # (p.ej. nino34 no tiene CHIRPS/CHIRTS) — no la contamos como falta
                if st == "vacio" and scfg.type == "indice" and not store.exists():
                    st = "na"
                c[st] += 1
                lo = a if a and (lo is None or a < lo) else lo
                hi = b if b and (hi is None or b > hi) else hi
            aplica = c["ok"] + c["parcial"] + c["vacio"]
            if c["ok"] == aplica:
                estado = click.style("completo", fg="green")
            elif c["ok"] or c["parcial"]:
                estado = click.style("en progreso", fg="yellow")
            else:
                estado = click.style("pendiente", fg="red")
            span = click.style(f"  {lo} → {hi}", dim=True) if lo else ""
            click.echo(f"  {v:<{ancho}}  "
                       + click.style(f"✔{c['ok']:>2}", fg="green") + "  "
                       + click.style(f"⚠{c['parcial']:>2}", fg="yellow") + "  "
                       + click.style(f"✗{c['vacio']:>2}", fg="red")
                       + f"  de {aplica} zonas  [{estado}]" + span)


@cli.command("init-bucket")
@click.pass_obj
def init_bucket(config_dir):
    """Create the destination bucket if it does not exist (private by default)."""
    gcfg = load_global(config_dir)
    store = S3Store(gcfg.s3.bucket, gcfg.s3.region, gcfg.s3.prefix)
    created = store.ensure_bucket()
    click.echo(f"s3://{gcfg.s3.bucket} " + ("created" if created else "already exists"))


if __name__ == "__main__":
    cli()
