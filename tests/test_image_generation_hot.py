"""Console changes to the image provider apply at the next chat; an in-flight job keeps its own."""

import pytest

from companion import image_generation, operator_config, runtime_config
from companion.contracts import CoreError
from companion.store import encode
from tests.test_runtime_configuration import hot as hot
from tests.test_runtime_configuration import save, task

FIRST, SECOND = "https://images.example.test/v1/images/generations", (
    "https://other-images.example.test/v1/images/generations")
KEY_A, KEY_B = "synthetic-image-key-aaaaaaaa", "synthetic-image-key-bbbbbbbb"


def provider(url, key=None, **endpoint):
    value = {"enabled": True, "daily_micro_usd": 200000,
             "endpoint": {"url": url, "model": "gpt-image-1", "max_request_micro_usd": 40000,
                          **({"key": key} if key is not None else {}), **endpoint}}
    return {"image_generation": value}


def finish(core, job):
    # Only one chat per conversation runs at a time; the binding outlives the job.
    core.store.execute("UPDATE jobs SET state='SUCCEEDED',lease_token=NULL WHERE job_id=?",
                       (job["job_id"],))


def pinned(core, job):
    return image_generation.options(core.store, core.settings, job["job_id"])


def test_next_chat_uses_saved_provider_and_inflight_job_keeps_its_own(hot):
    save(hot, **provider(FIRST, KEY_A))
    first = task(hot, 1)
    assert (pinned(hot, first).endpoint.url, pinned(hot, first).endpoint.key.get_secret_value()
            ) == (FIRST, KEY_A)
    saved = operator_config.latest(hot.store)
    candidate = operator_config._candidate(hot.settings, operator_config._decode(saved,
                                                                                  hot.settings))
    assert runtime_config.classify(hot.settings, candidate)["restart_fields"] == []

    save(hot, **provider(SECOND, KEY_B, model="dall-e-3"))
    finish(hot, first)
    second = task(hot, 2)
    assert pinned(hot, second).endpoint.url == SECOND
    assert pinned(hot, second).endpoint.key.get_secret_value() == KEY_B
    assert pinned(hot, second).endpoint.model == "dall-e-3"
    assert pinned(hot, first).endpoint.url == FIRST  # The running job is not switched.

    stored = encode(hot.store.rows("SELECT * FROM operator_configuration")) + encode(
        hot.store.rows("SELECT * FROM operator_runtime_snapshots"))
    assert KEY_A not in stored and KEY_B not in stored

    # Same destination: a write-only null keeps the current key.
    save(hot, image_generation={"cooldown_seconds": 60})
    finish(hot, second)
    assert pinned(hot, task(hot, 3)).endpoint.key.get_secret_value() == KEY_B


def test_new_destination_requires_an_explicit_key_and_https(hot):
    save(hot, **provider(FIRST, KEY_A))
    with pytest.raises(CoreError) as missing:
        save(hot, **provider(SECOND))
    assert missing.value.code == "CONFIGURATION_ENDPOINT_KEY_REQUIRED"
    with pytest.raises(CoreError) as reused:
        save(hot, **provider(SECOND, KEY_A))
    assert reused.value.code == "CONFIGURATION_ENDPOINT_KEY_REQUIRED"
    with pytest.raises(CoreError) as plain:
        save(hot, **provider("http://images.example.test/v1", "synthetic-image-key-cccccccc"))
    assert plain.value.code in {"CONFIGURATION_ENDPOINT_HTTPS_REQUIRED",
                                "INVALID_OPERATOR_CONFIGURATION"}


def test_console_view_marks_the_key_write_only_and_task_boundary(hot):
    save(hot, **provider(FIRST, KEY_A))
    view = operator_config.view(hot.store, hot.settings)
    field = next(item for item in view["fields"] if item["key"] == "image_generation")
    assert field["effect"] == "task_boundary" and field["group"] == "expression"
    assert field["value"]["endpoint"]["key"] is None
    assert field["value"]["endpoint"]["url"] == FIRST
    assert KEY_A not in encode(view)


@pytest.mark.parametrize("version", [2, 3])
def test_previous_snapshot_keeps_original_timing_and_signature_after_restart(hot, version):
    # Construct the pre-timing signed record, independently of the current snapshot writer.
    cfg = hot.settings
    raw = operator_config._raw(cfg)
    raw.pop("video_models")  # This field was absent from the original signed settings.
    fields = ("chat_time_labels", "conversation_policy", "style_live",
              "conversation_guidance", "image_generation")
    deployment = {key: raw.pop(key) for key in fields}
    raw.pop("operator_hot_reload")
    raw["deployment_expression"] = deployment
    base = runtime_config._digest(cfg, raw)
    expression = {"conversation_policy": deployment["conversation_policy"],
                  "conversation_guidance": None, "style_live_enabled": False}
    original = {"version": 0, "format_version": version, "base_sha256": base,
                "chat_time_labels": cfg.chat_time_labels, "expression": expression}
    stored = {"format_version": version, "conversation_policy": deployment["conversation_policy"],
              "style_live_enabled": False, "encrypted_guidance": None}
    if version == 3:
        image = operator_config._candidate(cfg, provider(FIRST, KEY_A)).image_generation
        original["image_generation"] = operator_config._raw(image)
        stored["encrypted_image_generation"] = operator_config._cipher(cfg).encrypt(
            encode(original["image_generation"]).encode()).decode()
    key = runtime_config._digest(cfg, original)
    row = {"snapshot_id": key, "version": 0, "base_sha256": base,
           "chat_time_labels": int(cfg.chat_time_labels), "expression_json": encode(stored)}

    save(hot, turn_taking={"silence_enabled": True}, input_debounce={"quiet_seconds": 6})
    restarted = operator_config.load(cfg)
    pinned = runtime_config.validate_record(restarted, row)
    assert runtime_config.identity(restarted, pinned) == key
    assert not pinned.silence_enabled and pinned.debounce.quiet_seconds == 2
    if version == 3:
        assert pinned.image_generation.endpoint.key.get_secret_value() == KEY_A
    restarted.turn_taking.follow_up_daily_checks += 1
    with pytest.raises(CoreError, match="CONFIGURATION_STARTUP_IDENTITY_CHANGED"):
        runtime_config.validate_record(restarted, row)


def test_snapshot_with_provider_survives_restart_validation(hot):
    save(hot, **provider(FIRST, KEY_A))
    job = task(hot, 1)
    reloaded = operator_config.load(hot.settings)
    snapshot = runtime_config.get(hot.store, reloaded, "job:" + job["job_id"])
    assert snapshot.format_version == 4
    assert snapshot.image_generation.endpoint.key.get_secret_value() == KEY_A
