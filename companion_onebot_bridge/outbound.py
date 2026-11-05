import re
import time
from datetime import datetime

from companion_delivery_protocol.authority import validate_authority_ref

from . import account_actions, owner_preflight, reply_segments
from .metrics import send_finished
from .native_actions import prepare
from .onebot_client import bounded_post


def validate_item(item, config, *, companion=False, account_scope=False, voice_scope=False,
                  image_scope=False, generated_image_scope=False):
    if not isinstance(item, dict):
        raise ValueError("INVALID_DELIVERY")
    if not companion and "window_origin_event_id" in item:
        raise ValueError("COMPANION_DELIVERY_SCOPE_REQUIRED")
    try:
        lease_until = datetime.fromisoformat(item["lease_until"])
        if lease_until.utcoffset() is None:
            raise ValueError("INVALID_DELIVERY_LEASE")
    except (KeyError, TypeError, ValueError):
        raise ValueError("INVALID_DELIVERY_LEASE") from None
    for name, prefix in (("message_id", "msg"), ("lease_token", "lease")):
        if not isinstance(item.get(name), str) or not re.fullmatch(
            prefix + r"_[a-f0-9]{32}", item[name]
        ):
            raise ValueError("INVALID_DELIVERY")
    if item.get("delivery_lane", "owner_private") not in {"owner_private", "qq_account"}:
        raise ValueError("INVALID_DELIVERY_LANE")
    if "voice" in item and (not voice_scope or not companion
        or item.get("action_type") not in {"qq.private.reply", "qq.private.proactive"}
        or not isinstance(item["voice"], dict) or not reply_segments.voice_content(item)):
        raise ValueError("VOICE_SCOPE_REQUIRED")
    if account_actions.is_account(item):
        if not account_scope:
            raise ValueError("ACCOUNT_ACTION_SCOPE_REQUIRED")
        account_actions.validate(item, config)
        return
    if (
        item.get("conversation_platform_session_key") != config.origin
        or item.get("action_type") not in (
            {"qq.private.reply", "qq.private.proactive", "qq.private.poke"}
            if companion else {"qq.private.reply"})
    ):
        raise ValueError("OWNER_REPLY_REQUIRED")
    content = item.get("content")
    if item["action_type"] == "qq.private.poke":
        if ("reply_revision" in item or "reply_segment" in item or "window_notice" in item
            or not re.fullmatch(r"poke-response:job_[a-f0-9]{32}", item.get("delivery_key", ""))
            or not re.fullmatch(r"evt_[a-f0-9]{64}", reply_segments.event_id(item))):
            raise ValueError("PRIVATE_POKE_DELIVERY_REQUIRED")
        prepare(content, config.owner_qq_id)
        return
    if "reply_revision" in item and (
        type(item["reply_revision"]) is not int or item["reply_revision"] < 0
    ):
        raise ValueError("INVALID_REPLY_REVISION")
    if "window_notice" in item and (
        not companion or item["window_notice"] != "commitment"
        or item["action_type"] != "qq.private.reply" or "reply_revision" in item
    ):
        raise ValueError("INVALID_NOTICE_SCOPE")
    if (
        not isinstance(content, dict)
        or set(content) - {"reply_to", "reply_part"} not in ({"text"}, {"text", "image"},
                                               {"text", "generated_image"})
        or not isinstance(content["text"], str)
        or not (1 <= len(content["text"]) <= 20000 or reply_segments.image_only(item))
    ):
        raise ValueError("TEXT_DELIVERY_REQUIRED")
    if "reply_part" in content:
        envelope = reply_segments.envelope(item)
        if not envelope or envelope["version"] != 3:
            raise ValueError("INVALID_REPLY_SEGMENT")
    if "reply_to" in content:
        reply_segments.quote(item)
    if "generated_image" in content:
        from .generated_image_http import valid_reference

        if (not generated_image_scope or not companion
                or item["action_type"] != "qq.private.reply" or "voice" in item
                or not valid_reference(content["generated_image"])):
            raise ValueError("GENERATED_IMAGE_SCOPE_REQUIRED")
    if "image" in content:
        image = content["image"]
        if (not (companion or image_scope) or item["action_type"] != "qq.private.reply"
            or not isinstance(image, dict)
            or not isinstance(image.get("category"), str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", image["category"])):
            raise ValueError("EXPRESSION_SCOPE_REQUIRED")
        if image.get("source") == "companion.meme_catalog":
            if (set(image) != {"source", "category", "candidate_id", "offer_key"}
                or not isinstance(image["candidate_id"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", image["candidate_id"])
                or image["offer_key"] != "meme:" + reply_segments.event_id(item)):
                raise ValueError("EXPRESSION_SCOPE_REQUIRED")
        elif image.get("source") != "astrbot.meme_manager" or set(image) != {"source", "category"}:
            raise ValueError("EXPRESSION_SCOPE_REQUIRED")
    reply_segments.envelope(item)


async def deliver_tick(client, onebot, spool, *, metrics=None, guard=None,
                       delivery_lane="owner_private"):
    # Finish reporting interrupted sends before taking new work; never repeat send I/O.
    if await spool.flush_result(client, runtime=getattr(guard, "runtime", None)):
        return True
    if not onebot.config.delivery_enabled and guard is None:
        return False
    if (delivery_lane == "owner_private" and guard is not None
        and hasattr(guard, "waiting_for_segment") and guard.waiting_for_segment()):
        return False
    request = {"worker_id": getattr(onebot, "worker_id", "onebot-direct"), "limit": 1}
    if guard is not None and getattr(guard, "authority", None) is not None:
        request["authority"] = validate_authority_ref(guard.authority)
    if delivery_lane == "qq_account":
        if guard is None or not getattr(onebot, "account_scope", False):
            raise ValueError("ACCOUNT_ACTION_SCOPE_REQUIRED")
        if getattr(guard, "authority", None) is not None:
            permission = await guard.runtime.check(client, purpose="account_actions")
            await guard.runtime.check(client, purpose="social")
            request = account_actions.lease_request_owner(guard.authority,
                permission["capability_policies"]["account_actions"])
        else:
            request = account_actions.lease_request(guard.window)
    elif delivery_lane != "owner_private":
        raise ValueError("INVALID_DELIVERY_LANE")
    voice_scope = bool(getattr(onebot, "voice_scope", False))
    batch = await bounded_post(client, "/internal/v1/deliveries/lease", request,
        max_bytes=spool.config.max_voice_lease_bytes if voice_scope else 65536)
    if not isinstance(batch.get("items"), list) or len(batch["items"]) > 1:
        raise ValueError("INVALID_DELIVERY_LEASE")
    if not batch["items"]:
        return False
    item = batch["items"][0]
    return await deliver_item(client, onebot, spool, item, metrics=metrics, guard=guard,
                              delivery_lane=delivery_lane)


async def deliver_item(client, onebot, spool, item, *, metrics=None, guard=None,
                       delivery_lane="owner_private"):
    """Release operation-local prepared media on every exit, including cancellation."""
    try:
        return await _deliver_item(client, onebot, spool, item, metrics=metrics, guard=guard,
                                   delivery_lane=delivery_lane)
    except owner_preflight.Expired as expiry:
        owner_preflight.save(guard, item, expiry.stage)
        await spool.flush_result(client, runtime=guard.runtime)
        return True
    finally:
        release = getattr(onebot, "release_prepared", None)
        if release is not None:
            release()


async def _deliver_item(client, onebot, spool, item, *, metrics=None, guard=None,
                        delivery_lane="owner_private"):
    """The single leased-item dispatch lifecycle for all transport drivers."""
    if "authority" in item:
        expected = validate_authority_ref(item["authority"])
        if guard is None or getattr(guard, "authority", None) is None or (
            validate_authority_ref(guard.authority) != expected
        ):
            raise ValueError("OWNER_AUTHORITY_GUARD_REQUIRED")
    voice_scope = bool(getattr(onebot, "voice_scope", False))
    if item.get("delivery_lane", "owner_private") != delivery_lane:
        raise ValueError("INVALID_DELIVERY_LANE")
    validate_item(item, onebot.config, companion=getattr(onebot, "companion", False),
                  account_scope=getattr(onebot, "account_scope", False), voice_scope=voice_scope,
                  image_scope=getattr(onebot, "image_scope", False),
                  generated_image_scope=getattr(onebot, "generated_image_scope", False))
    probe_before_reservation = getattr(onebot, "probe_before_reservation", False)
    if probe_before_reservation and not (await onebot.probe())["connection_ready"]:
        raise ValueError("ONEBOT_NOT_READY")
    if hasattr(onebot, "prepare_delivery"):
        await onebot.prepare_delivery(item)
    if guard is not None:
        guard.reserve(item)
    trace = spool.trace_for(item.get("delivery_key"))
    if metrics:
        metrics.emit("t_outbox_lease", trace)
    message_id = item["message_id"]
    try:
        if not probe_before_reservation and not (await onebot.probe())["connection_ready"]:
            raise ValueError("ONEBOT_NOT_READY")
        path = f"/internal/v1/deliveries/{message_id}/begin-send"
        body = {"lease_token": item["lease_token"]}
        if guard is not None and hasattr(guard, "preflight_post"):
            started = await guard.preflight_post(path, body)
        else:
            started = await bounded_post(client, path, body)
        if started != {"state": "SENDING"}:
            raise ValueError("INVALID_BEGIN_SEND_ACK")
    except owner_preflight.Expired:
        raise
    except Exception:
        if guard is not None and await guard.cancel_before_send(item):
            return True
        raise
    if metrics:
        metrics.emit("t_begin_send", trace)
    # The durable fallback is uncertain BEFORE starting any external send I/O.
    spool.start_delivery(item)
    provider_id = None
    send_start = time.perf_counter_ns()
    try:
        try:
            provider_id = await onebot.send_content(item["content"]) if hasattr(
                onebot, "send_content") else await onebot.send_text(item["content"]["text"])
        except BaseException as error:
            send_finished(send_start, error)
            raise
        else:
            send_finished(send_start)
    except owner_preflight.Expired:
        raise
    except Exception:
        if guard is not None and await guard.cancel_before_send(item):
            return True
    ack_ns = time.perf_counter_ns()
    spool.finish_delivery(message_id, provider_id,
                          error_code=getattr(onebot, "preflight_error", None))
    if metrics:
        metrics.emit(
            "t_onebot_send_ack" if provider_id else "delivery_uncertain",
            trace,
            elapsed_ms=(ack_ns - send_start) / 1e6,
            at_ns=ack_ns,
        )
        metrics.delivered(item.get("delivery_key", ""), provider_id, ack_ns,
                          segment=item.get("reply_segment"))
    await spool.flush_result(client, runtime=getattr(guard, "runtime", None))
    if guard is not None:
        guard.complete(item, provider_id)
    return True
