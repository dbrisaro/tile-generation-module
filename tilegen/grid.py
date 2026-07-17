"""Fixed lat/lon tile grid, MERIT/GHSL style.

Tiles are square blocks of ``size_deg`` degrees aligned to (0, 0) and named
by their lower-left corner, e.g. ``s20w080`` covers lon [-80, -70) and
lat [-20, -10) for a 10-degree grid. The grid is identical for every dataset,
so the same tile id always refers to the same footprint on Earth.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

_ID_RE = re.compile(r"([ns])(\d{2})([ew])(\d{3})")


@dataclass(frozen=True)
class Tile:
    id: str
    minx: float
    miny: float
    maxx: float
    maxy: float

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.minx, self.miny, self.maxx, self.maxy)


class TileGrid:
    def __init__(self, size_deg: int = 10):
        if size_deg <= 0 or 180 % size_deg != 0:
            raise ValueError("size_deg must be a positive divisor of 180")
        self.size = size_deg

    def tile_id(self, lat0: float, lon0: float) -> str:
        ns = "s" if lat0 < 0 else "n"
        ew = "w" if lon0 < 0 else "e"
        return f"{ns}{abs(round(lat0)):02d}{ew}{abs(round(lon0)):03d}"

    def _tile(self, lat0: int, lon0: int) -> Tile:
        return Tile(self.tile_id(lat0, lon0), lon0, lat0, lon0 + self.size, lat0 + self.size)

    def tile_at(self, lon: float, lat: float) -> Tile:
        """The tile containing a point."""
        lon0 = math.floor(lon / self.size) * self.size
        lat0 = math.floor(lat / self.size) * self.size
        return self._tile(lat0, lon0)

    def tiles_for_bbox(self, minx: float, miny: float, maxx: float, maxy: float) -> list[Tile]:
        """All tiles intersecting a bbox (lon/lat, EPSG:4326)."""
        tiles = []
        lat0 = math.floor(miny / self.size) * self.size
        while lat0 < maxy:
            lon0 = math.floor(minx / self.size) * self.size
            while lon0 < maxx:
                tiles.append(self._tile(lat0, lon0))
                lon0 += self.size
            lat0 += self.size
        return tiles

    def all_tiles(self) -> list[Tile]:
        return self.tiles_for_bbox(-180, -90, 180, 90)

    def snap_bbox(self, bbox) -> tuple[float, float, float, float]:
        """Expand a bbox outward to tile boundaries (so tiles are never cropped)."""
        minx, miny, maxx, maxy = bbox
        s = self.size
        return (
            max(-180, math.floor(minx / s) * s),
            max(-90, math.floor(miny / s) * s),
            min(180, math.ceil(maxx / s) * s),
            min(90, math.ceil(maxy / s) * s),
        )

    def parse_id(self, tile_id: str) -> Tile:
        m = _ID_RE.fullmatch(tile_id)
        if not m:
            raise ValueError(f"invalid tile id: {tile_id!r}")
        lat0 = int(m.group(2)) * (-1 if m.group(1) == "s" else 1)
        lon0 = int(m.group(4)) * (-1 if m.group(3) == "w" else 1)
        return self._tile(lat0, lon0)
