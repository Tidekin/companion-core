from contextlib import asynccontextmanager

from tests.native_continuous_support import frame, host
from tests.sdk_reader_fixture import Provider, answer, worker_for


@asynccontextmanager
async def ready(core, tmp_path, *, protocol_reply=None):
    async def source(ws):
        await ws.send(frame(1, text="synthetic preflight fences"))
        await ws.wait_closed()

    async with host(core, tmp_path, source, protocol_reply=protocol_reply, ticking=False) as (
        active, _, sends, _,
    ):
        await active.capture()
        await active.flush()
        model = Provider(answer("synthetic reply"))
        async with worker_for(core, model) as worker:
            assert await worker.run_once("preflight-fence-worker")
        usage = core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')
        assert len(model.requests) == len(usage) == 1 and usage[0]["state"] == "SETTLED"
        yield active, sends, usage
        assert core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')[:1] == usage
