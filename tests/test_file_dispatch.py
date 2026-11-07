"""Final parser transport fences use real launcher code with synthetic OS effects."""
import io
import json
from types import SimpleNamespace

import pytest

from companion import file_parser, file_parser_client
from companion.contracts import CoreError
from tests.support.file_parser import IMAGE, FakeProcess


@pytest.mark.parametrize("stop_at", [1, 2])
def test_docker_fences_launch_and_stdin(monkeypatch, tmp_path, stop_at):
    data = b"private document"
    calls, checks, writes = [], [], []
    result = file_parser.parse_document(data, "notes.txt", "text/plain")
    process = FakeProcess(json.dumps(result).encode())

    class Input(io.BytesIO):
        def write(self, blob):
            writes.append(blob)
            return super().write(blob)

    process.stdin = Input()

    def start(*args, **kwargs):
        calls.append("launch")
        return process

    def check():
        checks.append(True)
        if len(checks) == stop_at:
            raise CoreError("OWNER_AUTHORITY_STOPPED", 403)

    monkeypatch.setattr(file_parser.subprocess, "Popen", start)
    monkeypatch.setattr(file_parser.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=0))
    if stop_at == 1:
        with pytest.raises(CoreError, match="OWNER_AUTHORITY_STOPPED"):
            file_parser.parse_isolated(data, "notes.txt", "text/plain", image=IMAGE,
                                       workspace=tmp_path, effect_check=check)
        assert calls == []
    else:
        output = file_parser.parse_isolated(data, "notes.txt", "text/plain", image=IMAGE,
                                            workspace=tmp_path, effect_check=check)
        assert output["error_code"] == "PARSER_AUTHORITY_CHANGED"
        assert calls == ["launch"]
    assert writes == []


@pytest.mark.parametrize("stop_at", [1, 2, 3])
def test_socket_fences_connect_header_and_body(monkeypatch, stop_at):
    calls, checks = [], []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect(self, path):
            calls.append("connect")

        def sendall(self, data):
            calls.append(data)

    def check():
        checks.append(True)
        if len(checks) == stop_at:
            raise CoreError("OWNER_AUTHORITY_STOPPED", 403)

    monkeypatch.setattr(file_parser_client, "socket", SimpleNamespace(
        AF_UNIX=1, SOCK_STREAM=1, socket=lambda *args: Connection()))
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_STOPPED"):
        file_parser_client.parse_via_socket(b"private document", "notes.txt", "text/plain",
            socket_path="/run/parser.sock", image=IMAGE, request_id="file_test",
            effect_check=check)
    assert len(calls) == stop_at - 1
    assert b"private document" not in calls
