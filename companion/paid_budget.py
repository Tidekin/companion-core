"""Local token estimation and append-only custody for the existing usage ledger.

Estimates are planning inputs, not proof of remote tokenization or maximum charges.
No tokenizer download, provider attestation or external account access is required.
"""

import hashlib
import hmac
import json
import marshal
import os
import re
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from companion.contracts import CoreError
from companion.store import encode, file_identity, uid

# One complete input plus at most its length + 2 of immutable JSON payload.
CACHE_MAX_BYTES = 1048576


@lru_cache(maxsize=1)
def _validated_bytes(raw):
    lines = raw.decode("utf-8").splitlines()
    previous = "0" * 64
    for line in lines:
        row = json.loads(line)
        digest = row.pop("sha256")
        if row.get("previous") != previous or sha(row) != digest:
            raise ValueError("journal chain")
        previous = digest
    # Fresh decoding by the caller prevents fold/pop from changing cached objects.
    return ("[" + ",".join(lines) + "]").encode("utf-8")


def _journal_records(raw):
    if len(raw) <= CACHE_MAX_BYTES:
        try:
            payload = _validated_bytes(raw)
            records = json.loads(payload)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            _validated_bytes.cache_clear()
            # A damaged later line must not skip an earlier fold/error. Replay
            # the original interleaved path over the same freshly read bytes.
        else:
            return records, True
    else:
        _validated_bytes.cache_clear()
    return (json.loads(line) for line in raw.decode("utf-8").splitlines()), False


def sha(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def snapshot(settings):
    from companion.token_measurement import count_contract

    approval = settings.paid_model
    value = approval.model_dump(mode="json", exclude={"journal_path"})
    # Existing text-only journals bind the original bytes, not a new null field.
    if approval.image_input is None:
        value.pop("image_input")
    value["estimator"] = "utf8-third-v1"
    if counting := count_contract(settings):
        value["token_measurement"] = counting
    value["local_monthly_micro_usd"] = settings.monthly_limit_micro_usd
    value["scenario_daily_micro_usd"] = {
        "memory_extract": settings.memory_daily_micro_usd,
        "memory_review": settings.memory_daily_micro_usd,
        "diary": settings.diary_daily_micro_usd,
        "reflection": settings.reflection_daily_micro_usd,
        "activity": settings.autonomous_daily_micro_usd,
    }
    if approval.image_input is not None:
        value["image_estimator"] = "approved-fixed-image-plus-utf8-third-v1"
        value["scenario_daily_micro_usd"]["content_vision"] = settings.content.image_daily_micro_usd
    if approval.meme_input is not None:
        value["image_estimator"] = "approved-fixed-image-plus-utf8-third-v1"
        value["scenario_daily_micro_usd"]["meme_caption"] = settings.meme_caption_daily_micro_usd
    if settings.social_autonomy.enabled:
        value["scenario_daily_micro_usd"]["social_autonomy"] = (
            settings.social_autonomy.model_daily_micro_usd)
    # watch_review, style_distill and jargon stay out, like rem: their own daily caps apply at
    # reservation inside the approved window, and enabling them keeps the policy valid.
    value["reflection_monthly_micro_usd"] = settings.reflection_monthly_micro_usd
    # Private per-window credential binding, never the raw key or a public account identifier.
    value["credential_tag"] = hmac.new(
        settings.model_key.get_secret_value().encode(),
        encode([approval.window_id, approval.provider, approval.endpoint]).encode(),
        hashlib.sha256,
    ).hexdigest()
    if approval.video_input is not None:
        video = approval.video_input
        value["video_credential_tag"] = hmac.new(
            settings.video_model_key.get_secret_value().encode(),
            encode([approval.window_id, video.provider, video.endpoint]).encode(),
            hashlib.sha256,
        ).hexdigest()
    return value


def active(approval, now):
    """The approval interval; an absent expiry is open-ended."""
    return approval.starts_at <= now and (approval.expires_at is None or now < approval.expires_at)


def rates_valid(approval, now):
    return approval.rates_valid_until is None or now < approval.rates_valid_until


def contract(settings, now):
    if settings.mode != "live":
        raise CoreError("PAID_LIVE_MODE_REQUIRED", 503)
    approval = settings.paid_model
    if approval is None:
        raise CoreError("PAID_APPROVAL_REQUIRED", 503)
    if not approval.live_approved:
        raise CoreError("PAID_LIVE_OPT_IN_REQUIRED", 503)
    if not settings.model_key.get_secret_value():
        raise CoreError("PAID_MODEL_KEY_REQUIRED", 503)
    current = datetime.fromisoformat(now)
    if current.tzinfo is None or not active(approval, current):
        raise CoreError("PAID_APPROVAL_EXPIRED", 503)
    if not rates_valid(approval, current):
        raise CoreError("PAID_RATES_EXPIRED", 503)
    if (
        settings.provider,
        settings.model_name,
        settings.model_url.rstrip("/"),
        settings.timezone,
    ) != (approval.provider, approval.model, approval.endpoint.rstrip("/"), approval.timezone):
        raise CoreError("PAID_PROVIDER_CONTRACT_MISMATCH", 503)
    if (settings.input_rate, settings.cached_input_rate, settings.output_rate) != (
        approval.input_rate,
        approval.cached_input_rate,
        approval.output_rate,
    ):
        raise CoreError("PAID_RATE_MISMATCH", 503)
    return approval


def estimate_input_tokens(approval, messages, scenario, *, tools=()):
    """Estimate approved input locally, without claiming a provider-side token bound.

    UTF-8 bytes / 3 approximates mixed Chinese/Latin text, with per-message and
    structured-response overhead plus an explicit owner-configured safety margin.
    A validated image adds its separately approved fixed estimate, never base64 tokens.
    Actual provider usage is the calibration signal; this is never an upper proof.
    """
    try:
        estimate = 32
        if tools and scenario != "chat":
            raise CoreError("MODEL_TOOLS_SCOPE_REQUIRED", 503)
        if scenario == "chat":
            from companion.agent_messages import estimate_messages

            messages, tool_estimate = estimate_messages(messages, tools)
            estimate += tool_estimate
        if scenario == "content_video_preview":
            from companion.video_input import preview_text_and_estimate

            text, image_estimate = preview_text_and_estimate(
                approval.video_input, approval.image_input, messages,
            )
            messages = [{"role": "user", "content": text}]
            estimate += image_estimate
        elif scenario == "content_video":
            from companion.video_input import text_and_estimate

            text, video_estimate = text_and_estimate(approval.video_input, messages,
                                                    approval.image_input)
            messages = [{"role": "user", "content": text}]
            estimate += video_estimate
        elif scenario == "meme_caption":
            from companion.meme_input import text_and_estimate

            text, image_estimate = text_and_estimate(approval.meme_input, messages)
            messages = [{"role": "user", "content": text}]
            estimate += image_estimate
        elif scenario == "content_vision":
            from companion.image_input import text_and_estimate

            text, image_estimate = text_and_estimate(approval.image_input, messages)
            messages = [{"role": "user", "content": text}]
            estimate += image_estimate
            if approval.video_input is not None:
                from companion.video_input import TOOL

                estimate += (len(encode(TOOL).encode("utf-8")) + 2) // 3
        elif any(isinstance(message, dict) and isinstance(message.get("content"), list)
                 for message in messages):
            from companion.image_input import messages_and_estimate

            if scenario != "chat":
                raise ValueError("unsupported image scenario")
            messages, image_estimate = messages_and_estimate(approval.image_input, messages)
            estimate += image_estimate
        for message in messages:
            if (
                set(message) != {"role", "content"}
                or message["role"] not in {"system", "user", "assistant"}
                or not isinstance(message["content"], str)
            ):
                raise ValueError("unsupported message contract")
            estimate += 8 + (len(message["content"].encode("utf-8")) + 2) // 3
        if scenario in {"memory_review", "memory_extract", "diary", "reflection", "meme_caption",
                        "qq_poke", "social_autonomy", "content_video_preview", "watch_review",
                        "style_distill", "jargon"}:
            estimate += 32
        return (estimate * approval.input_estimate_safety_percent + 99) // 100
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise CoreError("MODEL_INPUT_ESTIMATE_INVALID", 503) from error


LIVE_LIFECYCLE = "model_budget_live_lifecycle"


def live_lifecycle(store):
    row = store.one("SELECT value FROM runtime_meta WHERE key=?", (LIVE_LIFECYCLE,))
    return json.loads(row["value"]) if row else None


def _live_record(row, digest, current, seen):
    kind = row.get("kind")
    if kind not in {"window_open", "window_stop", "window_handoff"}:
        return current
    fields = {"kind", "window_id", "plan_sha256", "previous"}
    if kind != "window_stop":
        fields.add("handoff_sha256")
    if kind == "window_handoff":
        fields |= {"predecessor_window_id", "predecessor_plan_sha256"}
    if set(row) != fields or not re.fullmatch(r"win_[a-f0-9]{32}", row["window_id"]) or (
        not re.fullmatch(r"[a-f0-9]{64}", row["plan_sha256"])
    ):
        raise ValueError("invalid live lifecycle")
    if kind != "window_stop" and row["handoff_sha256"] is not None and not re.fullmatch(
        r"[a-f0-9]{64}", row["handoff_sha256"]
    ):
        raise ValueError("invalid handoff hash")
    same = current and (current["window_id"], current["plan_sha256"]) == (
        row["window_id"], row["plan_sha256"]
    )
    if kind == "window_handoff":
        if (
            not current or current["state"] != "STOPPED" or not row["handoff_sha256"]
            or (current["window_id"], current["plan_sha256"]) != (
                row["predecessor_window_id"], row["predecessor_plan_sha256"]
            ) or row["window_id"] in seen or row["plan_sha256"] in seen
        ):
            raise ValueError("invalid lifecycle predecessor")
    elif kind == "window_open":
        if current and (not same or current["state"] != "PREPARED"):
            raise ValueError("stopped or replaced window")
        if current and row["handoff_sha256"] != current["handoff_sha256"]:
            raise ValueError("handoff changed")
        if not current and row["handoff_sha256"] is not None:
            raise ValueError("missing predecessor")
    elif current and not same:
        raise ValueError("stale window stop")
    seen.update((row["window_id"], row["plan_sha256"]))
    return {"window_id": row["window_id"], "plan_sha256": row["plan_sha256"],
            "state": {"window_open": "OPEN", "window_stop": "STOPPED",
                      "window_handoff": "PREPARED"}[kind],
            "handoff_sha256": row.get("handoff_sha256", (current or {}).get("handoff_sha256")),
            "head_sha256": digest}


IDENTITY = "SELECT value FROM runtime_meta WHERE key='model_budget_journal_id'"
SUMMARY = ("SELECT (SELECT count(*) FROM runtime_meta),"
           "(SELECT total(length(key)+length(value)) FROM runtime_meta),"
           "(SELECT count(*) FROM model_admissions),"
           "(SELECT count(*) FROM model_usage),"
           "(SELECT total(length(state)+billed_micro_usd) FROM model_usage)")
# Views shared by every connection of this process, for callers such as the API that open
# one connection per request. Keyed by the journal path.
_SHARED, _SHARED_LOCK = {}, threading.Lock()


def _view_key(store, path, raw):
    """What a read-only view depends on, as seen by this connection.

    `data_version` changes when another connection commits; `total_changes` when this one
    writes. The table summary also catches content neither counter sees, such as a page
    level restore into this connection.
    """
    digest = hashlib.sha256(raw).hexdigest()
    # Inside one write transaction or read snapshot only this connection's own writes,
    # counted by total_changes, can change the DB; the summary is then fixed.
    scope = getattr(store, "frozen_scope", None)
    memo = getattr(store, "_view_key_memo", None)
    if scope is not None and memo is not None and memo[0] == (
        scope, store.db.total_changes, str(path), digest,
    ):
        return memo[1]
    summary = tuple(store.db.execute(SUMMARY).fetchone())
    version = store.db.execute("PRAGMA data_version").fetchone()[0]
    key = (str(path), digest, store.db.total_changes, version, summary)
    if scope is not None:
        store._view_key_memo = ((scope, store.db.total_changes, str(path), digest), key)
    return key


def _shared_key(store, key):
    """The same view for any connection: journal bytes, table summary and the DB files.

    Every commit, from any process, writes the WAL (or the DB file after a checkpoint), so
    their inode, size and modification time change with it. A connection with its own
    uncommitted writes never uses or fills the shared entry.
    """
    if store.db.in_transaction and store.db.total_changes != getattr(store, "_txn_changes", None):
        return None
    path = getattr(store, "path", None)
    if not isinstance(path, (str, os.PathLike)) or str(path).startswith((":memory:", "file:")):
        return None
    files = file_identity(path)
    if files[0] is None:
        return None
    # A read snapshot is older than the files once anyone commits; such a view must
    # neither be filled under the newer files nor answer for the older snapshot.
    if getattr(store, "in_read_snapshot", False) and files != store._snapshot_files:
        return None
    return (key[0], key[1], key[4], files)


def forget_views(store=None):
    """Drop reusable views: one connection's, and every shared one."""
    if store is not None:
        store._journal_view = None
    with _SHARED_LOCK:
        _SHARED.clear()


def _permit(view, approval, allow_settlement):
    """The read-only refusals of `_journal`, applied again to a reused view."""
    authority, lifecycle = view["owner_authority"], view["lifecycle"]
    if not allow_settlement and (
        view["blocked_request_ids"]
        or authority and authority["state"] != "ACTIVE"
        or lifecycle and not authority and (
            lifecycle["state"] == "STOPPED"
            or getattr(approval, "window_id", lifecycle["window_id"]) != lifecycle["window_id"])
    ):
        _validated_bytes.cache_clear()
        raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)


def journal(store, approval, record=None, *, allow_settlement=False, initialize=True,
            resolution_replay=None, model_control_replay=None, accounting_replay=None):
    """Validated custody view; read-only calls reuse it while journal and DB are unchanged.

    One chat context used to re-validate the whole journal about 180 times (~25 s), and the
    API re-validated it on every Bridge poll while holding the write lock. Appends, replays
    and any change to the bytes or the DB still take the full path, and the read-only
    refusals are applied again on every reuse. Callers get a private copy, so folding into
    a view never changes the saved one.
    """
    if (record is not None or resolution_replay is not None
            or model_control_replay is not None or accounting_replay is not None):
        forget_views(store)
        return _journal(store, approval, record, allow_settlement=allow_settlement,
                        initialize=initialize, resolution_replay=resolution_replay,
                        model_control_replay=model_control_replay,
                        accounting_replay=accounting_replay)
    try:
        path = Path(approval.journal_path)
        try:
            raw = path.read_bytes()
        except OSError:
            raw = None
        key = shared = saved = None
        if raw is not None and len(raw) <= CACHE_MAX_BYTES:
            key = _view_key(store, path, raw)
            shared = _shared_key(store, key)
            saved = getattr(store, "_journal_view", None)
            if saved is None or saved[0] != key:
                with _SHARED_LOCK:
                    entry = _SHARED.get(key[0])
                saved = (key, *entry[1:]) if (
                    shared is not None and entry is not None and entry[0] == shared) else None
            identity = store.one(IDENTITY) if saved is not None else None
    except BaseException:
        _validated_bytes.cache_clear()
        raise
    if saved is not None and identity == saved[2]:
        if saved[3].get("gateway_clients"):
            from companion.gateway import validate_clients

            try:
                validate_clients(store, saved[3]["gateway_clients"])
            except (ValueError, KeyError, TypeError):
                raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503) from None
        if saved[3].get("gateway_bindings"):
            from companion.gateway import validate_projection

            try:
                validate_projection(store, saved[3]["gateway_bindings"])
            except (ValueError, KeyError, TypeError):
                raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503) from None
        _permit(saved[3], approval, allow_settlement)
        store._journal_view = saved
        return marshal.loads(saved[1])
    store._journal_view = None
    view = _journal(store, approval, allow_settlement=allow_settlement, initialize=initialize,
                    raw=raw)
    if key is not None:
        try:
            blob = marshal.dumps(view)
        except ValueError:
            return view
        after = _view_key(store, path, raw)
        entry = (after, blob, store.one(IDENTITY), marshal.loads(blob))
        store._journal_view = entry
        shared = _shared_key(store, after)
        if shared is not None:
            with _SHARED_LOCK:
                _SHARED[after[0]] = (shared, *entry[1:])
    return view


def _journal(store, approval, record=None, *, allow_settlement=False, initialize=True,
             resolution_replay=None, model_control_replay=None, accounting_replay=None,
             raw=None):
    """Caller holds BEGIN IMMEDIATE; fsync before DB commit makes failure conservative.

    This is not a second queue or message store: only reservation hashes and monetary
    lower bounds survive outside the DB. A mismatch blocks, never silently repairs.
    """
    try:
        from companion import (
            cumulative,
            delivery_reconciliation,
            model_accounting,
            model_carry,
            owner_authority,
        )

        path = Path(approval.journal_path)
        identity = store.one("SELECT value FROM runtime_meta WHERE key='model_budget_journal_id'")
    except BaseException:
        _validated_bytes.cache_clear()
        raise
    try:
        if not path.exists():
            # A query-only snapshot cannot record the identity, so it never creates the file.
            if (not initialize or getattr(store, "in_read_snapshot", False) or identity
                or store.one("SELECT 1 FROM model_admissions LIMIT 1")
                or store.one("SELECT 1 FROM runtime_meta WHERE key=?", (cumulative.PROJECTION,))):
                raise ValueError("missing independent journal")
            path.parent.mkdir(parents=True, exist_ok=True)
            journal_id = uid("budget")
            header = {"kind": "header", "journal_id": journal_id, "previous": "0" * 64}
            header["sha256"] = sha(header)
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                os.chmod(path, 0o600)
                stream.write(encode(header) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            store.insert("runtime_meta", key="model_budget_journal_id", value=journal_id)
            identity = {"value": journal_id}
        records, checked = _journal_records(path.read_bytes() if raw is None else raw)
        previous, latest, blocked = "0" * 64, {}, {}
        corrections = {}
        measurements = {}
        gateway_bindings = {}
        gateway_clients = {}
        resolutions = {}
        authorities = []
        heads = set()
        lifecycle, seen, managed = None, set(), False
        account = cumulative.empty_state()
        model_closures, model_carries, model_controls = {}, {}, {}
        accounting = {}
        for index, row in enumerate(records):
            digest = row.pop("sha256")
            heads.add(digest)
            if not checked and (row.get("previous") != previous or sha(row) != digest):
                raise ValueError("journal chain")
            previous = digest
            if index == 0:
                if (
                    row.get("kind") != "header"
                    or not identity
                    or row.get("journal_id") != identity["value"]
                ):
                    raise ValueError("journal identity")
            else:
                if row.get("kind") == "gateway_client":
                    client = row["client"]
                    gateway_clients[client["client_id"]] = client
                    continue
                if row.get("kind") == model_accounting.KIND:
                    model_accounting.fold(store, row, digest, identity["value"], latest,
                                          model_closures, blocked, accounting)
                    continue
                if row.get("kind") in model_carry.KINDS:
                    model_carry.fold(store, row, digest, identity["value"], authorities, latest,
                                     blocked, model_closures, model_carries)
                    model_controls[digest] = row
                    continue
                if row.get("kind") == owner_authority.RECORD_KIND:
                    owner_authority.fold(authorities, row, digest)
                    continue
                if row.get("kind") == delivery_reconciliation.KIND:
                    delivery_reconciliation.validate_journal_record(
                        store, row, digest, lifecycle, identity["value"], authorities,
                    )
                    message_id = row["delivery"]["message_id"]
                    if message_id in resolutions:
                        raise ValueError("duplicate owner resolution")
                    resolutions[message_id] = {**row, "sha256": digest}
                cumulative.fold(account, row)
                if row.get("kind") in cumulative.CONTROL_RECORDS:
                    continue
                if row.get("kind") in {"window_stop", "window_open", "window_handoff"}:
                    lifecycle = _live_record(row, digest, lifecycle, seen)
                    managed |= row["kind"] != "window_stop"
                    continue
                if row["kind"] == "video_rate_correction":
                    from companion.video_accounting import validate_record

                    request_id = row["request_id"]
                    validate_record(store, row, latest.get(request_id), blocked.get(request_id),
                                    identity["value"])
                    if set(blocked) != {request_id} or row["idempotency_key"] in corrections:
                        raise ValueError("unrelated block or correction replay")
                    blocked.pop(request_id)
                    corrections[row["idempotency_key"]] = row
                elif row["kind"] == "blocked":
                    blocked[row["request_id"]] = digest
                if row["kind"] == "reserve" and "measurement" in row:
                    proof = row["measurement"]
                    if (set(proof) != {"version", "sha256"} or proof["version"] != 1
                        or not re.fullmatch(r"[a-f0-9]{64}", proof["sha256"])
                        or row["request_id"] in measurements):
                        raise ValueError("measurement journal evidence")
                    measurements[row["request_id"]] = proof["sha256"]
                if "gateway" in row:
                    if row["kind"] != "reserve" or row["request_id"] in gateway_bindings:
                        raise ValueError("duplicate gateway attribution")
                    gateway_bindings[row["request_id"]] = row["gateway"]
                latest[row["request_id"]] = row
        if gateway_bindings or record and "gateway" in record or store.one(
            "SELECT 1 FROM model_usage WHERE scenario='gateway' LIMIT 1"
        ):
            from companion.gateway import validate_projection

            gateway_bindings = validate_projection(store, gateway_bindings, record)
        from companion.gateway import validate_clients

        gateway_clients = validate_clients(store, gateway_clients, record)
        model_carry.validate_projection(store, model_controls, replay=model_control_replay)
        accounting_pending = model_accounting.validate_projection(store, accounting,
                                                                   accounting_replay)
        projection = store.one("SELECT value FROM runtime_meta WHERE key=?",
                               (cumulative.PROJECTION,))
        expected = cumulative.binding(account, path)
        saved = json.loads(projection["value"]) if projection else None
        witness = saved.pop("control_head_sha256", None) if saved else None
        if saved != expected or (expected and witness not in heads):
            raise ValueError("cumulative policy projection behind journal or DB")
        for stop in store.rows("SELECT key,value FROM runtime_meta "
                               "WHERE key LIKE 'model_budget_stop:%'"):
            if (not identity or stop["value"] != identity["value"]
                or stop["key"].removeprefix("model_budget_stop:") not in heads):
                raise ValueError("durable cumulative stop missing from journal")
        stored_lifecycle = live_lifecycle(store)
        if (managed or stored_lifecycle) and stored_lifecycle != lifecycle:
            raise ValueError("live lifecycle behind journal or DB")
        delivery_reconciliation.validate_projection(store, resolutions, resolution_replay)
        owner_authority.validate_projection(store, authorities, identity["value"], path)
        authority_record = record and record.get("kind") == owner_authority.RECORD_KIND
        lifecycle_record = record and record.get("kind") in {
            "window_stop", "window_open", "window_handoff"
        }
        if not heads or (blocked and (
            not allow_settlement or (
                record and record.get("kind") in {"window_open", "window_handoff"}
            )
        )):
            raise ValueError("journal reconciliation")
        if authorities and authorities[-1]["state"] != "ACTIVE" and not (
            allow_settlement or authority_record
        ):
            raise ValueError("owner authority stopped")
        if lifecycle and not authorities and not authority_record and not allow_settlement and (
            lifecycle["state"] == "STOPPED"
            or getattr(approval, "window_id", lifecycle["window_id"]) != lifecycle["window_id"]
        ):
            raise ValueError("live window stopped or replaced")
        admissions = {row["request_id"]: row for row in store.rows(
            "SELECT a.*,m.billed_micro_usd,m.state "
            "FROM model_admissions a LEFT JOIN model_usage m USING(request_id)"
        )}
        for request_id, admission in admissions.items():
            receipt = json.loads(admission.get("measurement_json", "{}"))
            if not isinstance(receipt, dict):
                raise ValueError("invalid measurement receipt")
            expected = measurements.get(request_id)
            if (record and record.get("kind") == "reserve"
                and record.get("request_id") == request_id):
                proof = record.get("measurement", {})
                if proof and (set(proof) != {"version", "sha256"} or proof["version"] != 1):
                    raise ValueError("invalid new measurement version")
                expected = proof.get("sha256")
            if receipt:
                if (expected != sha(receipt) or receipt.get("version") != 1
                    or receipt.get("payload_sha256") != admission["payload_sha256"]):
                    raise ValueError("measurement custody mismatch")
            elif expected is not None:
                raise ValueError("measurement evidence removed")
        for request_id, row in latest.items():
            if record and request_id == record.get("request_id"):
                continue
            admission = admissions.get(request_id)
            if (
                not admission
                or admission["state"] is None
                or admission["billed_micro_usd"] < row["minimum_micro_usd"]
                or admission["state"] != row["state"]
                or admission["contract_sha256"] != row["contract_sha256"]
                or admission["io_started_at"] != row.get("io_started_at")
            ):
                raise ValueError("DB behind journal")
        # A DB from a different journal cannot accept an older valid prefix.
        ids = set(admissions)
        if record:
            ids.discard(record.get("request_id"))
        if set(latest) - ({record.get("request_id")} if record else set()) != ids:
            raise ValueError("journal behind DB")
        if record is not None:
            if record.get("kind") == "video_rate_correction":
                from companion.video_accounting import validate_record

                existing = corrections.get(record["idempotency_key"])
                if existing:
                    if record != {k: v for k, v in existing.items() if k != "previous"}:
                        raise ValueError("correction idempotency conflict")
                    return {"head_sha256": previous, "lifecycle": lifecycle,
                            "cumulative": account, "blocked_request_ids": sorted(blocked)}
                request_id = record["request_id"]
                validate_record(store, {**record, "previous": previous}, latest.get(request_id),
                                blocked.get(request_id), identity["value"])
                if set(blocked) != {request_id}:
                    raise ValueError("unrelated independent block")
            row = {**record, "previous": previous}
            row["sha256"] = sha(row)
            if authority_record:
                owner_authority.fold(authorities, {k: v for k, v in row.items() if k != "sha256"},
                                     row["sha256"])
            if record["kind"] == delivery_reconciliation.KIND:
                delivery_reconciliation.validate_journal_record(
                    store, {k: v for k, v in row.items() if k != "sha256"}, row["sha256"],
                    lifecycle, identity["value"], authorities,
                )
            if not authority_record:
                if record["kind"] == model_accounting.KIND:
                    model_accounting.fold(store, {k: v for k, v in row.items() if k != "sha256"},
                        row["sha256"], identity["value"], latest, model_closures, blocked,
                        accounting)
                elif record["kind"] in model_carry.KINDS:
                    model_carry.fold(store, {k: v for k, v in row.items() if k != "sha256"},
                        row["sha256"], identity["value"], authorities, latest, blocked,
                        model_closures, model_carries)
                else:
                    cumulative.fold(account, {k: v for k, v in row.items() if k != "sha256"})
            next_lifecycle = _live_record(
                {k: v for k, v in row.items() if k != "sha256"}, row["sha256"], lifecycle, seen
            )
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(encode(row) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if record["kind"] == "video_rate_correction":
                blocked.pop(record["request_id"])
            previous = row["sha256"]
            if record["kind"] in model_carry.KINDS:
                store.execute("INSERT INTO runtime_meta VALUES (?,?)",
                              ("model_control:" + previous, previous))
            if record["kind"] in {"quota_pause", delivery_reconciliation.KIND}:
                store.retain_budget_stop(identity["value"], previous)
            if authority_record and record["state"] == "STOPPED":
                store.retain_budget_stop(identity["value"], previous)
            if record["kind"] == delivery_reconciliation.KIND:
                resolutions[row["delivery"]["message_id"]] = row
            if lifecycle_record:
                store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                              (LIVE_LIFECYCLE, encode(next_lifecycle)))
                lifecycle = next_lifecycle
            if record["kind"] in cumulative.CONTROL_RECORDS and account["policy"]:
                store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                              (cumulative.PROJECTION,
                               encode(cumulative.binding(account, path, previous))))
        return {"head_sha256": previous, "lifecycle": lifecycle, "cumulative": account,
                "blocked_request_ids": sorted(blocked), "send_resolutions": resolutions,
                "model_closures": model_closures, "model_carries": model_carries,
                "model_controls": model_controls,
                "model_accounting": accounting,
                "model_accounting_pending": accounting_pending,
                "model_latest": latest,
                "gateway_bindings": gateway_bindings,
                "gateway_clients": gateway_clients,
                "owner_authority": authorities[-1] if authorities else None}
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        _validated_bytes.cache_clear()
        raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503) from error
    except BaseException:
        _validated_bytes.cache_clear()
        raise
