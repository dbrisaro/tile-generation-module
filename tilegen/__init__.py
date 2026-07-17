"""tilegen — download climate datasets and publish them to S3 as a fixed tile grid of COGs."""

__version__ = "0.1.0"

from .config import load_dataset, load_global
from .grid import Tile, TileGrid
from .pipeline import Pipeline

__all__ = ["Tile", "TileGrid", "Pipeline", "load_global", "load_dataset"]
