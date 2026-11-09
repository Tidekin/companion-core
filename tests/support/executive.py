import httpx

from companion.executive import Executive
from companion.executive_protocol import (
    ExecutionEnvelope,
    ExecutiveConfig,
    ExecutorEndpoint,
    ToolArguments,
    ToolRequest,
)
from companion.executor_gateway import receipt
from tests.conftest import ingest

TOKEN = "executor-test-token-" * 2


def configured(core, adapter=None, **changes):
    config = ExecutiveConfig(
        enabled=True,
        daily_calls=3,
        endpoints={
            "windows": ExecutorEndpoint(
                url="http://127.0.0.1:8991",
                token=TOKEN,
                workspace="work",
                capabilities=["windows.read_text", "windows.write_text"],
            )
        },
        **changes,
    )
    object.__setattr__(core.settings, "executive", config)
    ingest(core, 1, "请读取允许目录的文件")
    return Executive(core.store, core.settings, core.clock, adapter)


def proposal(number=1, capability="windows.read_text", **arguments):
    return ToolRequest(
        idempotency_key=f"tool-request-{number}",
        capability=capability,
        workspace="work",
        arguments=ToolArguments(path="result.txt", **arguments),
        source_event_ids=["evt_test_1"],
    )


def envelope(**changes):
    values = dict(
        task_id="tool_test",
        scope_sha256="a" * 64,
        capability="windows.read_text",
        workspace="work",
        arguments={"path": "result.txt"},
        max_model_calls=1,
        max_input_bytes=16000,
        max_output_tokens=1024,
        timeout_seconds=20,
    )
    values.update(changes)
    return ExecutionEnvelope(**values)


class FakeExecutor:
    def __init__(self):
        self.calls, self.callback, self.fail = [], None, False

    async def execute(self, endpoint, request):
        self.calls.append(request)
        if self.callback:
            self.callback()
        if self.fail:
            raise httpx.ReadTimeout("response lost after execution")
        return receipt(request, "SUCCEEDED", "实际读取的文本")
