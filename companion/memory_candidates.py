"""Bounded indexed nomination; Memory still performs all live source validation."""

import re

from companion import retrieval_work


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
        base += "AND m.memory_id = ANY(%s::text[]) "
        params.append(list(memory_ids))
    if subjects:
        base += "AND m.subject = ANY(%s::text[]) "
        params.append(list(subjects))
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
            lexical += collect(projection + base + "AND m.memory_id = ANY(%s::text[]) "
                    "ORDER BY m.updated_at DESC,m.memory_id LIMIT %s",
                    (*params, exact, channel + 1), channel)
            # Preserve SQLite NOCASE's ASCII-only identity matching.
            lexical += collect(projection + base + "AND translate(m.fact_key,"
                    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') = ANY(%s::text[]) "
                    "ORDER BY m.updated_at DESC,m.memory_id LIMIT %s",
                    (*params, [value.translate(str.maketrans(
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
                        for value in exact], channel + 1), channel)
            patterns = ["%" + value.replace("\\", "\\\\").replace("%", "\\%").replace(
                "_", "\\_") + "%" for value in terms(query)]
            if patterns:
                lexical += collect(projection + base + "AND m.content ILIKE ANY(%s::text[]) "
                    "ORDER BY public.word_similarity(%s,m.content) DESC,"
                    "m.updated_at DESC,m.memory_id LIMIT %s",
                    (*params, patterns, query, channel + 1), channel)
            if semantic_ids:
                values = work.rows(memory.store, projection + base
                    + "AND m.memory_id = ANY(%s::text[]) LIMIT %s",
                    (*params, list(semantic_ids), ceiling), limit=ceiling)
                position = {mid: i for i, mid in enumerate(semantic_ids)}
                for row in sorted(values, key=lambda row: position[row["memory_id"]]):
                    if row["memory_id"] not in headers and len(headers) < ceiling:
                        headers[row["memory_id"]] = row
        collect(projection + base + "ORDER BY m.needs_review,m.status='CONFIRMED' DESC,"
                "m.updated_at DESC,m.memory_id LIMIT %s", (*params, ceiling + 1), ceiling)
    except retrieval_work.WorkLimit:
        pass  # Already nominated identities can still undergo complete live validation.
    rows = []
    for header in headers.values():
        if not work.take("candidates"):
            break
        if not work.take("text_bytes", header["text_bytes"]):
            continue
        row = memory.store.one('SELECT memories.memory_id AS memory_id, memories.subject AS '
            'subject, memories.kind AS kind, memories.content AS content, memories.status AS '
            'status, memories.origin AS origin, memories.confidence AS confidence, '
            'memories.importance AS importance, memories.privacy_scope AS privacy_scope, '
            'memories.supersedes_memory_id AS supersedes_memory_id, memories.created_at AS '
            'created_at, memories.updated_at AS updated_at, memories.valid_from AS valid_from, '
            'memories.valid_to AS valid_to, memories.last_reviewed_at AS last_reviewed_at, '
            'memories.fact_key AS fact_key, memories.needs_review AS needs_review FROM memories AS '
            'memories WHERE memories.memory_id = %s AND OCTET_LENGTH(memories.content) = %s AND '
            "memories.status IN ('CONFIRMED', 'HYPOTHESIS') AND memories.privacy_scope = 'private' "
            'AND memories.confidence > 0',
            (header["memory_id"], header["text_bytes"]))
        if row:
            rows.append(row)
    allowed = {row["memory_id"] for row in rows}
    return rows, [mid for mid in lexical if mid in allowed]
