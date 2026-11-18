"""Populate the searchable projection of existing little-endian BGE float32 bytes."""

import math
import struct


def backfill(connection):
    # Keep the original bytes for byte-exact SQLite reconciliation, including opaque old formats.
    with connection.cursor(name="companion_vector_backfill") as source, connection.cursor() as out:
        source.execute("SELECT memory_id,vector FROM memory_vectors "
                       "WHERE embedding IS NULL AND octet_length(vector)=2048")
        while rows := source.fetchmany(128):
            updates = []
            for memory_id, raw in rows:
                values = struct.unpack("<512f", raw)
                if all(map(math.isfinite, values)):
                    updates.append((list(values), memory_id))
            out.executemany("UPDATE memory_vectors SET embedding=%s::real[]::public.vector "
                            "WHERE memory_id=%s", updates)
