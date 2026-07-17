"""Small shared helpers: logging, retries, date handling, gzip."""
from __future__ import annotations

import gzip
import logging
import shutil
import threading
import time
from datetime import date, timedelta
from functools import wraps
from pathlib import Path

log = logging.getLogger("tilegen")

# netCDF4/HDF5 is not thread-safe, not even across distinct files: concurrent
# reads/writes corrupt HDF5 global state (double free). Every netCDF open or
# write in threaded code must hold this lock.
nc_lock = threading.Lock()


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def retry(times: int = 4, delay: float = 5.0, backoff: float = 2.0,
          exceptions: tuple = (Exception,)):
    """Retry a function with exponential backoff on the given exceptions."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            wait = delay
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    if attempt == times:
                        raise
                    log.warning("%s failed (attempt %d/%d): %s — retrying in %.0fs",
                                fn.__name__, attempt, times, exc, wait)
                    time.sleep(wait)
                    wait *= backoff
        return wrapper
    return deco


def daterange(start: date, end: date):
    """Yield every date from start to end, inclusive."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def month_groups(dates) -> dict[tuple[int, int], list[date]]:
    """Group dates into {(year, month): [dates]}."""
    groups: dict[tuple[int, int], list[date]] = {}
    for d in dates:
        groups.setdefault((d.year, d.month), []).append(d)
    return groups


def gunzip(src: Path, dst: Path) -> Path:
    with gzip.open(src, "rb") as f_in, open(dst, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return dst
