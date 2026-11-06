"""One bounded increment of the existing derived vector cache in the Life worker."""

import json

from companion.contracts import CoreError
from companion.memory_context import MemoryContext, MemorySources
from companion.retrieval import INDEX_VERSION
from companion.retrieval_work import WorkLimit
from companion.store import encode

KEY = "memory_vector_scan"
SCAN_ROWS, BUILD_ROWS, TEXT_BYTES = 16, 8, 65536


async def warm_once(memory, clock, guard):
    if not memory.settings.memory_enabled or not guard():
        return False
    store = memory.store
    saved = store.one("SELECT value FROM runtime_meta WHERE key=?", (KEY,))
    saved = json.loads(saved["value"]) if saved else {}
    cursor = saved.get("cursor", 0) if saved.get("version") == INDEX_VERSION else 0
    headers = store.rows(
        "SELECT m.rowid AS cursor,m.memory_id,octet_length(m.content) AS text_bytes,"
        "v.memory_id AS indexed FROM memories m LEFT JOIN memory_vectors v "
        "ON v.memory_id=m.memory_id AND v.model_version=? WHERE m.rowid>? "
        "ORDER BY m.rowid LIMIT ?", (INDEX_VERSION, cursor, SCAN_ROWS))
    rows, used = [], 0
    sources = MemorySources(memory, clock(), bounded=True)
    for header in headers:
        if header["indexed"] is not None or len(rows) == BUILD_ROWS:
            continue
        if used + header["text_bytes"] > TEXT_BYTES:
            continue
        row = store.one("SELECT * FROM memories WHERE memory_id=? "
            "AND octet_length(content)=? AND status IN ('CONFIRMED','HYPOTHESIS') "
            "AND privacy_scope='private' AND confidence>0",
            (header["memory_id"], header["text_bytes"]))
        if not row:
            continue
        try:
            if sources.current(row) and (evidence := sources.evidence(row["memory_id"])):
                rows.append({**row, "evidence_ids": [event["event_id"] for event in evidence]})
                used += header["text_bytes"]
        except WorkLimit:
            break
    if rows:
        context = MemoryContext(memory)
        proof = context.pin(clock(), rows=rows)

        def revalidate():
            if not guard():
                raise CoreError("MEMORY_ACCESS_DENIED", 403)
            context.revalidate(proof, clock())

        await memory.index.scores_async("local derived index warmup", rows, revalidate=revalidate)
    next_cursor = headers[-1]["cursor"] if headers else 0
    if next_cursor != cursor:
        with store.transaction():
            store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                          (KEY, encode({"version": INDEX_VERSION, "cursor": next_cursor})))
        return True
    return bool(rows)
