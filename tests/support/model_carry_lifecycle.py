import httpx
import pytest

from companion import budget, owner_authority
from companion.contracts import CoreError
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop
from tests.conftest import run
from tests.model_carry_support import MESSAGES
from tests.owner_model_fixtures import authorized_job


def current_unknown(core, *, number=2):
    job = authorized_job(core, number=number)
    request_id = budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())

    def provider(request):
        raise httpx.ReadTimeout("native transport returned", request=request)

    async def complete():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with pytest.raises(CoreError, match="MODEL_RETRYABLE"):
                await budget.complete(
                    ModelClient(core.settings, client),
                    core.store,
                    core.settings,
                    request_id,
                    MESSAGES,
                    "chat",
                    core.clock,
                )

    run(complete())
    return request_id


def stop_current(core, case):
    return owner_authority.stop(
        core.store,
        core.settings,
        core.clock,
        AuthorityStop(operation_id="second-normal-stop", authority=case.successor["authority"]),
    )
