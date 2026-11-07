import asyncio
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from companion.config import Settings
from companion.contracts import InboundEvent
from companion.conversation_policy import ConversationPolicyConfig
from companion.core import Core
from companion.input_debounce import InputDebounceConfig
from companion.jargon_config import JargonConfig
from companion.model import ModelResponse
from companion.rem_protocol import RemConfig
from companion.store import migrate
from companion.turn_taking import TurnTakingConfig


class Clock:
    def __init__(self):
        self.now = datetime.fromisoformat("2026-10-19T04:00:00+00:00")

    def __call__(self):
        return self.now.isoformat(timespec="microseconds")

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeModel:
    def __init__(self):
        self.calls = []
        self.error = None
        self.callback = None
        self.extraction = '{"memories":[]}'
        self.review = None
        self.recall = None
        self.activity = "这段内容挺有意思，想和你聊聊。"

    async def complete(self, messages, scenario):
        self.calls.append((messages, scenario))
        if self.callback:
            self.callback()
        if self.error:
            raise self.error
        if scenario == "memory_extract":
            text = self.extraction
        elif scenario == "memory_review":
            payload = json.loads(messages[-1]["content"])
            if payload.get("operation") == "recall":
                text = (
                    self.recall(payload)
                    if self.recall
                    else json.dumps({"relevant": [row["ref"] for row in payload["memories"]]})
                )
            else:
                text = (
                    self.review(payload)
                    if self.review
                    else json.dumps(
                        {
                            "relations": [
                                {
                                    "supported": True,
                                    "update_authorized": bool(row.get("update")),
                                    "claims_replacement": False,
                                    "same": None,
                                    "conflicts": [],
                                    "replaces": [],
                                }
                                for row in payload["candidates"]
                            ]
                        }
                    )
                )
        elif scenario == "activity":
            text = self.activity
        else:
            text = "回复：" + messages[-1]["content"]
        return ModelResponse(text, 100, 20, 40)


def run(awaitable):
    return asyncio.run(awaitable)


def event(core, number=1, text="今天想聊聊茶", origin=None, at=None):
    cfg = core.settings
    origin = origin or f"qq:FriendMessage:{cfg.owner_qq_id}"
    return InboundEvent.model_validate(
        {
            "schema_version": 1,
            "event_id": f"evt_test_{number}",
            "source": "qq.onebot",
            "source_event_id": f"{cfg.bot_qq_id}:{origin}:{number}",
            "event_type": "message.received",
            "occurred_at": at or core.clock(),
            "conversation": {"platform_session_key": origin, "scope": "private"},
            "actor": {"platform_user_id": cfg.owner_qq_id, "role": "owner"},
            "payload": {"text": text, "media": []},
            "trace_id": f"trace_test_{number}",
        }
    )


def ingest(core, number=1, text="今天想聊聊茶", **kwargs):
    return core.ingest(event(core, number, text, **kwargs), f"key-{number}")


@pytest.fixture(scope="session")
def migrated_database():
    """One native template per pytest worker; all connections close before cloning."""
    from tests.postgres_fixture import isolated_database

    with isolated_database() as info:
        migrate(info)
        yield info


@pytest.fixture(scope="session", autouse=True)
def worker_scratch(tmp_path_factory):
    """Children inherit their worker's scratch; the caller owns the enclosing round."""
    path = tmp_path_factory.getbasetemp() / "process-tmp"
    path.mkdir(exist_ok=True)
    previous = {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")}
    os.environ.update(dict.fromkeys(previous, str(path)))
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def core_factory(tmp_path_factory, migrated_database):
    """Each call owns fresh native state; a property example never reuses another's DB."""

    @contextmanager
    def create(directory=None):
        directory = directory if directory is not None else tmp_path_factory.mktemp("scenario")
        from tests.postgres_fixture import isolated_database

        with isolated_database(template=migrated_database) as info:
            cfg = Settings(
                owner_qq_id="10001",
                bot_qq_id="10002",
                bridge_token="test-token-" * 4,
                database=info,
                state_directory=str(directory / "state"),
                erasure_ledger=str(directory / "privacy.jsonl"),
                embedding_cache_dir=os.environ.get(
                    "COMPANION_TEST_EMBEDDING_CACHE", "data/embedding-cache"),
                input_rate=300000,
                cached_input_rate=6000,
                output_rate=1200000,
                proactive_enabled=False,
                # Scenario tests opt into the humanlike production defaults explicitly;
                # see tests/test_humanlike_defaults.py for the defaults themselves.
                memory_enabled=False,
                segmented_reply=None,
                chat_thinking="disabled",
                rem=RemConfig(enabled=False),
                jargon=JargonConfig(enabled=False),
                input_debounce=InputDebounceConfig(enabled=False),
                turn_taking=TurnTakingConfig(silence_enabled=False, follow_up_enabled=False),
                conversation_policy=ConversationPolicyConfig(
                    mode="OFF", strategy="participation_v1"
                ),
                # Exact-message tests predate time labels; tests/test_time_context.py covers them.
                chat_time_labels=False,
                # Exact-wire/legacy scenarios opt into runtime switching explicitly.
                operator_hot_reload=False,
            )
            with Core(cfg, FakeModel(), Clock()) as instance:
                yield instance

    return create


@pytest.fixture
def core(tmp_path, core_factory):
    # Local files stay in tmp_path; authoritative state belongs to the native test database.
    with core_factory(tmp_path) as instance:
        yield instance
