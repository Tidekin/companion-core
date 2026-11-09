"""Real native admissions with synthetic external provider/deployment observations."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from companion import budget, model_carry, owner_authority, paid_budget
from companion.model import ModelClient
from companion.model_carry_controller import verify
from companion.owner_authority_protocol import AuthorityStop, OwnerCapture
from companion_delivery_protocol import digest
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live

MESSAGES = [{"role": "user", "content": "synthetic completion"}]


def executors(now):
    return [
        {
            "container_id": digit * 64,
            "image_id": "sha256:" + "c" * 64,
            "source_sha256": "d" * 64,
            "service": service,
            "started_at": "2026-10-19T03:00:00+00:00",
            "finished_at": now,
            "process_id": index + 100,
        }
        for index, (digit, service) in enumerate(
            (("a", "companion-api"), ("b", "companion-worker"))
        )
    ]


def observer(items):
    def observe(*args):
        if args[0] == "ps":
            return "\n".join(item["container_id"] for item in items)
        if args[0] == "wait":
            return "\n".join("0" for _ in items)
        return json.dumps(
            [
                {
                    "Id": item["container_id"],
                    "Image": item["image_id"],
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": "synthetic-core",
                            "com.docker.compose.service": item["service"],
                        }
                    },
                    "HostConfig": {"RestartPolicy": {"Name": "no"}},
                    "State": {
                        "Running": False,
                        "Pid": 0,
                        "Restarting": False,
                        "Status": "exited",
                        "StartedAt": item["started_at"],
                        "FinishedAt": item["finished_at"],
                    },
                }
                for item in items
            ]
        )

    return observe


def stranded(core, tmp_path, *, purposes=("chat",), **approval_changes):
    approved_live(core, tmp_path, concurrency=1, **approval_changes)
    job_id = authorized_job(core, purposes=purposes)
    request_id = budget.reserve(core.store, core.settings, job_id, "chat", MESSAGES, core.clock())

    async def exercise():
        def provider(request):
            raise httpx.ReadTimeout("synthetic lost response", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with pytest.raises(Exception, match="MODEL_RETRYABLE"):
                await budget.complete(
                    ModelClient(core.settings, client),
                    core.store,
                    core.settings,
                    request_id,
                    MESSAGES,
                    "chat",
                    core.clock,
                )

    asyncio.run(exercise())
    active, key = core.authorized_model_test_capture
    original_policy = owner_authority.evidence(
        core.store, core.settings, active["authority"]
    ).policy
    owner_authority.stop(
        core.store,
        core.settings,
        core.clock,
        AuthorityStop(operation_id="stop-for-model-carry", authority=active["authority"]),
    )
    items = executors(core.clock())
    verified = verify(items, observe=observer(items))
    core.settings.paid_model = core.settings.paid_model.model_copy(
        update={
            "window_id": "successor-window-1",
            "approval_sha256": "e" * 64,
            "concurrency_basis": "local_active_attempts_v2",
        }
    )
    with core.store.transaction():
        view = owner_authority.custody(core.store, core.settings, settlement=True)
        value = model_carry.manifest(
            core.store,
            core.settings,
            active["authority"],
            core.settings.paid_model.window_id,
            verified.value,
            view,
        )
    policy = original_policy.model_copy(
        update={
            "authorization_sha256": "f" * 64,
            "unknown_carry_sha256": digest(value),
            "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
        }
    )
    request = {
        "version": 1,
        "operation_id": "carry-original-unknown",
        "authorization_sha256": "f" * 64,
        "expected_head_sha256": view["head_sha256"],
        "authority": active["authority"],
        "successor_window_id": core.settings.paid_model.window_id,
        "manifest_sha256": digest(value),
        "successor_policy": policy.model_dump(mode="json"),
        "executors": items,
    }
    capture = OwnerCapture.model_validate(
        json.loads(
            core.store.one(
                'SELECT value FROM runtime_meta WHERE key LIKE '
                "'owner_input:%%'"
            )["value"]
        )["capture"]
    )
    return SimpleNamespace(
        request=request,
        verified=verified,
        policy=policy,
        key=key,
        active=active,
        request_id=request_id,
        capture=capture,
        job_id=job_id,
    )


def migrated(core, tmp_path, **approval_changes):
    from tests.support.owner_authority import activate

    case = stranded(core, tmp_path, **approval_changes)
    case.receipt = model_carry.prepare(
        core.store, core.settings, case.request, case.verified, core.clock()
    )
    successor, _ = activate(core, case.policy, revision=2, operation="activate-carried-successor")
    core.authorized_model_test_capture = successor, case.key
    core.authorized_model_test_jobs = {}
    case.successor = successor
    return case
