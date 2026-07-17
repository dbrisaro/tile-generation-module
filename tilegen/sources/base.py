"""Base classes for data sources.

A *granule* is the unit of download (one HTTP file, one CDS request).
Fetching a granule yields *assets*: single-variable, single-date rasters
ready for tiling. To add a new source, subclass DataSource, implement
``granules()`` and ``fetch()``, and register it in sources/__init__.py.
"""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import DatasetCfg


@dataclass
class Granule:
    key: str                 # human-readable id, e.g. "chirps:precip:2024-01-15"
    variable: str
    dates: list


@dataclass
class Asset:
    variable: str
    date: dt.date
    path: Path                        # local GeoTIFF or NetCDF
    band: int = 1                     # GeoTIFF band
    time_index: Optional[int] = None  # NetCDF time step


class DataSource(ABC):
    #: whether granules can be fetched concurrently (False for queued APIs like CDS)
    parallel_fetch = True

    def __init__(self, cfg: DatasetCfg, workdir: Path):
        self.cfg = cfg
        self.workdir = workdir
        workdir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def granules(self, variables: list[str], dates: list) -> list[Granule]:
        """Split the requested (variables x dates) into download units."""

    @abstractmethod
    def fetch(self, granule: Granule) -> list[Asset]:
        """Download one granule; return the assets it contains.

        Raise FileNotFoundError if the granule does not exist at the source
        (e.g. date not yet published) — the pipeline logs and skips it.
        """
