"""Storage layer: paths, atomic writes, and the raw and parquet formats.

Everything that touches the filesystem goes through here. No other package
constructs a data path or opens a data file directly.
"""

from fpl.storage.atomic import atomic_write_bytes
from fpl.storage.parquet_io import read_parquet, write_parquet
from fpl.storage.paths import (
    chunk_partition,
    decode_as_of,
    encode_as_of,
    facts_table,
    iter_chunks,
    latest_partition,
    raw_endpoint_dir,
    raw_partition,
    staged_table,
)
from fpl.storage.raw_io import RawArtifact, WriteResult, read_raw, write_chunk, write_raw

__all__ = [
    "RawArtifact",
    "WriteResult",
    "atomic_write_bytes",
    "chunk_partition",
    "decode_as_of",
    "encode_as_of",
    "facts_table",
    "iter_chunks",
    "latest_partition",
    "raw_endpoint_dir",
    "raw_partition",
    "read_parquet",
    "read_raw",
    "staged_table",
    "write_chunk",
    "write_parquet",
    "write_raw",
]
