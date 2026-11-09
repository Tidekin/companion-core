"""Original visual evidence is rechecked after final request hooks, before provider I/O."""

import hashlib
import json

import httpx
import pytest

from companion import owner_authority
from companion.agent_reading import revalidate_results
from companion.cloud_budget import CloudBudgetConfig
from companion.contracts import CoreError
from companion.core import Core
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop
from companion.store import after, encode
from companion_onebot_bridge import owner_notices
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.bili_video import (
    final_result,
    model_response,
    paid_media,
    phase_usage,
    upload_body,
)
from tests.support.failure_notice import assert_failure_notice
from tests.support.owner_recall_bridge import recall_frame


@pytest.mark.parametrize(
    "boundary",
    [
        "complete",
        "source",
        "body",
        "coherent_body",
        "evidence",
        "ttl",
        "receipt",
        "paid",
        "result_ids",
        "stop",
        "recall",
    ],
)


def test_original_visual_result_checked_at_final_model_boundary(
    core,
    tmp_path,
    boundary,
):
    controls, sent, checked = {}, [], []

    async def scenario():
        async with paid_media(
            core,
            tmp_path,
            lambda _: model_response({"analysis": "原采样帧中的蓝色区域", "need_clips": False}),
            controls=controls,
        ) as (module, item, prepared):
            result = await module.upload("bili", item["request_id"], upload_body(item, prepared))
            module.result(
                "bili", item["request_id"], final_result(item, prepared, [result], status="SUCCESS")
            )
            visual = core.store.one("SELECT * FROM bili_video_items")
            original_usage = phase_usage(core)
            assert len(original_usage) == 1 and original_usage[0]["state"] == "SETTLED"

            async def before_send(request):
                wire = json.loads(request.content)
                assert "原采样帧中的蓝色区域" in encode(wire["messages"])
                checked.append(request)
                event_id = visual["output_event_id"]
                if boundary == "source":
                    core.store.execute(
                        'UPDATE events SET source=%s WHERE event_id=%s',
                        ("observation.bilibili", visual["owner_event_id"]),
                    )
                elif boundary in {"body", "coherent_body"}:
                    row = core.store.one(
                        'SELECT body_json FROM events WHERE event_id=%s', (event_id,)
                    )
                    body = json.loads(row["body_json"])
                    body["text"] = body["text"].replace("蓝色区域", "篡改区域")
                    core.store.execute(
                        'UPDATE events SET body_json=%s WHERE event_id=%s', (encode(body), event_id)
                    )
                    if boundary == "coherent_body":
                        core.store.execute(
                            'UPDATE observations SET source_sha256=%s WHERE event_id=%s',
                            (hashlib.sha256(body["text"].encode()).hexdigest(), event_id),
                        )
                        core.store.execute(
                            "UPDATE bili_video_phases SET analysis_json=%s WHERE phase='preview'",
                            (encode({"analysis": "原采样帧中的篡改区域", "need_clips": False}),),
                        )
                elif boundary == "evidence":
                    row = core.store.one(
                        'SELECT evidence_json FROM observations WHERE event_id=%s', (event_id,)
                    )
                    evidence = json.loads(row["evidence_json"])
                    evidence["bili_video"]["phases"][0]["payload_sha256"] = "0" * 64
                    core.store.execute(
                        'UPDATE observations SET evidence_json=%s WHERE event_id=%s',
                        (encode(evidence), event_id),
                    )
                elif boundary == "ttl":
                    core.store.execute(
                        'UPDATE events SET ingested_at=%s WHERE event_id=%s',
                        (
                            after(
                                core.clock(), -86400 * (core.settings.raw_event_retention_days + 1)
                            ),
                            event_id,
                        ),
                    )
                elif boundary == "receipt":
                    row = core.store.one(
                        "SELECT response_json FROM bili_video_phases WHERE phase='preview'"
                    )
                    receipt = json.loads(row["response_json"])
                    receipt["receipt_sha256"] = "0" * 64
                    core.store.execute(
                        "UPDATE bili_video_phases SET response_json=%s WHERE phase='preview'",
                        (encode(receipt),),
                    )
                elif boundary == "paid":
                    core.store.execute(
                        "UPDATE bili_video_phases SET state='UNKNOWN' WHERE phase='preview'"
                    )
                elif boundary == "result_ids":
                    core.store.execute(
                        'UPDATE reading_attempts SET result_event_ids_json=%s WHERE request_id=%s',
                        (encode([visual["source_event_id"]]), item["request_id"]),
                    )
                elif boundary == "stop":
                    owner_authority.stop(
                        core.store,
                        core.settings,
                        core.clock,
                        AuthorityStop(
                            operation_id="stop-final-visual-read",
                            authority=controls["window"].runtime.authority,
                        ),
                    )
                elif boundary == "recall":
                    runtime = controls["window"].runtime
                    owner_notices.put(
                        runtime,
                        recall_frame(42, time=int(core.clock.now.timestamp())),
                        core.clock.now,
                        ("127.0.0.1", 3001),
                    )
                    assert await owner_notices.flush_one(runtime, controls["client"])

            def provider(request):
                sent.append(json.loads(request.content))
                return httpx.Response(200, json=answer("根据原采样的局部画面回答。"))

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(provider), event_hooks={"request": [before_send]}
            ) as http:
                with Core(core.settings, ModelClient(core.settings, http), core.clock) as worker:
                    assert worker.store is not core.store
                    assert await worker.run_once("final-visual-read")
            assert len(checked) == 1
            assert len(sent) == int(boundary == "complete")
            assert phase_usage(core) == original_usage
            if boundary == "complete":
                assert len(core.store.rows("SELECT * FROM outbox")) == 1
                proof = json.loads(
                    core.store.one("SELECT snapshots_json FROM agent_conversations")[
                        "snapshots_json"
                    ]
                )
                assert len([row for row in proof if row["kind"] == "reader"]) == 2
                assert core.store.one(
                    'SELECT 1 FROM job_sources WHERE event_id=%s', (visual["output_event_id"],)
                )
            else:
                if boundary in {"source", "stop", "recall"}:
                    assert not core.store.one("SELECT 1 FROM outbox")
                else:
                    assert_failure_notice(core)
                final = core.store.rows(

                        'SELECT m.request_id,m.state,m.billed_micro_usd,a.io_'
                        'started_at FROM model_usage m JOIN model_admissions '
                        "a USING(request_id) WHERE scenario='chat' ORDER BY "
                        'm.record_sequence DESC LIMIT 1'

                )[0]
                assert final["state"] == "SETTLED" and final["billed_micro_usd"] == 0
                assert core.store.one((
                    'SELECT 1 FROM audit_log WHERE entity_id=%s AND '
                    "action='model.not_dispatched'"
                ), (final["request_id"],))

    run(scenario())


@pytest.mark.parametrize("unknown_cost", [False, True])
def test_original_paid_preview_remains_readable_with_limited_model_call_room(
    core, tmp_path, unknown_cost,
):
    def media_provider(_):
        value = model_response({"analysis": "只观察到局部预览帧", "need_clips": True},
                               tokens=18000).json()
        if unknown_cost:
            del value["usage"]
        return httpx.Response(200, json=value)

    async def scenario():
        async with paid_media(core, tmp_path,
            media_provider,
            cloud=CloudBudgetConfig(), call_limit=4,
        ) as (module, item, prepared):
            preview = await module.upload("bili", item["request_id"], upload_body(item, prepared))
            assert preview["next_selection"] is None
            module.result("bili", item["request_id"],
                          final_result(item, prepared, [preview], status="SUCCESS"))
            usage = phase_usage(core)
            assert len(usage) == 1 and usage[0]["billed_micro_usd"] > 0
            assert usage[0]["state"] == ("UNKNOWN" if unknown_cost else "SETTLED")
            if not unknown_cost:
                assert usage[0]["billed_micro_usd"] == 18040
            provider = Provider(answer("只能根据有限的局部预览来回答。"))
            async with worker_for(core, provider) as worker:
                assert await worker.run_once("partial-visual-read")
            assert len(provider.requests) == 1
            wire = encode(provider.requests[0]["messages"])
            assert "只观察到局部预览帧" in wire and "未完整观看" in wire
            assert phase_usage(core) == usage
            assert not core.store.one("SELECT 1 FROM bili_video_phases WHERE phase='clips'")
    run(scenario())


@pytest.mark.parametrize("drift", [
    "body", "phase", "ttl", "source", "receipt", "result_ids", "binding",
])


def test_completed_job_reader_proof_revalidates_readonly_before_delivery(core, tmp_path, drift):
    async def scenario():
        async with paid_media(core, tmp_path,
            lambda _: model_response({"analysis": "原始视觉结果", "need_clips": False})) as (
                module, item, prepared):
            result = await module.upload("bili", item["request_id"], upload_body(item, prepared))
            module.result("bili", item["request_id"],
                          final_result(item, prepared, [result], status="SUCCESS"))
            provider = Provider(answer("根据原始视觉结果回答。"))
            async with worker_for(core, provider) as worker:
                assert await worker.run_once("original-visual-completion")
            job = core.store.one("SELECT j.*,e.conversation_id FROM jobs j JOIN events e "
                                 "USING(event_id) WHERE j.state='SUCCEEDED'")
            saved = core.store.one('SELECT snapshots_json FROM agent_conversations WHERE job_id=%s',
                                   (job["job_id"],))["snapshots_json"]
            snapshots = json.loads(saved)
            usage = core.store.rows("SELECT * FROM model_usage")
            with core.store.read_snapshot():
                revalidate_results(core.store, core.settings, core.clock, job, snapshots)
            visual = core.store.one("SELECT * FROM bili_video_items")
            if drift == "body":
                core.store.execute('UPDATE events SET body_json=%s WHERE event_id=%s',
                    (encode({"text": "被更换的视觉正文"}), visual["output_event_id"]))
            elif drift == "phase":
                core.store.execute((
                    'UPDATE bili_video_phases SET payload_sha256=%s WHERE '
                    "phase='preview'"
                ), ("0" * 64,))
            elif drift == "ttl":
                core.store.execute('UPDATE events SET ingested_at=%s WHERE event_id=%s',
                    (after(core.clock(), -86400 * (core.settings.raw_event_retention_days + 1)),
                     visual["output_event_id"]))
            elif drift == "source":
                core.store.execute('UPDATE events SET source=%s WHERE event_id=%s',
                    ("observation.bilibili", visual["owner_event_id"]))
            elif drift == "receipt":
                core.store.execute("UPDATE bili_video_phases SET response_json='{}' "
                                   "WHERE phase='preview'")
            elif drift == "binding":
                core.store.execute((
                    'DELETE FROM exploration_evidence WHERE request_id IN '
                    '(SELECT read_request_id FROM content_items WHERE '
                    'item_id=%s) AND event_id=%s'
                ),
                    (visual["item_id"], visual["source_event_id"]))
            else:
                core.store.execute((
                    "UPDATE reading_attempts SET result_event_ids_json='[]' "
                    'WHERE request_id=%s'
                ), (item["request_id"],))
            with core.store.read_snapshot():
                with pytest.raises(CoreError):
                    revalidate_results(core.store, core.settings, core.clock, job, snapshots)
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert core.store.one('SELECT snapshots_json FROM agent_conversations WHERE job_id=%s',
                                   (job["job_id"],))["snapshots_json"] == saved
            assert len(provider.requests) == 1
            assert core.store.one("SELECT state FROM outbox") == {"state": "PENDING"}
    run(scenario())
