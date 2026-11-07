import json

import pytest

from companion import owner_authority, voice_authority
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.store import after
from companion.voice_flow import VoiceFlow
from tests.voice_owner_fixtures import native_voice


def test_native_voice_input_uses_original_reference_without_window(core, tmp_path):
    capture, _, job = native_voice(core, tmp_path)
    assert core.settings.live_text_window is None
    flow = VoiceFlow(core.store, core.settings, core.clock)
    assert flow.input_status(capture.event_id, job).state == "WAITING"
    saved = json.loads(core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                                    ("voice_input:" + capture.event_id,))["value"])
    assert saved["authority"] == capture.authority and "window_sha256" not in saved
    assert saved["wait_until"] == after(core.clock(), core.settings.voice.asr.wait_seconds)


def test_native_voice_wait_cannot_outlive_original_expiry(core, tmp_path):
    capture, _, job = native_voice(core, tmp_path, expires_seconds=7)
    status = VoiceFlow(core.store, core.settings, core.clock).input_status(capture.event_id, job)
    assert status.wait_until.isoformat(timespec="microseconds") == after(core.clock(), 7)
    core.clock.advance(7)
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_EXPIRED"):
        voice_authority.source(core.store, core.settings, core.clock, capture.event_id)


def test_native_voice_stop_and_original_revision_are_not_rebound(core, tmp_path):
    capture, _, _ = native_voice(core, tmp_path)
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="native-voice-stop", authority=capture.authority))
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_DISABLED"):
        voice_authority.current(core.store, core.settings, core.clock, authority=capture.authority)


def test_chat_only_authority_keeps_optional_asr_disabled(core, tmp_path):
    capture, _, job = native_voice(core, tmp_path, voice=False)
    assert VoiceFlow(core.store, core.settings, core.clock).input_status(
        capture.event_id, job).state == "NONE"
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
        voice_authority.source(core.store, core.settings, core.clock, capture.event_id)
