"""Source registry: maps the ``source:`` field of a dataset YAML to a class."""

from .edh import Era5EdhSource
from .era5 import Era5CdsSource
from .http_geotiff import HttpGeotiffSource

SOURCES = {
    "http_geotiff": HttpGeotiffSource,
    "era5_cds": Era5CdsSource,
    "era5_edh": Era5EdhSource,
}
