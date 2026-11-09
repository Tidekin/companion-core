"""Bounded indexed nomination; Memory still performs all live source validation."""

import re

from companion import retrieval_work


def terms(query):
    values = re.findall(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)+|[A-Za-z0-9_\u4e00-\u9fff]+",
                        query[:4096])
    grams = []
    for value in values:
        grams.extend(value[i:i + min(3, len(value))]
                     for i in range(max(1, len(value) - 2))) if re.search(
            r"[\u4e00-\u9fff]", value) else grams.append(value)
    return [value for value in dict.fromkeys(grams) if len(value) >= 2][:40]


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
    projection = "SELECT m.memory_id,octet_length(m.content) AS text_bytes,m.updated_at "
    headers, lexical = {}, []
    channels = []

    def collect(sql, args, size, *, order="updated_at DESC,memory_id", lexical=False,
                extra=True):
        # Each indexed channel keeps its original LIMIT before the small window
        # assigns a deterministic position. UNION ALL then needs one metered read.
        channels.append((sql, args, size, order, lexical, extra))

    try:
        if query != "*":
            exact = exact_terms(query)
            channel = min(24, ceiling)
            collect(projection + base + "AND m.memory_id = ANY(%s::text[]) "
                    "ORDER BY m.updated_at DESC,m.memory_id LIMIT %s",
                    (*params, exact, channel + 1), channel, lexical=True)
            # Preserve SQLite NOCASE's ASCII-only identity matching.
            collect(projection + base + "AND translate(m.fact_key,"
                    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') = ANY(%s::text[]) "
                    "ORDER BY m.updated_at DESC,m.memory_id LIMIT %s",
                    (*params, [value.translate(str.maketrans(
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
                        for value in exact], channel + 1), channel, lexical=True)
            patterns = ["%" + value.replace("\\", "\\\\").replace("%", "\\%").replace(
                "_", "\\_") + "%" for value in terms(query)]
            if patterns:
                collect(projection.rstrip() + ",public.word_similarity(%s,m.content) AS score "
                    + base + "AND m.content ILIKE ANY(%s::text[]) "
                    "ORDER BY score DESC,"
                    "m.updated_at DESC,m.memory_id LIMIT %s",
                    (query, *params, patterns, channel + 1), channel,
                    order="score DESC,updated_at DESC,memory_id", lexical=True)
            if semantic_ids:
                positions = {mid: i for i, mid in enumerate(semantic_ids)}
                collect(projection.rstrip() + ",array_position(%s::text[],m.memory_id) AS rank "
                    + base
                    + "AND m.memory_id = ANY(%s::text[]) LIMIT %s",
                    (sorted(positions, key=positions.get), *params, list(semantic_ids), ceiling),
                    ceiling, order="rank", extra=False)
        collect(projection.rstrip() + ",m.needs_review,m.status='CONFIRMED' AS confirmed "
                + base + "ORDER BY m.needs_review,m.status='CONFIRMED' DESC,"
                "m.updated_at DESC,m.memory_id LIMIT %s", (*params, ceiling + 1), ceiling,
                order="needs_review,confirmed DESC,updated_at DESC,memory_id")
        statements, bindings = [], []
        for index, (statement, args, _, order, _, _) in enumerate(channels):
            statements.append(
                f"(SELECT {index} AS channel,row_number() OVER (ORDER BY {order}) AS position,"
                "c.memory_id,c.text_bytes FROM (" + statement + ") c)")
            bindings.extend(args)
        values = work.rows(memory.store, "SELECT * FROM (" + " UNION ALL ".join(statements)
            + ") nominated ORDER BY channel,position", tuple(bindings),
            limit=sum(size + extra for _, _, size, _, _, extra in channels))
        for index, (_, _, size, _, is_lexical, _) in enumerate(channels):
            rows = [row for row in values if row["channel"] == index]
            if len(rows) > size:
                work.mark("candidate_pool")
            for row in rows[:size]:
                if row["memory_id"] not in headers and len(headers) < ceiling:
                    headers[row["memory_id"]] = row
                if is_lexical:
                    lexical.append(row["memory_id"])
    except retrieval_work.WorkLimit:
        return [], []  # The shared work receipt records the incomplete nomination.
    selected = []
    for header in headers.values():
        if not work.take("candidates"):
            break
        if not work.take("text_bytes", header["text_bytes"]):
            continue
        selected.append(header)
    if not selected:
        return [], []
    from companion.memory_context import MEMORY_COLUMNS
    from companion.turn_recall import lexical as bm25

    try:
        rows = work.rows(memory.store, "SELECT " + ",".join("m." + key for key in MEMORY_COLUMNS)
            + " FROM memories m JOIN unnest(%s::text[],%s::bigint[]) "
            "AS h(memory_id,text_bytes) USING(memory_id) "
            "WHERE octet_length(m.content)=h.text_bytes "
            "AND m.status IN ('CONFIRMED','HYPOTHESIS') AND m.privacy_scope='private' "
            "AND m.confidence>0", ([h["memory_id"] for h in selected],
                                  [h["text_bytes"] for h in selected]), limit=len(selected))
    except retrieval_work.WorkLimit:
        return [], []
    positions = {h["memory_id"]: i for i, h in enumerate(selected)}
    rows.sort(key=lambda row: positions[row["memory_id"]])
    allowed = {row["memory_id"] for row in rows}
    lexical = list(dict.fromkeys(mid for mid in lexical if mid in allowed))
    contents = {row["memory_id"]: row["content"] for row in rows}
    scores = dict(zip(lexical, bm25(query, [contents[mid] for mid in lexical]), strict=True))
    return rows, sorted(lexical, key=lambda mid: -scores[mid])
