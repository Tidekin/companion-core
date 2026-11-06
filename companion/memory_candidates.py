"""Bounded indexed nomination; Memory still performs all live source validation."""

import re

from companion import retrieval_work
from companion.store import encode


def terms(query):
    values = re.findall(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)+|[A-Za-z0-9_\u4e00-\u9fff]+",
                        query[:4096])
    grams = []
    for value in values:
        grams.extend(value[i:i + 3] for i in range(len(value) - 2)) if re.search(
            r"[\u4e00-\u9fff]", value) else grams.append(value)
    return [value for value in dict.fromkeys(grams) if len(value) >= 3][:40]


def exact_terms(query):
    return list(dict.fromkeys([query[:4096], *re.findall(
        r"[A-Za-z0-9_-]{2,64}", query[:4096])]))[:24]


def nominate(memory, query, subjects, memory_ids, semantic_ids=()):
    work = retrieval_work.current()
    if not work.take("searches"):
        return [], []
    ceiling = min(retrieval_work.FACT_CANDIDATES, work.remaining("candidates"))
    if not ceiling:
        work.mark("candidates")
        return [], []
    if len(query) > 4096:
        work.mark("query_text")
    base = ("FROM memories m WHERE m.status IN ('CONFIRMED','HYPOTHESIS') "
            "AND m.privacy_scope='private' AND m.confidence>0 ")
    params = []
    if memory_ids is not None:
        base += "AND m.memory_id IN (SELECT value FROM json_each(?)) "
        params.append(encode(memory_ids))
    if subjects:
        base += "AND m.subject IN (SELECT value FROM json_each(?)) "
        params.append(encode(subjects))
    projection = "SELECT m.memory_id,octet_length(m.content) AS text_bytes "
    headers, lexical = {}, []

    def collect(sql, args, size):
        values = work.rows(memory.store, sql, args, limit=size + 1)
        if len(values) > size:
            work.mark("candidate_pool")
        for row in values[:size]:
            if row["memory_id"] not in headers and len(headers) < ceiling:
                headers[row["memory_id"]] = row
        return [row["memory_id"] for row in values[:size]]

    try:
        if query != "*":
            exact = exact_terms(query)
            channel = min(24, ceiling)
            lexical += collect(projection + base + "AND m.memory_id IN "
                    "(SELECT value FROM json_each(?)) "
                    "ORDER BY m.updated_at DESC,m.memory_id LIMIT ?",
                    (*params, encode(exact), channel + 1), channel)
            lexical += collect(projection + base.replace("FROM memories m",
                    "FROM memories m INDEXED BY "
                    "idx_memories_fact_key") + "AND m.fact_key COLLATE NOCASE IN "
                    "(SELECT value FROM json_each(?)) "
                    "ORDER BY m.updated_at DESC,m.memory_id LIMIT ?",
                    (*params, encode(exact), channel + 1), channel)
            match = " OR ".join('"' + value.replace('"', '""') + '"' for value in terms(query))
            if match:
                # rank is FTS5's native BM25 order and can stop at the requested head.
                lexical += collect(projection + "FROM memories_fts JOIN memories m "
                    "ON m.memory_id=memories_fts.memory_id WHERE memories_fts MATCH ? AND "
                    + base.split(" WHERE ", 1)[1] + "ORDER BY rank LIMIT ?",
                    (match, *params, channel + 1), channel)
            if semantic_ids:
                values = work.rows(memory.store, projection + base + "AND m.memory_id IN "
                    "(SELECT value FROM json_each(?)) LIMIT ?",
                    (*params, encode(semantic_ids), ceiling), limit=ceiling)
                position = {mid: i for i, mid in enumerate(semantic_ids)}
                for row in sorted(values, key=lambda row: position[row["memory_id"]]):
                    if row["memory_id"] not in headers and len(headers) < ceiling:
                        headers[row["memory_id"]] = row
        collect(projection + base + "ORDER BY m.needs_review,m.status='CONFIRMED' DESC,"
                "m.updated_at DESC,m.memory_id LIMIT ?", (*params, ceiling + 1), ceiling)
    except retrieval_work.WorkLimit:
        pass  # Already nominated identities can still undergo complete live validation.
    rows = []
    for header in headers.values():
        if not work.take("candidates"):
            break
        if not work.take("text_bytes", header["text_bytes"]):
            continue
        row = memory.store.one("SELECT * FROM memories WHERE memory_id=? "
            "AND octet_length(content)=? AND status IN ('CONFIRMED','HYPOTHESIS') "
            "AND privacy_scope='private' AND confidence>0",
            (header["memory_id"], header["text_bytes"]))
        if row:
            rows.append(row)
    allowed = {row["memory_id"] for row in rows}
    return rows, [mid for mid in lexical if mid in allowed]
