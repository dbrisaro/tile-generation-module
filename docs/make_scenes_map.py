"""Regenera docs/escenas_borrador.png a partir de config/scenes.yaml."""
from pathlib import Path

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import yaml
from matplotlib.lines import Line2D

ROOT = Path(__file__).resolve().parents[1]
SCENES = yaml.safe_load((ROOT / "config" / "scenes.yaml").read_text())["scenes"]

STYLE = {
    "P": dict(edgecolor="#245a86", facecolor="#2E6FA3", alpha_face=0.10, ls="-", lw=1.2),
    "I": dict(edgecolor="#C4622D", facecolor="none", alpha_face=0.0, ls="--", lw=1.3),
}

# posicion manual del numero cuando el centro del rectangulo queda ambiguo
LABEL_POS = {
    1: (-112, 24), 2: (-100, 16), 3: (-92, 8), 4: (-80, 24.5), 5: (-58.5, 17),
    6: (-75, 9), 7: (-66, 8.5), 8: (-53, 7), 9: (-90, -1), 10: (-86, -14),
    11: (-63, -16), 12: (-65, -5), 13: (-38, -8), 14: (-44, -19), 15: (-49, -31),
    16: (-59.5, -25.5), 17: (-54.5, -36), 18: (-73, -28), 19: (-63.5, -38),
    20: (-70, -52), 21: (-145, 0),
}


def draw_scene(ax, num, bounds, tipo):
    w, s, e, n = bounds
    st = STYLE[tipo]
    if st["alpha_face"]:
        ax.add_patch(mpatches.Rectangle(
            (w, s), e - w, n - s, transform=ccrs.PlateCarree(),
            facecolor=st["facecolor"], alpha=st["alpha_face"],
            edgecolor="none", zorder=3))
    ax.add_patch(mpatches.Rectangle(
        (w, s), e - w, n - s, transform=ccrs.PlateCarree(),
        facecolor="none", edgecolor=st["edgecolor"], lw=st["lw"],
        ls=st["ls"], zorder=4))
    lx, ly = LABEL_POS.get(num, ((w + e) / 2, (s + n) / 2))
    ax.text(lx, ly, str(num), transform=ccrs.PlateCarree(),
            ha="center", va="center", fontsize=8, color="#333333", zorder=6,
            bbox=dict(boxstyle="circle,pad=0.25", fc="white",
                      ec="#777777", lw=0.6, alpha=0.95))


def base(ax, extent):
    ax.set_extent(extent, crs=ccrs.PlateCarree())
    ax.add_feature(cfeature.LAND.with_scale("50m"), facecolor="#f2efe9", zorder=0)
    ax.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor="#fcfdfe", zorder=0)
    ax.add_feature(cfeature.COASTLINE.with_scale("50m"), lw=0.4,
                   edgecolor="#9a9a9a", zorder=1)
    ax.add_feature(cfeature.BORDERS.with_scale("50m"), lw=0.25,
                   edgecolor="#c0c0c0", zorder=1)
    ax.spines["geo"].set_linewidth(0.4)
    ax.spines["geo"].set_edgecolor("#bbbbbb")


items = [(cfg["num"], name, cfg["bbox"], "I" if cfg.get("type") == "indice" else "P")
         for name, cfg in SCENES.items()]
far_west = [num for num, _, bounds, _ in items if bounds[0] < -130]

fig = plt.figure(figsize=(6.5, 8.2))
gs = fig.add_gridspec(2, 1, height_ratios=[4.6, 1.0], hspace=0.05)

ax1 = fig.add_subplot(gs[0], projection=ccrs.PlateCarree())
base(ax1, [-126, -24, -62, 37])
for num, name, bounds, tipo in items:
    if num not in far_west:
        draw_scene(ax1, num, bounds, tipo)

ax2 = fig.add_subplot(gs[1], projection=ccrs.PlateCarree())
base(ax2, [-175, -68, -22, 12])
for num, name, bounds, tipo in items:
    if num in far_west or name in ("peru", "ecuador_galapagos"):
        draw_scene(ax2, num, bounds, tipo)

legend = [
    Line2D([], [], color="#245a86", lw=1.4,
           label="escena (pais o region, extendida al mar si hay costa)"),
    Line2D([], [], color="#C4622D", lw=1.4, ls="--",
           label="region indice sin pais (Nino 3.4)"),
]
ax1.legend(handles=legend, loc="lower left", fontsize=7.5, frameon=False)
ax2.text(0.01, 0.06, "Pacifico ecuatorial: Nino 3.4 (21); Nino 1+2 queda dentro de peru (10)",
         transform=ax2.transAxes, fontsize=7, color="#666666")

out = ROOT / "docs" / "escenas_borrador.png"
fig.savefig(out, dpi=200, bbox_inches="tight")
print(f"guardado {out}")
