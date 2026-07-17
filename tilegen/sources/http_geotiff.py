"""Generic source for datasets published as one GeoTIFF per variable per day
over HTTP(S) — covers CHIRPS, CHIRTS and most UCSB-CHC style archives.

Per-variable config: ``url`` (template with {year}, {month}, {day}) and
optional ``gzip: true`` when files are .gz-compressed.
"""
from __future__ import annotations

import logging

import requests

from ..utils import gunzip, retry
from .base import Asset, DataSource, Granule

log = logging.getLogger("tilegen.http")


class HttpGeotiffSource(DataSource):
    parallel_fetch = True

    def granules(self, variables, dates):
        return [Granule(f"{self.cfg.name}:{v}:{d:%Y-%m-%d}", v, [d])
                for v in variables for d in dates]

    @retry(times=4, delay=10.0, exceptions=(requests.RequestException,))
    def _download(self, url, dst):
        tmp = dst.with_name(dst.name + ".part")
        with requests.get(url, stream=True, timeout=180) as r:
            if r.status_code == 404:
                raise FileNotFoundError(url)
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.rename(dst)
        log.info("downloaded %s (%.1f MB)", dst.name, dst.stat().st_size / 1e6)

    def fetch(self, granule):
        v, d = granule.variable, granule.dates[0]
        vcfg = self.cfg.variables[v]
        url = vcfg.url.format(year=d.year, month=d.month, day=d.day)
        dst = self.workdir / f"{self.cfg.name}_{v}_{d:%Y%m%d}.tif"
        if not dst.exists():
            if getattr(vcfg, "gzip", False):
                raw = dst.with_suffix(".tif.gz")
                self._download(url, raw)
                gunzip(raw, dst)
                raw.unlink()
            else:
                self._download(url, dst)
        return [Asset(variable=v, date=d, path=dst)]
