"""Dedicated bounded caption samples using the existing model and paid ledger."""

from companion.contracts import CoreError
from companion.image_input import text_and_estimate as image_text_and_estimate


def _policy(policy):
    if policy is None:
        raise CoreError("PAID_MEME_APPROVAL_REQUIRED", 503)
    if policy.max_images_per_request == "unlimited" or policy.max_images_per_request > 5:
        raise CoreError("MODEL_MEME_SAMPLE_LIMIT", 503)
    return policy


def text_and_estimate(policy, messages):
    """Validate up to five static PNG samples with the approved per-image estimate."""
    text, estimate = image_text_and_estimate(_policy(policy), messages)
    if any(not part["image_url"]["url"].startswith("data:image/png;base64,")
           for part in messages[0]["content"][1:]):
        raise CoreError("MODEL_MEME_PNG_REQUIRED", 503)
    return text, estimate


def contract(settings, job_id, logical_request_id):
    approval = settings.paid_model
    if (
        settings.mode != "live" or approval is None or not approval.live_approved
        or settings.owner_runtime.mode != "ENABLED" or not settings.meme_semantics_enabled
    ):
        raise CoreError("MODEL_MEME_SCOPE_REQUIRED", 503)
    _policy(approval.meme_input)
    if settings.meme_caption_daily_micro_usd <= 0:
        raise CoreError("SCENARIO_BUDGET_DISABLED", 429)
    if not job_id or logical_request_id != job_id:
        raise CoreError("MODEL_MEME_JOB_REQUIRED", 503)


def check_attempt(store, job_id, request_id=None):
    """A caption job owns one provider attempt, including any uncertain attempt."""
    from companion.budget import not_dispatched

    if not store.one("SELECT 1 FROM jobs WHERE job_id = %s AND kind = 'meme_caption'", (job_id,)):
        raise CoreError("MODEL_MEME_JOB_REQUIRED", 503)
    if any(not not_dispatched(store, row["request_id"]) for row in store.rows(
        'SELECT request_id FROM model_usage WHERE job_id = %s AND request_id <> %s',
        (job_id, request_id or ""),
    )):
        raise CoreError("MODEL_MEME_ALREADY_ATTEMPTED", 409)


def validate_admission(store, settings, request_id, job_id, logical_id):
    contract(settings, job_id, logical_id)
    check_attempt(store, job_id, request_id)
