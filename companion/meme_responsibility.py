"""Read retained caption accounting without loading or reconstructing content."""

import re
from pathlib import Path
from types import SimpleNamespace

from companion import live_text, owner_authority, paid_budget
from companion.contracts import CoreError, StrictModel
from companion.store import encode
from companion_delivery_protocol.authority import AuthorityRef
from companion_meme_protocol import (
    CaptionIdentity,
    CaptionResponsibility,
    UsageReceipt,
    caption_cache_key,
    digest,
    parse,
    strict_json,
)

MAX_ROWS = 2048  # Original caption storage/ancestry bound.
SHA = re.compile(r"[a-f0-9]{64}")
JOB = re.compile(r"job_[a-f0-9]{32}")


class EmptyBody(StrictModel):
    pass


def _lineage(store, job_id):
    nodes, seen = [], set()
    while job_id:
        if job_id in seen or len(nodes) >= MAX_ROWS:
            raise ValueError("invalid caption ancestry")
        seen.add(job_id)
        row = store.one(
            'SELECT c.job_id, c.request_sha256, c.asset_sha256, c.source_sha256, c.cache_key, '
            'c.identity_json, c.usage_request_id, c.reuse_job_id, j.kind, j.event_id, j.state, '
            'j.authority_json, j.last_error_code, e.source, e.event_type, e.actor_id, v.scope, '
            'v.owner_actor_id FROM meme_captions AS c JOIN jobs AS j USING (job_id) JOIN events AS '
            'e ON e.event_id = j.event_id JOIN conversations AS v ON v.conversation_id = '
            'e.conversation_id WHERE c.job_id = %s', (job_id,),
        )
        if (not row or row["kind"] != "meme_caption" or row["source"] != "qq.onebot"
            or row["event_type"] != "message.received" or row["actor_id"] != "owner"
            or row["scope"] != "private" or row["owner_actor_id"] != "owner"
            or not store.one('SELECT 1 FROM job_sources WHERE job_id = %s AND event_id = %s',
                             (job_id, row["event_id"]))
            or any(not isinstance(row[k], str) or not SHA.fullmatch(row[k]) for k in (
                "request_sha256", "asset_sha256", "source_sha256", "cache_key"))):
            raise ValueError("unverified original caption")
        identity = parse(CaptionIdentity, row["identity_json"])
        if (row["cache_key"] != caption_cache_key(row["asset_sha256"], identity)
            or nodes and any(row[k] != nodes[0][k] for k in (
                "asset_sha256", "cache_key", "identity_json"))):
            raise ValueError("caption reuse identity changed")
        parent = row["reuse_job_id"]
        if parent is not None and (not isinstance(parent, str) or not JOB.fullmatch(parent)):
            raise ValueError("invalid caption ancestor")
        nodes.append(row)
        job_id = parent
    return nodes


def _journals(store, nodes):
    identity = store.one("SELECT value FROM runtime_meta WHERE key = 'model_budget_journal_id'")
    if not identity:
        raise ValueError("original journal identity missing")
    journals = {}
    for node in nodes:
        ref = AuthorityRef.from_dict(strict_json(node["authority_json"]))
        original = store.one('SELECT grant_id, revision, policy_sha256, restore_generation, '
            'journal_path, journal_id FROM owner_authority_revisions WHERE grant_id = %s AND '
            'revision = %s', (ref.grant_id, ref.revision))
        if (not original or owner_authority.reference(original) != ref
            or original["journal_id"] != identity["value"]):
            raise ValueError("original caption authority missing")
        path = Path(original["journal_path"])
        node["journal_path"] = str(path.resolve())
        if path not in journals:
            raw = path.read_bytes()
            proof = paid_budget.journal(store, SimpleNamespace(journal_path=str(path)),
                record=None, initialize=False, allow_settlement=True)
            if path.read_bytes() != raw:
                raise ValueError("original journal changed")
            journals[path] = (raw, proof["head_sha256"])
    if len({proof[1] for proof in journals.values()}) != 1:
        raise ValueError("original journals disagree")
    return identity["value"], journals


def _usage(store, nodes, journal_id):
    ids = encode([n["job_id"] for n in nodes])
    retained = [n["usage_request_id"] for n in nodes if n["usage_request_id"] is not None]
    if (any(not isinstance(value, str) or not 1 <= len(value) <= 160 for value in retained)
        or len(set(retained)) > 1):
        raise ValueError("caption fee pointers disagree")
    pointers = encode(retained)
    usages = store.rows('SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS '
        'request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, '
        'model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS '
        'input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, '
        'model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS '
        'billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS '
        'occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM '
        'model_usage AS model_usage WHERE model_usage.job_id IN (SELECT parsed.value COLLATE "C" '
        'AS _col_0 FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value)) OR '
        'model_usage.request_id IN (SELECT parsed.value COLLATE "C" AS _col_0 FROM '
        'JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value))',
        (ids, pointers))
    admissions = store.rows('SELECT request_id, logical_request_id, authority_json, journal_id, '
        'journal_path FROM model_admissions WHERE logical_request_id IN (SELECT value COLLATE "C" '
        'FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value)) OR request_id IN '
        '(SELECT value COLLATE "C" FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS '
        'parsed(value)) OR request_id IN (SELECT value COLLATE "C" FROM '
        'JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value))',
        (ids, pointers, encode([u["request_id"] for u in usages])))
    if not usages and not admissions:
        if any(n["state"] not in {"FAILED", "CANCELLED"} for n in nodes):
            raise ValueError("nonterminal caption without admission")
        return None, None
    if len(usages) != 1 or len(admissions) != 1:
        raise ValueError("ambiguous caption fee ownership")
    usage, admission, owner = usages[0], admissions[0], nodes[-1]
    request = usage["request_id"]
    if (usage["job_id"] != owner["job_id"] or usage["scenario"] != "meme_caption"
        or owner["usage_request_id"] != request
        or any(n["usage_request_id"] not in {None, request} for n in nodes)
        or admission["request_id"] != request
        or admission["logical_request_id"] != owner["job_id"]
        or strict_json(admission["authority_json"]) != strict_json(owner["authority_json"])
        or admission["journal_id"] != journal_id
        or str(Path(admission["journal_path"]).resolve()) != owner["journal_path"]):
        raise ValueError("caption fee owner changed")
    receipt = UsageReceipt(usage_request_id=request, state=usage["state"],
        input_tokens=usage["input_tokens"] if not usage["is_estimate"] else None,
        output_tokens=usage["output_tokens"] if not usage["is_estimate"] else None,
        billed_micro_usd=usage["billed_micro_usd"] if usage["state"] == "SETTLED" else None)
    return owner["job_id"], receipt


def read(store, settings, job_id):
    if not isinstance(job_id, str) or not JOB.fullmatch(job_id):
        raise CoreError("MEME_RESPONSIBILITY_JOB_INVALID", 400)
    try:
        with store.transaction():
            if store.one('SELECT 1 FROM runtime_meta WHERE key IN '
                "('model_budget_restore_required', 'cloud_budget_restore_required')"):
                raise CoreError("OWNER_AUTHORITY_RESTORE_RECONCILIATION_REQUIRED", 503)
            ledger = live_text._ledger_custody(store, settings)
            nodes = _lineage(store, job_id)
            journal_id, journals = _journals(store, nodes)
            owner, usage = _usage(store, nodes, journal_id)
            target = nodes[0]
            value = CaptionResponsibility(version=1, job_id=job_id, event_id=target["event_id"],
                request_sha256=target["request_sha256"], asset_sha256=target["asset_sha256"],
                source_evidence_sha256=target["source_sha256"], cache_key=target["cache_key"],
                authority_sha256=digest(strict_json(target["authority_json"])),
                job_state=target["state"],
                body_erased=target["last_error_code"] == "MEME_CAPTION_SOURCE_ERASED",
                liability_kind="USAGE" if usage else "NO_ADMISSION",
                usage_owner_job_id=owner, usage=usage,
                lineage_sha256=digest([{k: n[k] for k in (
                    "job_id", "event_id", "request_sha256", "asset_sha256", "source_sha256",
                    "cache_key", "authority_json", "reuse_job_id", "usage_request_id",
                )} for n in nodes]), erasure_ledger_sha256=ledger,
                budget_journal_id_sha256=digest(journal_id),
                budget_journal_head_sha256=next(iter(journals.values()))[1])
            live_text._ledger_custody(store, settings, ledger)
            for path, (raw, head) in journals.items():
                checked = paid_budget.journal(store, SimpleNamespace(journal_path=str(path)),
                    record=None, initialize=False, allow_settlement=True)
                if checked["head_sha256"] != head or path.read_bytes() != raw:
                    raise ValueError("original journal changed")
            return value.model_dump(mode="json")
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise CoreError("MEME_RESPONSIBILITY_UNVERIFIED", 409) from error
