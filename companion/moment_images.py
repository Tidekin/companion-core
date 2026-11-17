"""A generated picture for one of her moments, kept and forgotten with the moment.

Life may attach `image_prompt` to a PRIVATE_MOMENT decision. Saving that moment queues one
picture when image generation is enabled, the grant has `moments` and `expression` (the
purpose private-chat pictures use), and fewer than `moments_image_daily_limit` pictures were
queued today. The Life worker then runs one priced request per queued picture with the same
reservation and settlement as private-chat pictures (companion/image_generation.py): an
unknown outcome keeps its reservation and is never retried, and the moment stays text-only.
Pictures also count toward `image_generation.max_daily_images`.

Bytes live in the state directory under `moment-images/`, named by the row's moment
and slot. Forgetting or erasing the moment deletes them; only a PUBLISHED moment's READY
picture is served.
"""

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from companion import cloud_budget, image_generation, owner_authority
from companion.contracts import CoreError
from companion.media_provider import HTTPImageGenerator
from companion.store import uid
from companion_meme_protocol import validate_bytes

SCENARIO = "moment_image"
MOMENT_ID = re.compile(r"moment_[a-f0-9]{32}")
# A queued picture that cannot run (no provider dispatch) is dropped after this long,
# so the client never shows it as pending forever. Nothing was reserved for it.
QUEUE_SECONDS = 3600
RULES = ("\nallowed_destinations含PRIVATE_MOMENT且moment_images为true时，可以给这条动态配一张图："
         "image_prompt(300字内)写一句你想画的画面，只画景物、物件、氛围或你自己想象的场景，"
         "不画主人、真实人物或照片，不写文字和链接；不配图就省略。text里不要说配了图。")


def directory(store):
    return Path(store.state_directory).resolve() / "moment-images"


def _path(store, moment_id, slot):
    if not MOMENT_ID.fullmatch(moment_id) or slot != 0:
        raise CoreError("MOMENT_IMAGE_NOT_FOUND", 404)
    root = directory(store)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise CoreError("MOMENT_IMAGE_NOT_FOUND", 404)
    target = root / f"{moment_id}.{slot}.img"
    if target.is_symlink():
        raise CoreError("MOMENT_IMAGE_NOT_FOUND", 404)
    return target


def _write(store, moment_id, slot, data):
    target = _path(store, moment_id, slot)
    target.parent.mkdir(mode=0o700, exist_ok=True)
    partial = target.with_suffix(".tmp")
    with open(partial, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, target)


def _remove(store, moment_id, slot):
    try:
        target = _path(store, moment_id, slot)
    except CoreError:
        return
    target.unlink(missing_ok=True)
    target.with_suffix(".tmp").unlink(missing_ok=True)


def _today(store, settings, now):
    """Pictures that reached a provider reservation today, from chat and moments."""
    day, _ = image_generation._day_and_month(settings, now)
    return store.one(
        "SELECT COUNT(*) AS n FROM moment_images WHERE created_at >= %s "
        "AND state NOT IN ('QUEUED','EXPIRED')", (day,))["n"]


def offered(store, settings, view, destinations, now):
    """Whether this Life decision may ask for a picture with its moment."""
    if "PRIVATE_MOMENT" not in destinations or not {"moments", "expression"} <= set(
            view.policy.purposes):
        return False
    if not image_generation.enabled(settings, settings.image_generation):
        return False
    day, _ = image_generation._day_and_month(settings, now)
    queued = store.one("SELECT COUNT(*) AS n FROM moment_images WHERE created_at >= %s "
                       "AND state <> 'EXPIRED'", (day,))["n"]
    return queued < settings.moments_image_daily_limit


def offered_now(store, settings, destinations, now):
    """The Life request's flag; the queue rechecks everything when the moment is saved."""
    if not owner_authority.required(store, settings):
        return False
    try:
        view = owner_authority.current(store, settings, lambda: now, purpose="life")
    except CoreError:
        return False
    return offered(store, settings, view, destinations, now)


def queue(store, settings, clock, moment_id, decision, authority):
    """Inside moments.save: queue her picture when she asked and it is allowed now."""
    if decision.image_prompt is None:
        return False
    try:
        view = owner_authority.current(store, settings, clock, purpose="expression",
                                       authority=authority)
    except CoreError:
        return False
    now = clock()
    if not offered(store, settings, view, ["PRIVATE_MOMENT"], now):
        return False
    store.insert("moment_images", moment_id=moment_id, slot=0, state="QUEUED",
                 created_at=now, updated_at=now)
    return True


def available(core):
    """Real provider dispatch awaits F8, as for private-chat pictures."""
    return image_generation.enabled(core.settings, core.settings.image_generation) and (
        isinstance(getattr(core, "image_transport", None), httpx.MockTransport))


def _prompt(store, moment_id):
    row = store.one(
        "SELECT r.proposal_json FROM moments m JOIN life_activity_results r "
        "ON r.job_id = m.source_job_id WHERE m.moment_id = %s AND m.state = 'PUBLISHED'",
        (moment_id,))
    if not row:
        return None
    return (json.loads(row["proposal_json"] or "{}").get("image_prompt") or "").strip() or None


def _close(store, moment_id, usage_id, state, usage_state, now, error_code=None, **fields):
    with store.transaction():
        assignments = "".join("," + name + "=%s" for name in fields)
        store.execute(
            "UPDATE moment_images SET state=%s,error_code=%s,updated_at=%s" + assignments
            + " WHERE moment_id=%s AND slot=0 AND state='DISPATCHED'",
            (state, error_code, now, *fields.values(), moment_id))
        store.execute("UPDATE model_usage SET state = %s WHERE request_id = %s",
                      (usage_state, usage_id))
        cloud_budget.sync_reference(store, "model", usage_id)


def _expire(store, moment_id, now, code):
    store.execute("UPDATE moment_images SET state='EXPIRED',error_code=%s,updated_at=%s "
                  "WHERE moment_id=%s AND slot=0 AND state='QUEUED'", (code, now, moment_id))


async def run_once(core):
    """Run at most one queued picture; True when a row changed."""
    store, settings = core.store, core.settings
    now = core.clock()
    row = store.one("SELECT moment_id, created_at FROM moment_images WHERE state = 'QUEUED' "
                    "ORDER BY created_at, moment_id LIMIT 1")
    if row is None:
        return False
    moment_id = row["moment_id"]
    stale = datetime.fromisoformat(row["created_at"]) + timedelta(seconds=QUEUE_SECONDS) \
        <= datetime.fromisoformat(now)
    if not available(core):
        if not stale:
            return False
        with store.transaction():
            _expire(store, moment_id, now, "MOMENT_IMAGE_PROVIDER_UNAVAILABLE")
        return True
    cfg = settings.image_generation
    prompt, usage_id = _prompt(store, moment_id), uid("image_cost")
    price = cfg.endpoint.estimated_request_micro_usd
    try:
        with store.transaction():
            current = store.one("SELECT state FROM moment_images WHERE moment_id = %s "
                                "AND slot = 0 FOR UPDATE", (moment_id,))
            if not current or current["state"] != "QUEUED":
                return True
            try:
                owner_authority.current(store, settings, core.clock, purpose="moments")
                owner_authority.current(store, settings, core.clock, purpose="expression")
            except CoreError:
                _expire(store, moment_id, now, "MOMENT_IMAGE_AUTHORITY_REQUIRED")
                return True
            if prompt is None or stale:
                _expire(store, moment_id, now,
                        "MOMENT_IMAGE_SOURCE_GONE" if prompt is None else "MOMENT_IMAGE_STALE")
                return True
            day, _ = image_generation._day_and_month(settings, now)
            chat = store.one("SELECT COUNT(*) AS n FROM generated_images WHERE created_at >= %s",
                             (day,))["n"]
            if chat + _today(store, settings, now) >= cfg.max_daily_images:
                _expire(store, moment_id, now, "IMAGE_GENERATION_DAILY_LIMIT")
                return True
            cloud_budget.reserve(
                store, settings, reference_type="model", reference_id=usage_id,
                estimated_micro_usd=price, now=now, task_kind="model", task_id=usage_id,
                pricing={"pricing_known": True, "provider": "image-http",
                         "model": cfg.endpoint.model, "request_estimate_micro_usd": price})
            store.insert("model_usage", usage_id=uid("usage"), request_id=usage_id,
                         scenario=SCENARIO, provider="image-http", model=cfg.endpoint.model,
                         billed_micro_usd=price, state="RESERVED", pricing_known=1,
                         is_estimate=1, occurred_at=now)
            store.execute("UPDATE moment_images SET state='RESERVED', usage_request_id=%s, "
                          "updated_at=%s WHERE moment_id=%s AND slot=0",
                          (usage_id, now, moment_id))
    except CoreError as error:
        if error.status != 429:
            raise
        with store.transaction():
            _expire(store, moment_id, core.clock(), error.code)
        return True
    with store.transaction():
        cloud_budget.mark_dispatched(store, "model", usage_id, core.clock())
        store.execute("UPDATE moment_images SET state='DISPATCHED', updated_at=%s "
                      "WHERE moment_id=%s AND slot=0 AND state='RESERVED'",
                      (core.clock(), moment_id))
    generator = HTTPImageGenerator(cfg, core.image_transport)
    try:
        async with asyncio.timeout(cfg.provider_timeout_seconds + 2):
            data = await generator.generate(prompt, cfg.sizes[0])
    except CoreError as error:
        if error.code == "INVALID_IMAGE_PROVIDER_RESPONSE":
            _close(store, moment_id, usage_id, "FAILED", "SETTLED", core.clock(), error.code)
        else:
            _close(store, moment_id, usage_id, "UNKNOWN", "UNKNOWN", core.clock(), error.code)
        return True
    except BaseException:
        _close(store, moment_id, usage_id, "UNKNOWN", "UNKNOWN", core.clock(),
               "IMAGE_PROVIDER_UNCERTAIN")
        raise
    prepared = validate_bytes(data, max_bytes=cfg.max_output_bytes)
    if prepared is None or prepared.mime_type == "image/gif":
        _close(store, moment_id, usage_id, "FAILED", "SETTLED", core.clock(),
               "INVALID_IMAGE_PROVIDER_RESPONSE")
        return True
    _write(store, moment_id, 0, data)
    finished = core.clock()
    with store.transaction():
        updated = store.execute(
            "UPDATE moment_images SET state='READY', mime=%s, sha256=%s, byte_length=%s, "
            "updated_at=%s WHERE moment_id=%s AND slot=0 AND state='DISPATCHED' "
            "AND EXISTS(SELECT 1 FROM moments WHERE moment_id=%s AND state <> 'ERASED')",
            (prepared.mime_type, prepared.sha256, len(data), finished, moment_id, moment_id))
        store.execute("UPDATE model_usage SET state = 'SETTLED' WHERE request_id = %s",
                      (usage_id,))
        cloud_budget.sync_reference(store, "model", usage_id)
    if not updated:
        # Forgotten while drawing: the answer is charged, the bytes are not kept.
        _remove(store, moment_id, 0)
    return True


def slots(store, moment_ids):
    """READY picture slots and still-drawing moments for feed items."""
    if not moment_ids:
        return {}, set()
    rows = store.rows("SELECT moment_id, slot, state FROM moment_images "
                      "WHERE moment_id = ANY(%s::text[]) ORDER BY slot", (sorted(moment_ids),))
    ready, pending = {}, set()
    for row in rows:
        if row["state"] == "READY":
            ready.setdefault(row["moment_id"], []).append(row["slot"])
        elif row["state"] in {"QUEUED", "RESERVED", "DISPATCHED"}:
            pending.add(row["moment_id"])
    return ready, pending


def read(store, moment_id, slot):
    """The bytes of a READY picture on a PUBLISHED moment."""
    target = _path(store, moment_id, slot)
    row = store.one(
        "SELECT i.mime, i.sha256, i.byte_length FROM moment_images i JOIN moments m "
        "USING (moment_id) WHERE i.moment_id = %s AND i.slot = %s AND i.state = 'READY' "
        "AND m.state = 'PUBLISHED'", (moment_id, slot))
    if not row:
        raise CoreError("MOMENT_IMAGE_NOT_FOUND", 404)
    try:
        data = target.read_bytes()
    except OSError as error:
        raise CoreError("MOMENT_IMAGE_NOT_FOUND", 404) from error
    if len(data) != row["byte_length"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise CoreError("MOMENT_IMAGE_NOT_FOUND", 404)
    return data, row["mime"]


def erase(store, moment_ids):
    """Called with the moment's own erasure; replay deletes the same files."""
    if not moment_ids or not store.has_table("moment_images"):
        return
    for row in store.rows("SELECT moment_id, slot FROM moment_images "
                          "WHERE moment_id = ANY(%s::text[])", (sorted(moment_ids),)):
        _remove(store, row["moment_id"], row["slot"])
    store.execute("UPDATE moment_images SET state='ERASED', mime=NULL, sha256=NULL, "
                  "byte_length=NULL WHERE moment_id = ANY(%s::text[])", (sorted(moment_ids),))

