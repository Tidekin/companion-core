"""Owner-attested imports preserve source bytes without replaying historical work."""

import asyncio
from io import BytesIO

import pytest
from cryptography.fernet import Fernet
from PIL import Image
from pydantic import ValidationError

from companion import history_import
from companion.backup import backup, restore
from companion.contracts import Candidate, CoreError
from companion.core import Core
from companion.privacy import epoch
from companion.store import encode
from tests.conftest import FakeModel, ingest, run
from tests.support.owner_memory import environment


def package():
    parts = ["【角色设定】虚构的鲸鱼女孩。\r\n",
             "分析：先理解问题，再用一点玩笑和温柔回应。\r\n",
             "主人：搬家后的暗号用米饭。\r\n",
             "（尾巴尖轻轻晃了晃）好啦，记住要发🍚哦。\r\n"]
    segments, cursor = [], 0
    for text, role, kind in zip(parts, ["user", "assistant", "user", "assistant"],
                                ["prompt", "analysis", "dialogue", "dialogue"], strict=True):
        segments.append({"role": role, "kind": kind, "start": cursor, "end": cursor + len(text)})
        cursor += len(text)
    raw = "".join(parts).encode()
    request = history_import.HistoryRequest(source_label="合成的网页手动复制记录",
        source_sha256=history_import.digest(raw), segments=segments, memories=[{
            "subject": "relationship", "kind": "episode", "fact_key": "move_rice_greeting",
            "content": "之前商量搬进QQ时，主人约定把米饭作为搬家后的暗号。",
            "segment": 2, "quote": "搬家后的暗号用米饭"}], character={
            "name": "汐米", "aliases": ["鱼鱼"], "fictional_identity": "虚构的鲸鱼女孩",
            "traits": ["聪明又慵懒", "傲娇但温柔"],
            "speech_style": ["保留括号动作、尾巴动作和神态，频率随语境变化"],
            "response_habits": ["先理解对方的意图，再决定玩笑还是认真回应"],
            "examples": [{"user": "🍚", "assistant": parts[-1].strip()}]})
    return request, raw, history_import.digest(encode(request.model_dump()).encode())


def imported(core):
    core.settings.memory_enabled = True
    request, raw, sha = package()
    return history_import.apply(core, request, raw, sha)


def test_import_is_atomic_idempotent_and_not_new_qq_ingress(core):
    request, raw, sha = package()
    core.settings.memory_enabled = True
    before = history_import.preview(request, raw)
    assert before["segments"]["analysis"] == 1
    assert core.store.rows("SELECT * FROM events") == []
    result = history_import.apply(core, request, raw, sha)
    version = epoch(core.store)
    assert history_import.apply(core, request, raw, sha) == {**result, "created": False}
    assert epoch(core.store) == version
    assert core.store.rows("SELECT * FROM jobs") == []
    assert core.store.rows("SELECT * FROM outbox") == []
    assert not core.store.one("SELECT 1 FROM events WHERE source='qq.onebot'")
    assert history_import.export(core, result["archive_event_id"]) == raw
    assert core.store.one("SELECT status,origin FROM memories") == {
        "status": "CONFIRMED", "origin": "user_confirmed"}
    assert core.store.rows("SELECT * FROM traits") == []
    with pytest.raises(CoreError, match="IMPORT_CONFLICT"):
        history_import.apply(core, request, raw, "f" * 64)
    assert epoch(core.store) == version


@pytest.mark.parametrize("kind", ["analysis", "prompt", "unknown", "notice"])
def test_non_dialogue_sources_cannot_become_facts(core, kind):
    request, raw, sha = package()
    request.segments[2].kind = kind
    with pytest.raises(CoreError, match="IMPORT_EVIDENCE_INVALID"):
        history_import.apply(core, request, raw, sha)
    assert core.store.rows("SELECT * FROM events") == []


def test_wrong_hash_gaps_speaker_and_unknown_times_are_not_fabricated(core):
    request, raw, sha = package()
    with pytest.raises(CoreError, match="IMPORT_SOURCE_CHANGED"):
        history_import.apply(core, request, raw + b"changed", sha)
    request.segments[1].start += 1
    with pytest.raises(CoreError, match="IMPORT_SEGMENT_INVALID"):
        history_import.preview(request, raw)
    with pytest.raises(ValidationError):
        history_import.ImportedFact(subject="self", kind="preference", fact_key="rice",
            content="我喜欢米饭", segment=1, quote="米饭")
    request, raw, sha = package()
    request.memories[0].subject = "user"
    request.segments[2].role = "assistant"
    with pytest.raises(CoreError, match="IMPORT_EVIDENCE_INVALID"):
        history_import.preview(request, raw)


def test_initial_role_and_actions_reach_context_even_with_memory_disabled(core):
    profile = package()[0].character
    history_import.set_character(core, profile)
    ingest(core, 1, "鱼鱼，🍚")
    job = core.store.one("SELECT j.*,e.* FROM jobs j JOIN events e USING(event_id)")
    snapshots = []
    messages = core.context(job, snapshots=snapshots)
    prompt = "\n".join(message["content"] for message in messages)
    assert "汐米" in prompt and "尾巴尖轻轻晃了晃" in prompt
    assert "括号描写动作神态" in prompt and "没有预设角色开始" in prompt
    assert "分析：先理解问题" not in prompt
    assert all(set(message) == {"role", "content"} for message in messages)
    assert [item for item in snapshots if item["kind"] == "character"]
    history_import.clear_character(core)
    with pytest.raises(CoreError, match="REPLY_CANCELLED|PRIVACY_CHANGED"):
        core._check_context_snapshots(job, snapshots)
    rebuilt = core.context(job)
    assert all("主人指定的初始角色（已" not in message["content"] for message in rebuilt)


def test_character_replacement_survives_restart_and_invalidates_old_context(core):
    imported(core)
    ingest(core, 1, "你好")
    job = core.store.one("SELECT j.*,e.* FROM jobs j JOIN events e USING(event_id)")
    snapshots = []
    core.context(job, memories=[], snapshots=snapshots)
    history_import.set_character(core, history_import.InitialCharacter(name="新角色"))
    with pytest.raises(CoreError, match="REPLY_CANCELLED|PRIVACY_CHANGED"):
        core._check_context_snapshots(job, snapshots)
    core.settings.memory_enabled = False
    with Core(core.settings, FakeModel(), core.clock) as reopened:
        assert "新角色" in encode(reopened.context(job))


def test_forget_one_imported_fact_erases_whole_raw_source_and_backup_replay(
    empty_database, core, tmp_path,
):
    result = imported(core)
    ingest(core, 1, "我喜欢茉莉绿茶")
    with core.store.transaction(control_write=True):
        independent = core.memory.add(Candidate(subject="user", kind="preference", fact_key="tea",
            content="主人喜欢茉莉绿茶", evidence_event_id="evt_test_1",
            evidence_quote="喜欢茉莉绿茶"),
            core.clock(), confirmed=True)
    key = Fernet.generate_key()
    archive, restored = tmp_path / "before.enc", tmp_path / "restored-state"
    backup(core.settings.database, archive, key)
    with core.store.transaction(control_write=True):
        core.ledger.append(core.store, result["memory_ids"][0], core.clock())
    assert core.store.one("SELECT value FROM runtime_meta WHERE key=%s",
                           (history_import.ACTIVE,)) is None
    assert core.store.one("SELECT redacted FROM events WHERE event_id=%s",
                           (result["archive_event_id"],))["redacted"] == 1
    assert core.store.one("SELECT content FROM memories WHERE memory_id=%s",
                           (independent,))["content"] == "主人喜欢茉莉绿茶"
    with pytest.raises(CoreError, match="IMPORT_ARCHIVE_UNAVAILABLE"):
        history_import.export(core, result["archive_event_id"])
    with pytest.raises(CoreError, match="IMPORT_PREVIOUSLY_FORGOTTEN"):
        imported(core)
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored)
    with Core(core.settings.model_copy(update={
        "database": empty_database, "state_directory": str(restored),
    }), FakeModel(), core.clock) as replayed:
        store = replayed.store
        assert not store.one("SELECT 1 FROM events WHERE source IN (%s,%s) AND redacted=0",
                              (history_import.SOURCE, history_import.CHARACTER_SOURCE))
        assert store.one("SELECT content FROM memories WHERE memory_id=%s",
                          (independent,))["content"] == "主人喜欢茉莉绿茶"


def test_native_retention_expires_import_and_role_together(core):
    result = imported(core)
    core.clock.advance((core.settings.raw_event_retention_days + 1) * 86400)
    assert core.expire_raw_events() == 3
    assert core.store.one("SELECT value FROM runtime_meta WHERE key=%s",
                           (history_import.ACTIVE,)) is None
    assert core.store.one("SELECT redacted FROM events WHERE event_id=%s",
                           (result["archive_event_id"],))["redacted"] == 1


def test_current_signed_scopes_gate_import_role_and_historical_reads(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path, additional_purposes=("expression",)):
            result = imported(core)
            assert history_import.export(core, result["archive_event_id"]) == package()[1]
            from companion import owner_authority
            from tests.test_owner_authority import activate

            view = owner_authority.current(core.store, core.settings, core.clock)
            policy = view.policy.model_copy(update={
                "purposes": ["chat", "memory"], "history": view.policy.history.model_copy(
                    update={"scope": "none", "not_before": None, "max_turns": 0,
                            "max_utf8_bytes": 0})})
            activate(core, policy, revision=2, operation="remove-history-and-expression")
            assert history_import.character_context(core, {}) is None
            with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
                history_import.export(core, result["archive_event_id"])
            assert core.memory_context.pin(core.clock(), event_ids=[])
            with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
                history_import.set_character(core, package()[0].character)

    asyncio.run(scenario())


def test_import_rolls_back_when_role_is_not_authorized(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path):
            with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
                imported(core)
            assert not core.store.one("SELECT 1 FROM events WHERE source=%s",
                                       (history_import.SOURCE,))
            assert core.store.rows("SELECT * FROM memories") == []

    run(scenario())


def test_original_images_are_hashed_linked_exportable_and_forgotten(core, tmp_path):
    request, raw, sha = package()
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "navy").save(buffer, format="PNG")
    picture = buffer.getvalue()
    image_sha = history_import.digest(picture)
    request.images = [history_import.ImportedImage(sha256=image_sha, mime="image/png",
        description="合成的蓝色图片", source_segments=[2])]
    media = {image_sha: picture}
    core.settings.memory_enabled = True
    with pytest.raises(CoreError, match="IMPORT_IMAGES_CHANGED"):
        history_import.apply(core, request, raw, sha)
    with pytest.raises(CoreError, match="IMPORT_IMAGES_REQUIRED"):
        history_import.load_images(request)
    path = tmp_path / request.images[0].filename
    path.write_bytes(picture)
    assert history_import.load_images(request, tmp_path) == media
    assert history_import.preview(request, raw, media)["images"] == 1
    with pytest.raises(CoreError, match="IMPORT_IMAGES_CHANGED"):
        history_import.apply(core, request, raw, sha, {image_sha: picture + b"changed"})
    result = history_import.apply(core, request, raw, sha, media)
    assert history_import.export(core, result["archive_event_id"], image_sha) == picture
    evidence = core.store.one("SELECT event_id FROM memory_evidence")
    origins = history_import.provenance(core.store, [evidence["event_id"]])
    assert origins[0]["image_sha256s"] == [image_sha]
    history_import.forget(core, result["archive_event_id"])
    with pytest.raises(CoreError, match="IMPORT_ARCHIVE_UNAVAILABLE"):
        history_import.export(core, result["archive_event_id"], image_sha)
