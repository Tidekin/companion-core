"""Her moment pictures: one queued, priced request each, kept and forgotten with the moment."""

import httpx
import pytest
from pydantic import ValidationError

from companion import moment_images, moments
from companion.contracts import CoreError
from companion.life_protocol import LifeDecision
from tests.conftest import run
from tests.support.image_generation import config, image_reply, png
from tests.support.life_expression_consumption import (
    close_provider_clients as close_provider_clients,
)
from tests.support.moments import moment_ready
from tests.support.owner_console import client as client
from tests.support.owner_console import login

PROMPT = "傍晚窗边，一杯热可可和一本摊开的星图"


def drawn(core, tmp_path, *, purposes=("moments", "expression"), prompt=PROMPT, **settings):
    core.settings.image_generation = config()
    for key, value in settings.items():
        setattr(core.settings, key, value)
    job, *_ = moment_ready(core, tmp_path, decision_extra={"image_prompt": prompt},
                           purposes=list(purposes))
    assert core.life_topics.consume_due(diary=core.diary)
    return core.store.one("SELECT moment_id FROM moments")["moment_id"]


def provide(core, handler):
    core.image_transport = httpx.MockTransport(handler)


def test_a_queued_picture_is_drawn_once_shown_and_forgotten_with_the_moment(core, tmp_path):
    moment_id = drawn(core, tmp_path)
    assert core.store.one("SELECT state FROM moment_images")["state"] == "QUEUED"
    [item] = moments.feed(core.store)
    assert (item["images"], item.get("image_pending")) == ([], True)
    prompts, data = [], png()

    def handler(request):
        prompts.append(request.read())
        return image_reply(data)

    provide(core, handler)
    before = moments.signature(core.store)
    assert run(moment_images.run_once(core))
    assert not run(moment_images.run_once(core))
    assert moments.signature(core.store) != before
    assert len(prompts) == 1 and PROMPT.encode() in prompts[0]
    assert core.store.one("SELECT state FROM moment_images")["state"] == "READY"
    [item] = moments.feed(core.store)
    assert item["images"] == [0] and "image_pending" not in item
    assert moment_images.read(core.store, moment_id, 0) == (data, "image/png")
    usage = core.store.one("SELECT scenario, state FROM model_usage WHERE scenario = %s",
                           (moment_images.SCENARIO,))
    assert usage == {"scenario": "moment_image", "state": "SETTLED"}
    path = moment_images._path(core.store, moment_id, 0)
    assert path.is_file()
    with core.store.transaction(control_write=True):
        moments.forget(core.store, core.ledger, moment_id, core.clock())
    assert not path.exists()
    assert core.store.one("SELECT state, sha256 FROM moment_images") == {
        "state": "ERASED", "sha256": None}
    with pytest.raises(CoreError, match="MOMENT_IMAGE_NOT_FOUND"):
        moment_images.read(core.store, moment_id, 0)


def test_a_hidden_moment_does_not_serve_its_picture(core, tmp_path):
    moment_id = drawn(core, tmp_path)
    provide(core, lambda request: image_reply(png()))
    run(moment_images.run_once(core))
    moments.set_hidden(core.store, moment_id, True, core.clock())
    with pytest.raises(CoreError, match="MOMENT_IMAGE_NOT_FOUND"):
        moment_images.read(core.store, moment_id, 0)


@pytest.mark.parametrize("case", ["disabled", "no_expression", "daily_limit"])
def test_no_picture_is_queued_unless_allowed(core, tmp_path, case):
    if case == "disabled":
        core.settings.image_generation = config()
        moment_ready(core, tmp_path, decision_extra={"image_prompt": PROMPT},
                     purposes=["moments", "expression"])
        core.settings.image_generation = config(enabled=False)
        assert core.life_topics.consume_due(diary=core.diary)
    elif case == "no_expression":
        drawn(core, tmp_path, purposes=("moments",))
    else:
        drawn(core, tmp_path, moments_image_daily_limit=0)
    assert core.store.one("SELECT text FROM moments")["text"]
    assert core.store.rows("SELECT * FROM moment_images") == []


def test_without_provider_dispatch_a_queued_picture_expires_unpaid(core, tmp_path):
    drawn(core, tmp_path)
    assert not run(moment_images.run_once(core))
    core.clock.advance(moment_images.QUEUE_SECONDS)
    assert run(moment_images.run_once(core))
    assert core.store.one("SELECT state, error_code FROM moment_images") == {
        "state": "EXPIRED", "error_code": "MOMENT_IMAGE_PROVIDER_UNAVAILABLE"}
    assert core.store.rows("SELECT * FROM model_usage WHERE scenario = 'moment_image'") == []
    assert "image_pending" not in moments.feed(core.store)[0]


def test_an_unknown_outcome_keeps_its_reservation_and_is_not_retried(core, tmp_path):
    drawn(core, tmp_path)
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("lost", request=request)

    provide(core, handler)
    assert run(moment_images.run_once(core))
    assert not run(moment_images.run_once(core))
    assert len(calls) == 1
    assert core.store.one("SELECT state FROM moment_images")["state"] == "UNKNOWN"
    assert core.store.one("SELECT state FROM model_usage WHERE scenario = 'moment_image'")[
        "state"] == "UNKNOWN"
    assert moments.feed(core.store)[0]["images"] == []


def test_a_rejected_answer_settles_and_leaves_the_moment_text_only(core, tmp_path):
    drawn(core, tmp_path)
    provide(core, lambda request: httpx.Response(200, json={"data": [{"url": "https://x/y"}]}))
    assert run(moment_images.run_once(core))
    assert core.store.one("SELECT state FROM moment_images")["state"] == "FAILED"
    assert core.store.one("SELECT state FROM model_usage WHERE scenario = 'moment_image'")[
        "state"] == "SETTLED"


def test_moment_pictures_count_toward_the_total_daily_image_limit(core, tmp_path):
    drawn(core, tmp_path)
    core.settings.image_generation = config(max_daily_images=0)
    provide(core, lambda request: image_reply(png()))
    assert run(moment_images.run_once(core))
    assert core.store.one("SELECT state, error_code FROM moment_images") == {
        "state": "EXPIRED", "error_code": "IMAGE_GENERATION_DAILY_LIMIT"}


def test_a_picture_prompt_belongs_only_to_a_moment():
    base = {"action": "SHARE", "source_event_ids": ["evt_a"], "reason": "r", "text": "t"}
    assert LifeDecision.model_validate(
        {**base, "destination": "PRIVATE_MOMENT", "image_prompt": PROMPT}).image_prompt
    for bad in ({"destination": "PRIVATE_DIARY", "image_prompt": PROMPT},
                {"destination": "PRIVATE_MOMENT", "image_prompt": "  "}):
        with pytest.raises(ValidationError):
            LifeDecision.model_validate({**base, **bad})
    # Absent stays absent, so earlier decisions keep their exact stored form.
    assert "image_prompt" not in LifeDecision.model_validate(
        {**base, "destination": "PRIVATE_MOMENT"}).model_dump()

def _interrupt_image_worker(settings, now, phase, channel):
    from contextlib import ExitStack, closing
    from types import SimpleNamespace
    from unittest.mock import patch

    from companion.postgres import Database
    from companion.store import Store

    def pause():
        channel.send("paused")
        channel.recv()

    def provider(request):
        channel.send("called")
        if phase == "dispatched":
            pause()
        return image_reply(png())

    def write_then_pause(*args):
        write(*args)
        pause()

    with ExitStack() as stack:
        database = stack.enter_context(Database(settings.database))
        store = stack.enter_context(closing(Store(database, settings.state_directory)))
        core = SimpleNamespace(settings=settings, clock=lambda: now, store=store,
                               image_transport=httpx.MockTransport(provider))
        write = moment_images._write
        if phase == "reserved":
            stack.enter_context(patch.object(moment_images.cloud_budget, "mark_dispatched",
                                            lambda *args: pause()))
        if phase == "written":
            stack.enter_context(patch.object(moment_images, "_write", write_then_pause))
        run(moment_images.run_once(core))


@pytest.mark.parametrize(("phase", "erased"), [
    ("reserved", False), ("dispatched", False), ("written", False),
    ("reserved", True), ("dispatched", True),
])
def test_dead_worker_recovery_preserves_fees_files_and_native_feed(
    client, core, tmp_path, phase, erased,
):
    import multiprocessing

    moment_id = drawn(core, tmp_path)
    headers = login(client)
    feed = "/v1/console/chat/moments"
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe()
    worker = ctx.Process(target=_interrupt_image_worker,
                         args=(core.settings, core.clock(), phase, sender))
    worker.start()
    sender.close()
    path = moment_images._path(core.store, moment_id, 0)
    try:
        if phase != "reserved":
            assert receiver.poll(45) and receiver.recv() == "called"
        assert receiver.poll(45) and receiver.recv() == "paused"
        assert client.get(feed, headers=headers).json()["items"][0]["image_pending"]
        assert path.exists() == (phase == "written")
        assert not run(moment_images.run_once(core, allow_draw=False))
    finally:
        worker.terminate()
        worker.join(15)
        receiver.close()
        assert not worker.is_alive()
        worker.close()

    if erased:
        with core.store.transaction(control_write=True):
            moments.forget(core.store, core.ledger, moment_id, core.clock())
    assert run(moment_images.run_once(core, allow_draw=False))
    assert not run(moment_images.run_once(core))
    entries = client.get(feed, headers=headers).json()["items"]
    if erased:
        assert entries == []
    else:
        assert "image_pending" not in entries[0] and entries[0]["images"] == []
    assert not path.exists() and not path.with_suffix(".tmp").exists()
    row = core.store.one(
        "SELECT i.state, u.state AS usage_state, u.billed_micro_usd, c.state AS cost_state, "
        "c.accounted_micro_usd, c.reserved_micro_usd, c.dispatched_at FROM moment_images i "
        "JOIN model_usage u ON u.request_id=i.usage_request_id JOIN cloud_cost_snapshots c "
        "ON c.reference_type='model' AND c.reference_id=u.request_id WHERE i.moment_id=%s",
        (moment_id,))
    if phase == "reserved":
        assert row["state"] == ("ERASED" if erased else "EXPIRED")
        assert row["usage_state"] == row["cost_state"] == "SETTLED"
        assert row["billed_micro_usd"] == row["accounted_micro_usd"] == 0
        assert row["dispatched_at"] is None
    else:
        assert row["state"] == ("ERASED" if erased else "UNKNOWN")
        assert row["usage_state"] == row["cost_state"] == "UNKNOWN"
        assert row["billed_micro_usd"] == row["accounted_micro_usd"]
        assert row["accounted_micro_usd"] == row["reserved_micro_usd"] > 0
        assert row["dispatched_at"] is not None
