"""Configuration models (pydantic) and YAML loaders.

Global settings live in ``tilegen/conf/config.yaml``; each dataset is a YAML
file under ``tilegen/conf/datasets/``. (``config/`` in the repo root is a
symlink to ``tilegen/conf``, so both paths work.) Dataset files may carry
source-specific extra fields (url templates, CDS request params) — models
allow extras.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class S3Cfg(BaseModel):
    bucket: str
    prefix: str = ""
    region: str = "us-east-1"


class GridCfg(BaseModel):
    tile_size_deg: int = 10


class RuntimeCfg(BaseModel):
    workdir: Path = Path(".work")
    workers: int = 6
    keep_local: bool = False


class CogCfg(BaseModel):
    compress: str = "DEFLATE"
    blocksize: int = 256
    overview_resampling: str = "average"


class ZarrCfg(BaseModel):
    # A read of one pixel costs T * chunk_lat * chunk_lon * 4 bytes, which does
    # not depend on time_chunk — so spatial_chunk is the knob that decides how
    # cheap a sub-scene read is, and time_chunk only trades write cost (a daily
    # write rewrites a whole time chunk) against request count.
    time_chunk: int = Field(365, gt=0)     # days per internal chunk
    spatial_chunk: int = Field(32, gt=0)   # pixels per internal chunk side (a cap)


class OutputCfg(BaseModel):
    format: str = "zarr"      # "zarr" (time series) or "cog" (per-date map tiles)


class GlobalCfg(BaseModel):
    s3: S3Cfg
    grid: GridCfg = GridCfg()
    runtime: RuntimeCfg = RuntimeCfg()
    cog: CogCfg = CogCfg()
    zarr: ZarrCfg = ZarrCfg()
    output: OutputCfg = OutputCfg()


class SceneCfg(BaseModel):
    num: int
    type: str = "pais"          # pais | grupo | region | indice
    bbox: list[float]           # [W, S, E, N]
    description: str = ""


class VariableCfg(BaseModel):
    model_config = ConfigDict(extra="allow")
    units: Optional[str] = None


class DatasetCfg(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    version: str
    description: str = ""
    source: str
    temporal: str = "daily"
    start: dt.date
    end: Optional[dt.date] = None
    lag_days: int = 0
    nodata: Optional[float] = None
    tile_size_deg: Optional[int] = None
    variables: dict[str, VariableCfg]


def load_global(config_dir: Path) -> GlobalCfg:
    return GlobalCfg(**yaml.safe_load((config_dir / "config.yaml").read_text()))


def load_scenes(config_dir: Path) -> dict[str, SceneCfg]:
    data = yaml.safe_load((config_dir / "scenes.yaml").read_text())
    return {name: SceneCfg(**cfg) for name, cfg in data["scenes"].items()}


def list_datasets(config_dir: Path) -> list[str]:
    return sorted(p.stem for p in (config_dir / "datasets").glob("*.yaml"))


def load_dataset(config_dir: Path, name: str) -> DatasetCfg:
    path = config_dir / "datasets" / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"unknown dataset {name!r} — available: {', '.join(list_datasets(config_dir))}")
    return DatasetCfg(**yaml.safe_load(path.read_text()))
