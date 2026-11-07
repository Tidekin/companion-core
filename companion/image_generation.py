"""Model-requested pictures for owner private chat, delivered with the same reply.

The SDK tool reserves one priced request per chat job, calls the adapter and keeps
the validated PNG/JPEG only in a short-lived spool in the local state directory. Core's final
reply transaction attaches it to the job's last Outbox part; the Bridge reads the
bytes under that item's delivery lease. An unknown provider outcome keeps its
reservation and is never retried; erasure and expiry remove the spooled bytes.
"""

import base64
import hashlib
import json
import os
import re
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field

from companion import (
    cloud_budget,
    owner_authority,
    owner_delivery,
    paid_budget,
    reply_revision,
    transport,
)
from companion.budget import total_monthly
from companion.contracts import CoreError
from companion.media_provider import HTTPImageGenerator
from companion.owner_authority_protocol import AuthorityReference
from companion.store import uid
from companion_meme_protocol import validate_bytes

SCENARIO = "image_generation"
IMAGE_ID = re.compile(r"gimg_[a-f0-9]{32}")
LIVE_OUTBOX = ("PENDING", "RETRY_WAIT", "LEASED", "SENDING")


def options(store, settings, job_id):
    """The provider choice pinned to this job, so a Console change applies to the next chat."""
    from companion import runtime_config

    return runtime_config.effective(
        settings, runtime_config.check(store, settings, "job:" + job_id)
    ).image_generation


def enabled(settings, config):
    return bool(config.enabled and settings.owner_runtime.mode == "ENABLED")


def available(core, job):
    """Real provider dispatch awaits its own approval (F8 in docs/design-future/p8b1-backlog.md).

    Until then only an injected synthetic transport can run, as for paid media input.
    """
    return enabled(core.settings, options(core.store, core.settings, job["job_id"])) and (
        isinstance(getattr(core, "image_transport", None), httpx.MockTransport)
    )


def directory(store):
    return Path(store.state_directory).resolve() / "generated-images"


def _path(store, image_id):
    if not IMAGE_ID.fullmatch(image_id):
        raise CoreError("GENERATED_IMAGE_UNAVAILABLE", 410)
    root = directory(store)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise CoreError("GENERATED_IMAGE_UNAVAILABLE", 410)
    target = root / (image_id + ".img")
    if target.is_symlink():
        raise CoreError("GENERATED_IMAGE_UNAVAILABLE", 410)
    return target


def _write(store, image_id, data):
    target = _path(store, image_id)
    target.parent.mkdir(mode=0o700, exist_ok=True)
    partial = target.with_suffix(".tmp")
    with open(partial, "xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, target)


def _read(store, image_id, max_bytes):
    target = _path(store, image_id)
    try:
        descriptor = os.open(
            target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        )
    except OSError as error:
        raise CoreError("GENERATED_IMAGE_UNAVAILABLE", 410) from error
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(max_bytes + 1)
    if not data or len(data) > max_bytes:
        raise CoreError("GENERATED_IMAGE_UNAVAILABLE", 410)
    return data


def cleanup(store, now, *, entries=None, limit=None):
    """Delete spooled bytes that no live, unexpired Outbox item can still send."""
    root = directory(store)
    if root.is_symlink() or not root.is_dir() or not _table(store):
        return 0
    if entries is None:
        with os.scandir(root) as entries:
            return cleanup(store, now, entries=entries, limit=limit)
    processed = 0
    for entry in islice(entries, limit):
        processed += 1
        path = Path(entry.path)
        image_id = path.name.split(".", 1)[0]
        with nullcontext() if store.in_transaction else store.transaction():
            row = (
                store.one(
                    (
                        'SELECT g.state, g.expires_at, g.message_id, o.state AS '
                        'outbox_state, j.state AS job_state, j.lease_until FROM '
                        'generated_images AS g JOIN jobs AS j USING (job_id) '
                        'LEFT JOIN outbox AS o USING (message_id) WHERE '
                        'image_id = %s'
                    ),
                    (image_id,),
                )
                if IMAGE_ID.fullmatch(image_id) and path.suffix in {".img", ".tmp"}
                else None
            )
            # _write() prepares bytes before the final result transaction. A live
            # dispatch owns its partial/final file until that original lease ends.
            writing = (
                row
                and row["state"] == "DISPATCHED"
                and row["job_state"] == "RUNNING"
                and row["lease_until"]
                and row["lease_until"] > now
            )
            keep = writing or (
                row
                and path.suffix == ".img"
                and row["state"] == "SUCCEEDED"
                and row["expires_at"] > now
                and (row["message_id"] is None or row["outbox_state"] in LIVE_OUTBOX)
            )
            if not keep and not path.is_dir():
                path.unlink(missing_ok=True)
    return processed


def _table(store):
    return bool(
        store.one(

                'SELECT 1 FROM pg_catalog.pg_class WHERE '
                'relnamespace=current_schema()::regnamespace AND '
                "relname='generated_images' AND relkind IN ('r','p')"

        )
    )


def erase(store, event_ids):
    """Forget the prompt digest and picture identity; accounting stays in model_usage."""
    if not _table(store):
        return
    for event_id in event_ids:
        store.execute(
            "UPDATE generated_images SET state = 'ERASED', prompt_sha256 = '', mime = NULL, sha256 "
            "= NULL, byte_length = NULL, expires_at = NULL WHERE event_id = %s",
            (event_id,),
        )


def _refused(code, **detail):
    return {
        "state": "REFUSED",
        "error_code": code,
        **detail,
        "note": "这次没有生成图片；用文字回应，不要说图片已经发出。",
    }


def _recent_owner_text(store, job):
    rows = store.rows(
        (
            'SELECT body_json FROM events WHERE conversation_id = '
            "%s AND actor_id = 'owner' AND redacted = 0 AND "
            'record_sequence <= (SELECT record_sequence AS rowid '
            'FROM events WHERE event_id = %s) ORDER BY '
            'record_sequence DESC NULLS LAST LIMIT 6'
        ),
        (job["conversation_id"], job["event_id"]),
    )
    return "\n".join(json.loads(row["body_json"]).get("text", "") for row in rows)


def _binding(core, job, call_id, arguments):
    row = core.store.one(
        "SELECT t.job_id AS job_id, t.tool_call_id AS tool_call_id, t.tool_name AS tool_name, "
        "t.args_sha256 AS args_sha256, t.model_request_id AS model_request_id, t.state AS state, "
        "t.result_json AS result_json, t.metadata_json AS metadata_json, t.request_id AS "
        "request_id, t.created_at AS created_at, t.updated_at AS updated_at, t.voice_job_id AS "
        "voice_job_id, m.state AS primary_state, m.scenario AS scenario, m.job_id AS primary_job "
        "FROM agent_tool_calls AS t JOIN model_usage AS m ON m.request_id = t.model_request_id "
        "WHERE t.job_id = %s AND t.tool_call_id = %s",
        (job["job_id"], call_id),
    )
    if (
        not row
        or row["state"] != "STARTED"
        or row["tool_name"] != "generate_image"
        or row["args_sha256"] != paid_budget.sha(arguments)
        or row["primary_state"] != "SETTLED"
        or row["scenario"] != "chat"
        or row["primary_job"] != job["job_id"]
    ):
        raise CoreError("IMAGE_GENERATION_TOOL_INVALID", 403)
    return row


def _day_and_month(settings, now):
    local = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    return (
        day.astimezone(UTC).isoformat(timespec="microseconds"),
        month.astimezone(UTC).isoformat(timespec="microseconds"),
    )


def _limit(store, settings, cfg, job, now):
    """Return a refusal code for an ordinary limit; the caller holds the transaction."""
    if store.one("SELECT 1 FROM generated_images WHERE job_id = %s", (job["job_id"],)):
        return "IMAGE_GENERATION_ONCE_PER_REPLY"
    day, month = _day_and_month(settings, now)
    if (
        store.one("SELECT COUNT(*) AS n FROM generated_images WHERE created_at >= %s", (day,))["n"]
        >= cfg.max_daily_images
    ):
        return "IMAGE_GENERATION_DAILY_LIMIT"
    since = (datetime.fromisoformat(now) - timedelta(seconds=cfg.cooldown_seconds)).isoformat()
    if cfg.cooldown_seconds and store.one(
        "SELECT 1 FROM generated_images WHERE conversation_id = %s AND created_at > %s",
        (job["conversation_id"], since),
    ):
        return "IMAGE_GENERATION_COOLDOWN"
    price = cfg.endpoint.max_request_micro_usd
    daily = store.one(
        (
            "SELECT COALESCE(SUM(billed_micro_usd),0) n FROM "
            "model_usage WHERE scenario=%s AND occurred_at>=%s"
        ),
        (SCENARIO, day),
    )["n"]
    if (
        daily + price > cfg.daily_micro_usd
        or total_monthly(store, month) + price > settings.monthly_limit_micro_usd
    ):
        return "IMAGE_GENERATION_BUDGET_EXHAUSTED"
    return None


def _close(store, image_id, usage_id, state, usage_state, now, error_code=None, **fields):
    with store.transaction():
        assignments = "".join("," + name + "=%s" for name in fields)
        store.execute(
            "UPDATE generated_images SET state=%s,error_code=%s,updated_at=%s"
            + assignments
            + " WHERE image_id=%s AND state='DISPATCHED'",
            (state, error_code, now, *fields.values(), image_id),
        )
        store.execute(
            "UPDATE model_usage SET state = %s WHERE request_id = %s", (usage_state, usage_id)
        )
        cloud_budget.sync_reference(store, "model", usage_id)


async def generate(core, job, call_id, arguments, check):
    """Run one bounded request for the leased owner job; `check` rechecks its sources."""
    from companion.expression import SERIOUS

    store, settings = core.store, core.settings
    cfg = options(store, settings, job["job_id"])
    prompt, size = arguments["prompt"].strip(), arguments["size"]
    if not prompt:
        return _refused("IMAGE_PROMPT_REQUIRED")
    if size not in cfg.sizes:
        return _refused("IMAGE_SIZE_NOT_ALLOWED", allowed_sizes=list(cfg.sizes))
    check("expression")
    if SERIOUS.search(_recent_owner_text(store, job) + "\n" + prompt):
        return _refused("IMAGE_CONTEXT_SERIOUS")
    now = core.clock()
    current = store.one("SELECT lease_until FROM jobs WHERE job_id = %s", (job["job_id"],))
    if datetime.fromisoformat(current["lease_until"]) - datetime.fromisoformat(now) < timedelta(
        seconds=cfg.provider_timeout_seconds + 10
    ):
        return _refused("IMAGE_GENERATION_LEASE_TOO_SHORT")
    image_id, usage_id = uid("gimg"), uid("image_cost")
    price = cfg.endpoint.max_request_micro_usd
    try:
        with store.transaction():
            check("expression")
            _binding(core, job, call_id, arguments)
            if refusal := _limit(store, settings, cfg, job, now):
                return _refused(refusal)
            cloud_budget.reserve(
                store,
                settings,
                reference_type="model",
                reference_id=usage_id,
                estimated_micro_usd=price,
                now=now,
                task_kind="model",
                task_id=usage_id,
                pricing={
                    "pricing_known": True,
                    "provider": "image-http",
                    "model": cfg.endpoint.model,
                    "request_estimate_micro_usd": price,
                },
            )
            store.insert(
                "model_usage",
                usage_id=uid("usage"),
                request_id=usage_id,
                scenario=SCENARIO,
                provider="image-http",
                model=cfg.endpoint.model,
                billed_micro_usd=price,
                state="RESERVED",
                pricing_known=1,
                is_estimate=1,
                occurred_at=now,
            )
            store.insert(
                "generated_images",
                image_id=image_id,
                job_id=job["job_id"],
                event_id=job["event_id"],
                conversation_id=job["conversation_id"],
                tool_call_id=call_id,
                state="RESERVED",
                size=size,
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                usage_request_id=usage_id,
                created_at=now,
                updated_at=now,
            )
            store.execute(
                "UPDATE agent_tool_calls SET metadata_json = %s, updated_at = %s WHERE job_id = %s "
                "AND tool_call_id = %s AND state = 'STARTED'",
                (
                    json.dumps(
                        {"kind": "generated_image", "image_id": image_id, "request_id": usage_id}
                    ),
                    now,
                    job["job_id"],
                    call_id,
                ),
            )
    except CoreError as error:
        if error.status == 429:
            return _refused(error.code)
        raise
    with store.transaction():
        check("expression")
        cloud_budget.mark_dispatched(store, "model", usage_id, core.clock())
        store.execute(
            "UPDATE generated_images SET state = 'DISPATCHED', updated_at = %s WHERE "
            "image_id = %s AND state = 'RESERVED'",
            (core.clock(), image_id),
        )
    generator = HTTPImageGenerator(cfg, core.image_transport)
    try:
        import asyncio

        async with asyncio.timeout(cfg.provider_timeout_seconds + 2):
            data = await generator.generate(prompt, size)
    except CoreError as error:
        if error.code == "INVALID_IMAGE_PROVIDER_RESPONSE":
            # A complete answer was received and is charged, but carries no usable image.
            _close(store, image_id, usage_id, "FAILED", "SETTLED", core.clock(), error.code)
            return {
                "state": "FAILED",
                "error_code": error.code,
                "note": "图片没有生成成功；用文字回应。",
            }
        _close(store, image_id, usage_id, "UNKNOWN", "UNKNOWN", core.clock(), error.code)
        return {
            "state": "UNKNOWN",
            "error_code": error.code,
            "note": "生图服务没有给出确定结果，不会自动重试；用文字回应，别说图片已发出。",
        }
    except BaseException:
        _close(
            store,
            image_id,
            usage_id,
            "UNKNOWN",
            "UNKNOWN",
            core.clock(),
            "IMAGE_PROVIDER_UNCERTAIN",
        )
        raise
    prepared = validate_bytes(data, max_bytes=cfg.max_output_bytes)
    if prepared is None or prepared.mime_type == "image/gif":
        _close(
            store,
            image_id,
            usage_id,
            "FAILED",
            "SETTLED",
            core.clock(),
            "INVALID_IMAGE_PROVIDER_RESPONSE",
        )
        return {
            "state": "FAILED",
            "error_code": "INVALID_IMAGE_PROVIDER_RESPONSE",
            "note": "图片没有生成成功；用文字回应。",
        }
    _write(store, image_id, data)
    finished = core.clock()
    expires = (
        datetime.fromisoformat(finished) + timedelta(seconds=cfg.retention_seconds)
    ).isoformat()
    try:
        with store.transaction():
            check("expression")
            store.execute(
                "UPDATE generated_images SET state = 'SUCCEEDED', mime = %s, sha256 = %s, "
                "byte_length = %s, expires_at = %s, updated_at = %s WHERE image_id = %s AND state "
                "= 'DISPATCHED'",
                (prepared.mime_type, prepared.sha256, len(data), expires, finished, image_id),
            )
            store.execute(
                "UPDATE model_usage SET state = 'SETTLED' WHERE request_id = %s", (usage_id,)
            )
            cloud_budget.sync_reference(store, "model", usage_id)
    except BaseException:
        # The answer arrived and is charged; a changed owner grant keeps it unsendable.
        _close(
            store,
            image_id,
            usage_id,
            "FAILED",
            "SETTLED",
            core.clock(),
            "IMAGE_GENERATION_SOURCE_CHANGED",
        )
        _path(store, image_id).unlink(missing_ok=True)
        raise
    return {
        "state": "SUCCEEDED",
        "image_id": image_id,
        "size": size,
        "delivery": "attached_to_this_reply",
        "note": "图片会附在这次回复的最后一段一起发出；正文不要贴链接，也不要声称对方已经看到。",
    }


def attach(store, job, message_ids, now):
    """Bind this job's picture to its last reply part inside the enqueue transaction."""
    if not message_ids or job["kind"] != "chat" or not _table(store):
        return None
    row = store.one(
        "SELECT generated_images.image_id AS image_id, generated_images.job_id AS "
        "job_id, generated_images.event_id AS event_id, generated_images.conversation_id AS "
        "conversation_id, generated_images.tool_call_id AS tool_call_id, generated_images.state AS "
        "state, generated_images.size AS size, generated_images.prompt_sha256 AS prompt_sha256, "
        "generated_images.usage_request_id AS usage_request_id, generated_images.mime AS mime, "
        "generated_images.sha256 AS sha256, generated_images.byte_length AS byte_length, "
        "generated_images.message_id AS message_id, generated_images.error_code AS error_code, "
        "generated_images.expires_at AS expires_at, generated_images.created_at AS created_at, "
        "generated_images.updated_at AS updated_at FROM generated_images AS generated_images WHERE "
        "generated_images.job_id = %s AND generated_images.state = 'SUCCEEDED' AND "
        "generated_images.message_id IS NULL",
        (job["job_id"],),
    )
    if not row or row["expires_at"] <= now or not _path(store, row["image_id"]).is_file():
        return None
    message_id = message_ids[-1]
    outbox = store.one(
        "SELECT outbox.message_id AS message_id, outbox.origin_event_id AS "
        "origin_event_id, outbox.conversation_id AS conversation_id, outbox.action_type AS "
        "action_type, outbox.content_json AS content_json, outbox.content_sha256 AS "
        "content_sha256, outbox.approval_id AS approval_id, outbox.state AS state, "
        "outbox.delivery_key AS delivery_key, outbox.lease_owner AS lease_owner, "
        "outbox.lease_until AS lease_until, outbox.attempt_count AS attempt_count, "
        "outbox.provider_message_id AS provider_message_id, outbox.last_error_code AS "
        "last_error_code, outbox.created_at AS created_at, outbox.updated_at AS updated_at, "
        "outbox.sent_at AS sent_at, outbox.lease_token AS lease_token, outbox.next_attempt_at AS "
        "next_attempt_at, outbox.reply_group_id AS reply_group_id, outbox.segment_index AS "
        "segment_index, outbox.segment_count AS segment_count, outbox.reply_group_sha256 AS "
        "reply_group_sha256, outbox.segment_interval_ms AS segment_interval_ms, "
        "outbox.delivery_lane AS delivery_lane, outbox.native_ack_json AS native_ack_json, "
        "outbox.authority_json AS authority_json FROM outbox AS outbox WHERE outbox.message_id = "
        "%s",
        (message_id,),
    )
    if (
        not outbox
        or outbox["state"] != "PENDING"
        or outbox["action_type"] != "qq.private.reply"
        or outbox["origin_event_id"] != job["event_id"]
    ):
        return None
    content = json.loads(outbox["content_json"])
    # A generated picture replaces a meme pick; one image per QQ message.
    content.pop("image", None)
    content["generated_image"] = reference(row)
    transport.set_content(store, message_id, content)
    store.execute('UPDATE generated_images SET message_id=%s,updated_at=%s WHERE image_id=%s',
                  (message_id, now, row["image_id"]))
    return message_id


def reference(row):
    return {
        "image_id": row["image_id"],
        "sha256": row["sha256"],
        "mime": row["mime"],
        "byte_length": row["byte_length"],
    }


class AssetRequest(AuthorityReference):
    event_id: str = Field(pattern=r"^evt_[a-f0-9]{64}$")
    reply_revision: int = Field(strict=True, ge=1, lt=2**53)
    lease_token: str = Field(pattern=r"^lease_[a-f0-9]{32}$")
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def check_send(store, settings, clock, authority):
    """Sending a picture needs the current expression grant; text-only fallback is allowed."""
    owner_authority.current(store, settings, clock, purpose="expression", authority=authority)


def _deliverable(store, clock, message_id):
    row = store.one(
        "SELECT outbox.message_id AS message_id, outbox.origin_event_id AS "
        "origin_event_id, outbox.conversation_id AS conversation_id, outbox.action_type AS "
        "action_type, outbox.content_json AS content_json, outbox.content_sha256 AS "
        "content_sha256, outbox.approval_id AS approval_id, outbox.state AS state, "
        "outbox.delivery_key AS delivery_key, outbox.lease_owner AS lease_owner, "
        "outbox.lease_until AS lease_until, outbox.attempt_count AS attempt_count, "
        "outbox.provider_message_id AS provider_message_id, outbox.last_error_code AS "
        "last_error_code, outbox.created_at AS created_at, outbox.updated_at AS updated_at, "
        "outbox.sent_at AS sent_at, outbox.lease_token AS lease_token, outbox.next_attempt_at AS "
        "next_attempt_at, outbox.reply_group_id AS reply_group_id, outbox.segment_index AS "
        "segment_index, outbox.segment_count AS segment_count, outbox.reply_group_sha256 AS "
        "reply_group_sha256, outbox.segment_interval_ms AS segment_interval_ms, "
        "outbox.delivery_lane AS delivery_lane, outbox.native_ack_json AS native_ack_json, "
        "outbox.authority_json AS authority_json FROM outbox AS outbox WHERE outbox.message_id = "
        "%s",
        (message_id,),
    )
    ref = json.loads(row["content_json"]).get("generated_image") if row else None
    image = (
        store.one(
            (
                'SELECT generated_images.image_id AS image_id, '
                'generated_images.job_id AS job_id, '
                'generated_images.event_id AS event_id, '
                'generated_images.conversation_id AS conversation_id, '
                'generated_images.tool_call_id AS tool_call_id, '
                'generated_images.state AS state, generated_images.size '
                'AS size, generated_images.prompt_sha256 AS '
                'prompt_sha256, generated_images.usage_request_id AS '
                'usage_request_id, generated_images.mime AS mime, '
                'generated_images.sha256 AS sha256, '
                'generated_images.byte_length AS byte_length, '
                'generated_images.message_id AS message_id, '
                'generated_images.error_code AS error_code, '
                'generated_images.expires_at AS expires_at, '
                'generated_images.created_at AS created_at, '
                'generated_images.updated_at AS updated_at FROM '
                'generated_images AS generated_images WHERE '
                'generated_images.image_id = %s'
            ),
            (ref["image_id"],),
        )
        if isinstance(ref, dict)
        else None
    )
    if (
        not image
        or image["state"] != "SUCCEEDED"
        or image["message_id"] != message_id
        or reference(image) != ref
        or image["expires_at"] <= clock()
    ):
        raise CoreError("GENERATED_IMAGE_UNAVAILABLE", 410)
    return image


def read_asset(store, settings, clock, message_id, body):
    """Return the picture bytes only to the Bridge holding this item's delivery lease."""
    with store.transaction():
        permission = owner_delivery.check_delivery(store, settings, clock, message_id)
        owner_authority.current(
            store, settings, clock, purpose="expression", authority=body.authority
        )
        _, receipt = owner_delivery.asset_lease(store, clock, message_id, body, permission)
        image = _deliverable(store, clock, message_id)
        if not enabled(settings, options(store, settings, image["job_id"])):
            raise CoreError("IMAGE_GENERATION_DISABLED", 403)
        data = _read(store, image["image_id"], 4 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != image["sha256"] or len(data) != image["byte_length"]:
            raise CoreError("GENERATED_IMAGE_UNAVAILABLE", 410)
        return {
            **receipt,
            **reference(image),
            "data_base64": base64.b64encode(data).decode(),
        }


def toolset(core, job, snapshots):
    from typing import Annotated, Literal

    from pydantic_ai import RunContext
    from pydantic_ai.toolsets import FunctionToolset

    from companion.agent_runtime import check_conversation

    def check(purpose):
        view = check_conversation(core, job, snapshots, purpose=purpose)
        if not reply_revision.owner_input(core.store, job["event_id"]) or not core.store.one(
            "SELECT 1 FROM conversations WHERE conversation_id = %s AND scope = 'private' "
            "AND owner_actor_id = 'owner'",
            (job["conversation_id"],),
        ):
            raise CoreError("CONVERSATION_OWNER_SCOPE_REQUIRED", 403)
        return view

    sizes = options(core.store, core.settings, job["job_id"]).sizes
    Size = Literal[sizes]  # noqa: N806 - the schema lists only configured sizes.

    async def generate_image(
        ctx: RunContext,
        prompt: Annotated[str, Field(min_length=1, max_length=1000)],
        size: Size,
    ) -> dict:
        """Draw one picture to send with this reply, when the owner asks for one.

        At most once per reply. The picture is attached to the end of your reply; do not
        paste links or claim the owner has seen it. A REFUSED/FAILED/UNKNOWN state means
        no picture will be sent: answer in words and never call again in this reply.

        Args:
            prompt: A self-contained visual description; never a URL or local path.
            size: Pixel size of the picture; use the first option unless the owner asks.
        """
        return await generate(core, job, ctx.tool_call_id, {"prompt": prompt, "size": size}, check)

    return FunctionToolset([generate_image], sequential=True, max_retries=1)
