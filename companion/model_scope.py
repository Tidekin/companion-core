"""Scope checks for an existing model reservation, from preparation through transport.

The job and media owners validate their own sources. Budget calls these checks at
the existing transaction boundaries and holds the reservation and accounting state.
"""

from dataclasses import dataclass
from datetime import datetime

import httpx

from companion import owner_authority
from companion.contracts import CoreError
from companion.store import encode
from companion.video_input import SCENARIOS as VIDEO_SCENARIOS


@dataclass(frozen=True)
class ReservationInput:
    logical_id: str | None
    video_binding: dict | None = None
    native_image_chat: bool = False


def prepare_input(store, settings, clock, wire, approval, request_id, logical_id,
                  job_id, scenario, messages, tools, cloud_kind, cloud_id):
    if scenario == "meme_caption":
        from companion import meme_input

        logical_id = logical_id or job_id
        meme_input.contract(settings, job_id, logical_id)
    elif scenario in VIDEO_SCENARIOS:
        from companion.bili_video import reserve_model_input

        logical_id = logical_id or job_id
        binding = reserve_model_input(store, settings, clock, request_id, job_id, scenario,
                                      logical_id, wire, messages, tools, cloud_kind, cloud_id)
        return ReservationInput(logical_id, video_binding=binding)
    elif scenario == "content_vision" or any(
        isinstance(message, dict) and isinstance(message.get("content"), list)
        for message in messages
    ):
        from companion import image_input

        if scenario == "evaluation":
            image_input.check_evaluation_input(approval, messages)
        else:
            logical_id = logical_id or job_id
            native = image_input.check_chat_scope(store, settings, job_id, logical_id, scenario)
            return ReservationInput(logical_id, native_image_chat=native)
    return ReservationInput(logical_id)


def check_sources(store, settings, clock, job_id, messages, inputs, source_check):
    from companion import reply_revision

    job = store.one("SELECT * FROM jobs WHERE job_id=?", (job_id,)) if job_id is not None else None
    if job:
        reply_revision.check_job(store, job)
    if source_check is not None:
        source_check()
    if inputs.native_image_chat:
        from companion import agent_images

        if not job or job["kind"] != "chat":
            raise CoreError("MODEL_IMAGE_CHAT_JOB_REQUIRED", 503)
        owner_authority.check_job(store, settings, clock, job, purpose="image", executing=True)
        agent_images.check_payload(store, settings, clock, job, messages)


def admit(store, settings, clock, now, job_id, scenario, request_id, inputs, messages,
          estimate, estimated_input):
    from companion import live_text

    current_time = clock or (lambda: now)
    if scenario == "meme_caption":
        from companion import meme_input

        meme_input.check_attempt(store, job_id)
    if inputs.video_binding is not None:
        from companion.bili_video import model_binding

        if model_binding(store, settings, current_time, request_id, job_id=job_id,
                         scenario=scenario, logical_request_id=inputs.logical_id
                         ) != inputs.video_binding:
            raise CoreError("BILI_VIDEO_MODEL_BINDING_CHANGED", 403)

    if scenario == "gateway":
        from companion import gateway_admission

        gateway_admission.admit(store, settings, current_time, request_id,
                                estimate, estimated_input)
        return None
    if scenario == "evaluation":
        from companion import evaluation

        evaluation.admit(store, settings, clock, job_id, request_id, messages,
                         estimate, estimated_input)
        return None
    owner = None
    if owner_authority.required(store, settings):
        owner = owner_authority.admit_model(store, settings, current_time, job_id, scenario)
    else:
        live_text.reserve_model(store, settings, clock, job_id, scenario)
    if scenario == "rem":
        from companion import rem

        rem.admit(store, settings, current_time, job_id, estimated_input, estimate)
    return owner


def admit_attempt(store, settings, clock, now, job_id, scenario, request_id, inputs, messages,
                  source_check):
    if scenario == "social_autonomy":
        from companion.social_autonomy import SocialAutonomy

        if job_id is not None and source_check is None:
            raise CoreError("SOCIAL_MODEL_SCOPE_REQUIRED", 403)
        SocialAutonomy(store, settings, clock=clock).admit_model(
            request_id, inputs.logical_id, messages, job_id=job_id,
        )
    elif scenario in {"content_vision", *VIDEO_SCENARIOS}:
        from companion import image_input

        image_input.check_attempt(store, job_id, scenario, inputs.video_binding)
    elif scenario == "activity":
        from companion import notices

        notices.admit_activity(store, settings, job_id, now)


def journal_binding(store, scenario, request_id):
    from companion import gateway_admission

    if scenario == "gateway":
        return {"gateway": gateway_admission.binding(store, request_id)}
    return {}


def freeze_authority(store, settings, clock, scenario, job_id, request_id, owner):
    if scenario == "evaluation":
        owner = owner_authority.check_job(
            store, settings, clock, store.one("SELECT * FROM jobs WHERE job_id=?", (job_id,)),
            purpose="evaluation", executing=True,
        )
    if owner is not None:
        store.execute("UPDATE model_admissions SET authority_json=? WHERE request_id=?",
                      (encode(owner.authority.as_dict()), request_id))
    elif scenario != "gateway":
        from companion.private_life import freeze_model

        freeze_model(store, settings, request_id)


def check(store, settings, clock, request_id, scenario, *, yield_to_chat=True):
    from companion import evaluation, live_text

    if scenario == "gateway":
        from companion import gateway_admission

        return gateway_admission.check_model(store, settings, clock, request_id)
    if scenario == "evaluation":
        return evaluation.check_request(store, settings, clock, request_id)
    if owner_authority.required(store, settings):
        return owner_authority.check_model(store, settings, clock, request_id,
                                           yield_to_chat=yield_to_chat)
    return live_text.check_model(store, settings, clock, request_id)


def check_media(store, settings, clock, request_id, scenario, row):
    if scenario == "meme_caption":
        from companion.meme_input import validate_admission

        validate_admission(store, settings, request_id,
                           row["usage_job_id"], row["logical_request_id"])
    elif scenario in {"content_vision", *VIDEO_SCENARIOS}:
        from companion.bili_video import validate_model_input

        validate_model_input(store, settings, clock, request_id, scenario,
                             row["usage_job_id"], row["logical_request_id"])


def media_identity_changed(scenario, current, saved):
    return scenario in {"content_vision", *VIDEO_SCENARIOS, "meme_caption"} and (
        current["scenario"] != scenario or current["usage_job_id"] != saved["usage_job_id"]
        or current["logical_request_id"] != saved["logical_request_id"]
    )


def check_adapter(store, settings, wire, model, scenario):
    from companion import live_text
    from companion.model import ModelClient, model_route

    if (owner_authority.required(store, settings) or live_text.active(store, settings)
        or scenario in {"content_vision", *VIDEO_SCENARIOS, "meme_caption", "gateway"}):
        if not isinstance(model, ModelClient):
            raise CoreError("LIVE_TEXT_MODEL_ADAPTER_REQUIRED", 503)
        if live_text.active(store, settings):
            _check_synthetic_transport(settings, model, httpx.URL(model_route(wire, scenario)[2]))
    if isinstance(model, ModelClient) and model.settings is not settings:
        raise CoreError("MODEL_ADMISSION_CONFIGURATION_MISMATCH", 503)


def _check_synthetic_transport(settings, model, url):
    if (settings.live_text_window.execution == "local_synthetic"
        and not isinstance(model.client._transport_for_url(url), httpx.MockTransport)):
        raise CoreError("LIVE_TEXT_SYNTHETIC_MODEL_TRANSPORT_REQUIRED", 503)


def transport_fence(store, settings, clock, request_id, scenario, model, url,
                    admission, current_job, quota_state):
    from companion import cumulative, live_text, paid_budget

    if (scenario != "gateway" and live_text.active(store, settings)
        and not owner_authority.required(store, settings)):
        _check_synthetic_transport(settings, model, url)
        live_text.dispatch_fence(
            settings, clock, current_job["lease_until"], admission["lease_until"],
            quota_state=quota_state, request_id=request_id,
        )
        return
    paid = paid_budget.contract(settings, clock())
    lease_deadline = datetime.fromisoformat(admission["lease_until"])
    if scenario == "gateway" or owner_authority.required(store, settings):
        # The budget sync may race a STOP. Check the current grant before sampling time.
        current_job = check(store, settings, clock, request_id, scenario)
    dispatch_now = datetime.fromisoformat(clock())
    if not paid_budget.active(paid, dispatch_now):
        raise CoreError("PAID_APPROVAL_EXPIRED", 503)
    if not paid_budget.rates_valid(paid, dispatch_now):
        raise CoreError("PAID_RATES_EXPIRED", 503)
    if dispatch_now >= lease_deadline:
        raise CoreError("MODEL_ADMISSION_LEASE_EXPIRED", 503)
    if current_job and dispatch_now >= datetime.fromisoformat(current_job["lease_until"]):
        raise CoreError("EVALUATION_JOB_FENCED" if scenario == "evaluation" else "JOB_LEASE_LOST",
                        409)
    cumulative.final_fence(settings, quota_state, dispatch_now, request_id)
