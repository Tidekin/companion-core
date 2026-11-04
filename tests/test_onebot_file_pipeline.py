"""Real Bridge capture/spool/journal, loopback WS and mocked Core/native file I/O."""

import asyncio
import base64
import hashlib
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from websockets.asyncio.server import serve

from companion_onebot_bridge import continuous, pending_media
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.files import original_files
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.rich_input import input_digest
from companion_onebot_bridge.spool import Spool, encode
from companion_onebot_bridge.window import (
    TestWindow,
    WindowPlan,
    check_protocol_request,
    digest,
)
from tests.conftest import run
from tests.support.onebot import provider
from tests.support.onebot_events import config, raw_event
from tests.test_onebot_capture import capture_approval
from tests.test_onebot_files import BODY, segment
from tests.test_onebot_images import image_segment
from tests.test_onebot_window_handoff import CoreContract, plan_for
from tests.test_owner_recall_bridge import recall_frame


def file_frame(count=1, *, images=False):
    return raw_event(1, message=[
        {"type": "text", "data": {"text": "请读这些资料"}},
        *([image_segment()] if images else []),
        *[segment(file=f"资料{slot}.txt", name=f"资料{slot}.txt",
                  file_id=f"opaque-file-{slot}", id=f"opaque-file-{slot}")
          for slot in range(count)],
    ])


def file_plan(tmp_path, port=3001, *, images=False, file_scope="owner_file_v1"):
    cfg, plan, now = plan_for(tmp_path)
    cfg = replace(cfg, onebot_ws_url=f"ws://127.0.0.1:{port}")
    value = deepcopy(plan.value)
    value["live_text"].update(
        automatic_reply=True, companion_scope="owner_private_v1",
        companion_approval_sha256="a" * 64, history_scope="owner_private",
        history_approval_sha256="b" * 64, max_history_turns=15, max_history_utf8_bytes=16000,
        memory_scope="owner_private", memory_approval_sha256="c" * 64,
        rich_input_scope="references_v1", file_scope=file_scope,
    )
    if images:
        value["live_text"].update(image_scope="owner_image_v1", image_approval_sha256="d" * 64)
    inventory = {key: value[key] for key in (
        "identity_sha256", "protocol_epoch", "config_sha256", "config_version", "image_ids")}
    inventory["config_sha256"] = digest(cfg.snapshot())
    value.update(config_sha256=inventory["config_sha256"],
                 runtime_attestation_sha256=digest(inventory))
    return cfg, WindowPlan(value, cfg, inventory), now, inventory


class FileContract(CoreContract):
    """Independent synthetic Core receipts, not document parser/model acceptance."""

    def __init__(self, plan, now):
        super().__init__(plan)
        self.now = now
        self.events, self.states, self.content = {}, {}, {}
        self.approvals, self.uploads, self.lookups, self.downloads = [], [], [], []
        self.failure = None
        self.claim_change = self.check_change = self.upload_change = None
        self.unsettled_calls = 0
        self.accepted = lambda: None

    def original(self, event_id, kind, slot):
        return next(item for item in self.events[event_id]["payload"]["rich"]["segments"]
                    if item["type"] == kind and item["slot"] == slot)

    def state(self, event_id):
        event = self.events[event_id]
        return {"window_sha256": self.current.sha256, "event_id": event_id,
                "input_sha256": input_digest(event["payload"]), "reply_revision": 1,
                "state": self.states[event_id], "unsettled_model": False,
                "unsettled_delivery": False}

    def __call__(self, request):
        path = request.url.path
        if path == "/internal/v1/ops/status":
            value = super().__call__(request).json()
            value["operations"] = {"budget": {"unsettled_calls": self.unsettled_calls}}
            return httpx.Response(200, json=value)
        if path.endswith("/inputs/approve"):
            self.approvals.append(json.loads(request.content))
        if path == "/internal/v1/inbox/events":
            event = json.loads(request.content)
            for event_id in self.events:
                self.states[event_id] = "SUPERSEDED"
            self.events[event["event_id"]] = event
            self.states[event["event_id"]] = "QUEUED"
            for item in event["payload"].get("shared_content", []):
                if item["kind"] in {"image", "file"}:
                    self.content[(event["event_id"], item["kind"], item["slot"])] = "WAITING"
            self.accepted()
        if path.endswith("/inputs/recall"):
            body = json.loads(request.content)
            self.states[body["target_event_id"]] = "RECALLED"
            self.accepted()
            return httpx.Response(200, json={"recall_id": body["recall_id"],
                "target_event_id": body["target_event_id"], "state": "RECALLED",
                "duplicate": False})
        if path.startswith("/internal/v1/live-text/inputs/") and path.endswith("/state"):
            return httpx.Response(200, json=self.state(path.split("/")[-2]))
        if path.startswith("/internal/v2/content/"):
            event_id = path.split("/")[4]
            if request.method == "GET":
                self.calls.append((path, None))
                return httpx.Response(200, json={
                    "items": [{"kind": "qq_" + kind, "slot": slot, "state": state,
                               "error_code": None}
                              for (owner, kind, slot), state in self.content.items()
                              if owner == event_id],
                    "pending": True, "wait_until": (self.now + timedelta(seconds=60)).isoformat(),
                })
            kind = path.split("/")[5]
            if path.endswith("/claim"):
                body = json.loads(request.content)
                self.calls.append((path, body))
                slot = int(body["request_key"].split(":")[1])
                original = self.original(event_id, kind, slot)
                assert body == {"request_key": f"{kind}:{slot}",
                                "source_sha256": original["source_sha256"]}
                assert self.content[(event_id, kind, slot)] == "WAITING"
                self.content[(event_id, kind, slot)] = "FETCHING"
                if self.failure == "claim_lost":
                    raise httpx.ReadTimeout("synthetic lost claim ACK")
                value = {"state": "FETCHING", "lease_token": f"content_lease_{slot:032x}",
                         "lease_until": (self.now + timedelta(seconds=15)).isoformat(),
                         "max_bytes": 4096}
                if self.claim_change:
                    self.claim_change(value)
                return httpx.Response(200, json=value)
            if path.endswith("/check"):
                body = json.loads(request.content)
                slot = int(body["lease_token"].removeprefix("content_lease_"), 16)
                original = self.original(event_id, kind, slot)
                assert body["source_sha256"] == original["source_sha256"]
                assert self.states[event_id] == "QUEUED"
                value = {"approved": True,
                         "lease_until": (self.now + timedelta(seconds=15)).isoformat()}
                if self.check_change:
                    self.check_change(value)
                return httpx.Response(200, json=value)
            slot = int(request.headers["X-Content-Key"].split(":")[1])
            original = self.original(event_id, kind, slot)
            if kind == "image":
                self.content[(event_id, kind, slot)] = "SUCCEEDED"
                return httpx.Response(200, json={"state": "SUCCEEDED"})
            encoded = request.headers["X-Content-Proof"]
            proof = json.loads(base64.urlsafe_b64decode(encoded))
            assert base64.urlsafe_b64encode(encode(proof).encode()).decode() == encoded
            signature = proof.pop("signature")
            assert proof == {
                "plan_sha256": self.current.sha256, "event_id": event_id, "slot": slot,
                "source_sha256": original["source_sha256"], "file_id": original["file_id"],
                "filename": original["filename"], "mime": "text/plain",
                "lease_token": request.headers["X-Content-Lease"],
                "media_sha256": hashlib.sha256(request.content).hexdigest(),
                "bytes": len(request.content),
            }
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(
                self.current.config.capture_public_key)).verify(bytes.fromhex(signature),
                    b"companion-core/onebot-owner-file/v1\x00" + encode(proof).encode())
            assert request.content == BODY
            self.uploads.append(proof)
            self.content[(event_id, kind, slot)] = (
                "UNKNOWN" if self.failure == "upload_unknown" else "QUEUED")
            if self.failure in {"upload_lost", "upload_unknown"}:
                raise httpx.ReadTimeout("synthetic lost upload ACK")
            value = {"item_id": f"content_{slot:032x}", "state": "QUEUED", "duplicate": False}
            if self.upload_change:
                self.upload_change(value)
            return httpx.Response(200, json=value)
        return super().__call__(request)

    def native(self, request):
        if request.url.path != "/get_private_file_url":
            return provider(request, [])
        body = json.loads(request.content)
        self.lookups.append(body)
        assert body["user_id"] == 10001 and body["file_hash"] == "opaque-hash"
        if self.failure == "native_lost":
            raise httpx.ReadTimeout("synthetic native resolution ACK lost")
        return httpx.Response(200, json={"status": "ok", "retcode": 0,
            "data": {"url": "http://download.ftn.qq.com:8080/" + body["file_id"]}})

    def download(self, request):
        self.downloads.append(request.url.path)
        assert request.url.host == "1.1.1.1"
        if self.failure == "download_lost":
            raise httpx.ReadTimeout("synthetic CDN connection interrupted")
        return httpx.Response(200, content=BODY, headers={"Content-Type": "text/plain"})


async def resolve(*_):
    return ["1.1.1.1"]


@asynccontextmanager
async def file_window(tmp_path, *, count=1, images=False, source=None):
    async def receive(ws):
        assert ws.request.headers["Authorization"] == "Bearer " + config().onebot_token
        if source:
            await source(ws)
        else:
            await ws.send(encode(file_frame(count, images=images)))
            await ws.wait_closed()

    async with serve(receive, "127.0.0.1", 0) as server:
        cfg, plan, now, _ = file_plan(tmp_path, server.sockets[0].getsockname()[1], images=images)
        contract = FileContract(plan, now)
        with Spool(cfg) as spool:
            async with contract.client() as client, private_http_client(
                cfg.onebot_http_url, cfg.onebot_token,
                transport=httpx.MockTransport(contract.native),
            ) as protocol:
                window = TestWindow(spool, plan, clock=lambda: now)
                await window.open(client, protocol)
                yield window, client, protocol, contract


def pump(window, client, protocol, contract):
    return pending_media.MediaPump(window, client, protocol=protocol,
        file_transport=httpx.MockTransport(contract.download), file_resolver=resolve,
        image_transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=b"synthetic image", headers={"Content-Type": "image/png"})),
        image_resolver=resolve)


async def capture_and_queue(window, client, protocol):
    await window.capture_once(client, protocol)
    await window.approve_captured(client, capture_approval(window), defer_media=True)
    return window.db.execute("SELECT * FROM automatic_media").fetchone()


def test_explicit_file_scope_preserves_source_order_and_keeps_filename_out_of_owner_text():
    raw = file_frame(2, images=True)
    event = normalize(encode(raw), config(), now=raw["time"],
        rich_input_scope="references_v1", image_scope="owner_image_v1", file_scope="owner_file_v1")
    assert event["event_type"] == "message.received"
    assert event["payload"]["text"] == "请读这些资料"
    assert event["payload"]["shared_content"] == [
        {"kind": "image", "slot": 0}, {"kind": "file", "slot": 0}, {"kind": "file", "slot": 1}]
    projected = [item for item in event["payload"]["rich"]["segments"] if item["type"] == "file"]
    assert projected == [file.projection() for file in original_files(raw["message"])]
    baseline = normalize(encode(raw), config(), now=raw["time"], rich_input_scope="references_v1")
    assert baseline["payload"]["shared_content"] == []
    assert all(item.get("coverage") == "UNSUPPORTED"
               for item in baseline["payload"]["rich"]["segments"])
    assert input_digest(event["payload"]) != input_digest(baseline["payload"])


@pytest.mark.parametrize("changes", [
    {"file_scope": "anything"}, {"file_scope": {}}, {"rich_input_scope": "none"},
    {"capture_public_key": None}, {"companion_scope": "none"},
])
def test_window_plan_rejects_incomplete_or_unrecognized_file_authority(tmp_path, changes):
    cfg, plan, _, inventory = file_plan(tmp_path)
    value = deepcopy(plan.value)
    value["live_text"].update(changes)
    with pytest.raises(ValueError):
        WindowPlan(value, cfg, inventory)


@pytest.mark.parametrize("count", [1, 2])
def test_capture_signed_queue_and_native_upload_preserve_private_file_metadata(tmp_path, count):
    async def scenario():
        async with file_window(tmp_path, count=count) as (window, client, protocol, contract):
            row = await capture_and_queue(window, client, protocol)
            assert row["image_count"] == 0 and row["next_slot"] == 0 and row["state"] == "PENDING"
            value = json.loads(row["manifest_json"])
            assert len(value["files"]) == count and value["images"] == []
            assert "请读这些资料" not in row["manifest_json"]
            assert contract.approvals[0]["input_sha256"] == input_digest(
                next(iter(contract.events.values()))["payload"])
            assert not contract.lookups and not contract.downloads and not contract.uploads
            await pump(window, client, protocol, contract).execute(row)
            finished = window.db.execute("SELECT * FROM automatic_media").fetchone()
            assert finished["state"] == "DONE" and finished["next_slot"] == count
            assert finished["manifest_json"] is None
            assert [item["filename"] for item in contract.uploads] == [
                f"资料{slot}.txt" for slot in range(count)]
            assert len(contract.lookups) == len(contract.downloads) == count
            records, _ = window.read_journal()
            queued = next(record["data"] for record in records if record["kind"] == "MEDIA_QUEUED")
            assert queued["file_count"] == count and queued["image_count"] == 0
            window.verify()

    run(scenario())


def test_file_only_transfer_does_not_wait_for_unsettled_image_model_budget(tmp_path):
    async def scenario():
        async with file_window(tmp_path) as (window, client, protocol, contract):
            row = await capture_and_queue(window, client, protocol)
            contract.unsettled_calls = 1
            before = len([path for path, _ in contract.calls if path.endswith("/ops/status")])
            await asyncio.wait_for(pump(window, client, protocol, contract).execute(row), 3)
            after = len([path for path, _ in contract.calls if path.endswith("/ops/status")])
            assert after == before
            assert len(contract.uploads) == 1

    run(scenario())


def test_one_shot_file_approval_uses_the_same_durable_queue(tmp_path):
    async def scenario():
        async with file_window(tmp_path) as (window, client, protocol, contract):
            await window.capture_once(client, protocol)
            await window.approve_captured(client, capture_approval(window), protocol=protocol,
                file_transport=httpx.MockTransport(contract.download), file_resolver=resolve)
            row = window.db.execute("SELECT * FROM automatic_media").fetchone()
            assert row["state"] == "DONE" and row["next_slot"] == 1
            assert len(contract.lookups) == len(contract.downloads) == len(contract.uploads) == 1
            window.verify()

    run(scenario())


def test_regular_onebot_guard_never_acquires_private_file_action_permission(tmp_path):
    _, plan, now, _ = file_plan(tmp_path)
    with Spool(plan.config) as spool:
        window = TestWindow(spool, plan, clock=lambda: now)
        request = httpx.Request("POST", plan.config.onebot_http_url + "/get_private_file_url",
            json={"user_id": 10001, "file_id": "opaque", "file_hash": "hash"},
            headers={"Authorization": "Bearer " + plan.config.onebot_token})
        with pytest.raises(ValueError, match="LIVE_TEXT_ONEBOT_REQUEST_CHANGED"):
            check_protocol_request(window, request)


@pytest.mark.parametrize("failure,code", [
    ("claim_lost", None), ("native_lost", "FILE_FETCH_TIMEOUT"),
    ("download_lost", "FILE_FETCH_TIMEOUT"), ("upload_unknown", None),
])
def test_restart_file_read_resumes_but_uncertain_claim_and_upload_do_not(tmp_path, failure, code):
    async def scenario():
        async with file_window(tmp_path) as (window, client, protocol, contract):
            row = await capture_and_queue(window, client, protocol)
            contract.failure = failure
            with pytest.raises(ValueError if code else httpx.ReadTimeout, match=code):
                await pump(window, client, protocol, contract).execute(row)
            before = len(contract.lookups), len(contract.downloads), len(contract.uploads)
            row = window.db.execute("SELECT * FROM automatic_media").fetchone()
            claims = sum(path.endswith("/claim") for path, _ in contract.calls)
            contract.failure = None
            resumed = failure in {"native_lost", "download_lost"}
            if resumed:
                await pump(window, client, protocol, contract).execute(row)
                assert len(contract.lookups) == before[0] + 1
                assert len(contract.downloads) == before[1] + 1
                assert len(contract.uploads) == 1
                assert sum(path.endswith("/claim") for path, _ in contract.calls) == claims
            else:
                with pytest.raises(ValueError, match="AUTOMATIC_MEDIA_RECONCILIATION_REQUIRED"):
                    await pump(window, client, protocol, contract).execute(row)
                assert (len(contract.lookups), len(contract.downloads),
                        len(contract.uploads)) == before
            await window.stop_live(client)
            stopped = window.db.execute("SELECT * FROM automatic_media").fetchone()
            assert stopped["state"] == ("DONE" if resumed else "CANCELLED")
            assert stopped["manifest_json"] is None
            window.verify(active=False)

    run(scenario())


@pytest.mark.parametrize("accepted_state", ["QUEUED", "RUNNING", "SUCCEEDED"])
def test_lost_file_upload_ack_uses_core_state_without_repeating_native_io(tmp_path, accepted_state):
    async def scenario():
        async with file_window(tmp_path) as (window, client, protocol, contract):
            row = await capture_and_queue(window, client, protocol)
            contract.failure = "upload_lost"
            with pytest.raises(httpx.ReadTimeout):
                await pump(window, client, protocol, contract).execute(row)
            contract.content[(row["event_id"], "file", 0)] = accepted_state
            row = window.db.execute("SELECT * FROM automatic_media").fetchone()
            assert row["state"] == "UPLOADING"
            contract.failure = None
            await pump(window, client, protocol, contract).execute(row)
            assert len(contract.lookups) == len(contract.downloads) == len(contract.uploads) == 1
            assert window.db.execute("SELECT state FROM automatic_media").fetchone()[0] == "DONE"
            window.verify()

    run(scenario())


def test_mixed_batch_resume_preserves_completed_image_and_only_uploads_remaining_file(
    tmp_path, monkeypatch,
):
    async def scenario():
        async with file_window(tmp_path, images=True) as (window, client, protocol, contract):
            row = await capture_and_queue(window, client, protocol)
            assert row["image_count"] == 1
            transition = pending_media.transition

            def crash(window, event_id, state, position, receipt=None):
                transition(window, event_id, state, position, receipt)
                if state == "PENDING" and position == 1:
                    raise RuntimeError("synthetic process exit after image receipt")

            monkeypatch.setattr(pending_media, "transition", crash)
            with pytest.raises(RuntimeError, match="after image receipt"):
                await pump(window, client, protocol, contract).execute(row)
            monkeypatch.setattr(pending_media, "transition", transition)
            row = window.db.execute("SELECT * FROM automatic_media").fetchone()
            assert row["state"] == "PENDING" and row["next_slot"] == 1
            assert contract.content[(row["event_id"], "image", 0)] == "SUCCEEDED"
            before = list(contract.calls)
            contract.unsettled_calls = 1
            await asyncio.wait_for(pump(window, client, protocol, contract).execute(row), 3)
            added = contract.calls[len(before):]
            assert not any("/image" in path or path.endswith("/ops/status") for path, _ in added)
            assert len(contract.uploads) == 1
            finished = window.db.execute("SELECT * FROM automatic_media").fetchone()
            assert (finished["image_count"], finished["next_slot"], finished["state"]) == (
                1, 2, "DONE")
            window.verify()

    run(scenario())


@pytest.mark.parametrize("control", ["new_input", "recall"])
def test_blocked_file_download_allows_new_owner_input_and_recall(tmp_path, monkeypatch, control):
    started, release, pause = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def source(ws):
        await ws.send(encode(file_frame()))
        await started.wait()
        await ws.send(recall_frame() if control == "recall" else encode(
            raw_event(2, text="先不看附件，换个问题")))
        await ws.wait_closed()

    async def scenario():
        async with file_window(tmp_path, source=source) as (window, client, protocol, contract):
            def admitted():
                if started.is_set() and (len(contract.events) > 1
                    or "RECALLED" in contract.states.values()):
                    release.set()

            async def download(request):
                started.set()
                await release.wait()
                return contract.download(request)

            contract.accepted = admitted
            monkeypatch.setattr(continuous, "emit", lambda kind, **values: (
                pause.set() if kind == "automatic_input_retired" else None))
            try:
                result = await asyncio.wait_for(continuous.run(window, client, protocol,
                    pause=pause, file_transport=httpx.MockTransport(download),
                    file_resolver=resolve), 8)
            finally:
                release.set()
            assert result["state"] == "OPEN" and release.is_set()
            assert len(contract.lookups) == len(contract.downloads) == 1
            assert not contract.uploads
            row = window.db.execute("SELECT * FROM automatic_media").fetchone()
            assert row["state"] == "CANCELLED" and row["manifest_json"] is None
            assert len(contract.events) == (2 if control == "new_input" else 1)
            window.verify()

    run(scenario())


@pytest.mark.parametrize("change", [
    {"state": "WAITING"}, {"lease_token": "image_lease_wrong"}, {"lease_until": "bad-date"},
    {"lease_until": "2020-01-01T00:00:00+00:00"}, {"max_bytes": True},
    {"max_bytes": 128 * 1024 * 1024 + 1}, {"extra": True},
])
def test_invalid_core_file_claim_never_resolves_a_native_url(tmp_path, change):
    async def scenario():
        async with file_window(tmp_path) as (window, client, protocol, contract):
            row = await capture_and_queue(window, client, protocol)
            contract.claim_change = lambda value: value.update(change)
            with pytest.raises(ValueError, match="FILE_DOWNLOAD_CLAIM_REJECTED"):
                await pump(window, client, protocol, contract).execute(row)
            assert not contract.lookups and not contract.downloads and not contract.uploads
            assert window.db.execute("SELECT state FROM automatic_media").fetchone()[0] == (
                "CLAIMING")

    run(scenario())


@pytest.mark.parametrize("change", [{"approved": False}, {"approved": 1},
                                      {"lease_until": "changed"}, {"extra": True}])
def test_invalid_core_file_check_prevents_private_file_lookup(tmp_path, change):
    async def scenario():
        async with file_window(tmp_path) as (window, client, protocol, contract):
            row = await capture_and_queue(window, client, protocol)
            contract.check_change = lambda value: value.update(change)
            with pytest.raises(ValueError, match="FILE_DOWNLOAD_CHECK_REJECTED"):
                await pump(window, client, protocol, contract).execute(row)
            assert not contract.lookups and not contract.downloads and not contract.uploads

    run(scenario())


@pytest.mark.parametrize("change", [{"state": "SUCCEEDED"}, {"item_id": "wrong"},
                                      {"duplicate": 1}, {"extra": True}])
def test_invalid_file_upload_receipt_does_not_mark_the_media_queue_done(tmp_path, change):
    async def scenario():
        async with file_window(tmp_path) as (window, client, protocol, contract):
            row = await capture_and_queue(window, client, protocol)
            contract.upload_change = lambda value: value.update(change)
            with pytest.raises(ValueError, match="FILE_UPLOAD_RECEIPT_REJECTED"):
                await pump(window, client, protocol, contract).execute(row)
            assert len(contract.uploads) == 1
            assert window.db.execute("SELECT state FROM automatic_media").fetchone()[0] == (
                "UPLOADING")

    run(scenario())


def test_replayed_file_manifest_is_byte_exact_and_drift_is_rejected(tmp_path):
    async def scenario():
        async with file_window(tmp_path) as (window, client, protocol, contract):
            row = await capture_and_queue(window, client, protocol)
            event = contract.events[row["event_id"]]
            captured = window.db.execute("SELECT capture_json FROM window_inputs").fetchone()[0]
            receipt = json.loads(captured)
            before = dict(row), window.plan.journal.read_bytes()
            pending_media.queue(window, event, file_frame(), receipt)
            assert dict(window.db.execute("SELECT * FROM automatic_media").fetchone()) == before[0]
            assert window.plan.journal.read_bytes() == before[1]
            changed = file_frame()
            changed["message"][1]["data"]["file_hash"] = "other-native-resource"
            with pytest.raises(ValueError, match="AUTOMATIC_MEDIA_CHANGED"):
                pending_media.queue(window, event, changed, receipt)
            assert window.plan.journal.read_bytes() == before[1]
            window.db.execute("UPDATE automatic_media SET next_slot=2")
            with pytest.raises(ValueError, match="AUTOMATIC_MEDIA_JOURNAL_CHANGED"):
                window.verify()

    run(scenario())


def test_captured_file_projection_change_fails_before_core_approval_or_native_io(tmp_path):
    async def scenario():
        async with file_window(tmp_path) as (window, client, protocol, contract):
            await window.capture_once(client, protocol)
            row = window.db.execute("SELECT key,body FROM inbox").fetchone()
            event = json.loads(row["body"])
            event["payload"]["rich"]["segments"][0]["filename"] = "swapped.txt"
            window.db.execute("UPDATE inbox SET body=? WHERE key=?", (encode(event), row["key"]))
            with pytest.raises(ValueError, match="CAPTURE_INPUT_CHANGED"):
                await window.approve_captured(client, capture_approval(window), defer_media=True)
            assert not contract.approvals and not contract.lookups and not contract.downloads
            assert window.status()["state"] == "STOPPED"

    run(scenario())
