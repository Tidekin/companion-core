"""Separate execution service. Never receives Core storage paths or QQ credentials."""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import sys
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException

from companion.executive_protocol import ExecutionEnvelope, ToolReceipt
from companion.executor_files import open_scoped


def receipt(request, status, text="", error=None, billed=0, calls=0, usage=None):
    return ToolReceipt(
        task_id=request.task_id,
        scope_sha256=request.scope_sha256,
        status=status,
        text=text,
        evidence_sha256=hashlib.sha256(text.encode()).hexdigest(),
        billed_micro_usd=billed,
        model_calls=calls,
        input_tokens=(usage or {}).get("input_tokens", 0),
        output_tokens=(usage or {}).get("output_tokens", 0),
        unknown_model_calls=(usage or {}).get("unknown_calls", 0),
        error_code=error,
    )


def scoped_path(root, relative):
    root = Path(os.path.abspath(root))
    parts = relative.replace("\\", "/").split("/")
    if not parts or any(
        part in {"", ".", ".."}
        or ":" in part
        or part.startswith(".")
        or part != part.rstrip(" .")
        or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", part.split(".")[0], re.I)
        for part in parts
    ):
        raise ValueError("PATH_OUTSIDE_WORKSPACE")
    path = root.joinpath(*parts)
    if path.suffix.lower() not in {".txt", ".md", ".json", ".csv", ".log", ".py"}:
        raise ValueError("FILE_TYPE_NOT_ALLOWED")
    return path


def purge_job_files(run_dir):
    shutil.rmtree(run_dir)
    # Absolute /tmp and POSIX shared-memory writes are possible in the isolated container.
    # UID 10002 belongs solely to this service's disposable job; retain supervisor files.
    for directory in (Path("/tmp"), Path("/dev/shm")):
        for path in directory.iterdir():
            if path.lstat().st_uid != 10002:
                continue
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                shutil.rmtree(path)


class FileExecutor:
    """Native Windows-compatible tools, sharing the same scopes with the MCP endpoint."""

    def __init__(self, workspaces, allow_changes=False):
        self.workspaces, self.allow_changes = workspaces, allow_changes

    async def execute(self, request):
        if request.workspace not in self.workspaces:
            return receipt(request, "FAILED", error="WORKSPACE_NOT_ALLOWED")
        started = False
        try:
            path = scoped_path(self.workspaces[request.workspace], request.arguments.path)
            if request.capability == "windows.read_text":
                with open_scoped(path) as stream:
                    raw = stream.read(48001)
                if len(raw) > 48000:
                    raise ValueError("FILE_TOO_LARGE")
                text = raw.decode("utf-8")
                if len(text) > 12000:
                    raise ValueError("FILE_TOO_LARGE")
                return receipt(request, "SUCCEEDED", text or "[empty UTF-8 file]")
            if request.capability != "windows.write_text" or not self.allow_changes:
                raise ValueError("CAPABILITY_DISABLED")
            # Create-only avoids replacing user data. Scope approval binds exact bytes and path.
            with open_scoped(path, create=True) as stream:
                started = True
                stream.write(request.arguments.text.encode())
                stream.flush()
                os.fsync(stream.fileno())
                stream.seek(0)
                digest = hashlib.sha256(stream.read(48001)).hexdigest()
            if digest != hashlib.sha256(request.arguments.text.encode()).hexdigest():
                raise ValueError("WRITTEN_CONTENT_CHANGED")
            return receipt(
                request, "SUCCEEDED", f"created {request.arguments.path}; sha256={digest}"
            )
        except (OSError, UnicodeError, ValueError):
            return receipt(
                request,
                "UNKNOWN" if started else "FAILED",
                error="FILE_OPERATION_UNCERTAIN" if started else "FILE_OPERATION_REJECTED",
            )


class DSHExecutor:
    """SDK subprocess runs as a different unprivileged UID on an internal-only network."""

    def __init__(self, proxy_url, proxy_token, scratch, enabled=False, workspace="scratch"):
        self.proxy_url, self.proxy_token = proxy_url.rstrip("/"), proxy_token
        self.scratch, self.enabled, self.workspace = Path(scratch), enabled, workspace
        self.container = Path("/.dockerenv").exists()

    async def execute(self, request):
        if (
            not self.enabled
            or sys.platform != "linux"
            or os.geteuid() != 0
            or not self.container
            or request.workspace != self.workspace
            or request.capability != "dsh.run"
        ):
            return receipt(request, "FAILED", error="DSH_ISOLATION_UNAVAILABLE")
        admin = {"Authorization": "Bearer " + self.proxy_token}
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(
                self.proxy_url + "/internal/v1/reservations",
                headers=admin,
                json=request.model_dump(),
            )
            response.raise_for_status()
            grant = response.json()
            run_dir = Path(tempfile.mkdtemp(prefix="dsh-", dir=self.scratch))
            os.chown(run_dir, 10002, 10002)
            os.chmod(run_dir, 0o700)
            process = None
            try:

                def isolate_identity():
                    os.setgroups([])
                    os.setgid(10002)
                    os.setuid(10002)

                env = {
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": str(run_dir),
                    "TMPDIR": str(run_dir),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": "/app",
                }
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "companion.dsh_job",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=run_dir,
                    env=env,
                    start_new_session=True,
                    preexec_fn=isolate_identity,
                )
                body = {
                    "prompt": request.arguments.prompt,
                    "home": str(run_dir / "home"),
                    "workspace": str(run_dir / "work"),
                    "proxy_url": self.proxy_url + "/v1",
                    "token": grant["token"],
                    "model": grant["model"],
                    "max_tokens": request.max_output_tokens,
                    "timeout": request.timeout_seconds,
                }
                raw, _ = await asyncio.wait_for(
                    process.communicate(json.dumps(body).encode()), timeout=request.timeout_seconds
                )
                if process.returncode or len(raw) > 65536:
                    raise ValueError("DSH_NO_VALID_RESULT")
                result = json.loads(raw)
                if result.get("status") != "SUCCEEDED":
                    raise ValueError("DSH_DID_NOT_COMPLETE")
                text = result["text"]
                if not isinstance(text, str) or not text.strip() or len(text) > 11000:
                    raise ValueError("DSH_NO_VALID_RESULT")
                text = text.replace(grant["token"], "[REDACTED]")
                usage = await client.get(
                    self.proxy_url + "/internal/v1/reservations/" + grant["token"], headers=admin
                )
                usage.raise_for_status()
                usage = usage.json()
                return receipt(
                    request,
                    "SUCCEEDED",
                    "DSH会话返回（外部效果未独立核验）：\n" + text,
                    billed=usage["billed_micro_usd"],
                    calls=usage["calls"],
                    usage=usage,
                    error="MODEL_COST_UNKNOWN" if usage["unknown_calls"] else None,
                )
            except (Exception, asyncio.CancelledError):
                usage = {}
                try:
                    response = await client.get(
                        self.proxy_url + "/internal/v1/reservations/" + grant["token"],
                        headers=admin,
                    )
                    response.raise_for_status()
                    usage = response.json()
                except (Exception, asyncio.CancelledError):
                    pass
                return receipt(
                    request,
                    "UNKNOWN",
                    error="DSH_EXECUTION_UNCERTAIN",
                    billed=usage.get("billed_micro_usd", 0),
                    calls=usage.get("calls", 0),
                    usage=usage,
                )
            finally:
                # Kill the whole process group, including shells forked by the SDK.
                if process:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                    # A shell can detach into another process group. This UID is exclusively
                    # reserved for disposable jobs inside the gateway's private PID namespace.
                    sweeper = os.fork()
                    if sweeper == 0:
                        os.setgroups([])
                        os.setgid(10002)
                        os.setuid(10002)
                        os.kill(-1, signal.SIGKILL)
                        os._exit(0)
                    await asyncio.to_thread(os.waitpid, sweeper, 0)
                try:
                    await client.delete(
                        self.proxy_url + "/internal/v1/reservations/" + grant["token"],
                        headers=admin,
                    )
                finally:
                    purge_job_files(run_dir)


def create_executor_app(token, state_path, file_executor=None, dsh_executor=None):
    if len(token) < 24:
        raise ValueError("executor token must contain at least 24 characters")
    app, lock = FastAPI(), asyncio.Lock()
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS executions (task_id TEXT PRIMARY KEY, "
        "request_hmac TEXT NOT NULL, state TEXT NOT NULL)"
    )
    app.state.db = db

    def authenticate(value):
        if not secrets.compare_digest(value or "", "Bearer " + token):
            raise HTTPException(401, "UNAUTHORIZED")

    @app.post("/internal/v1/execute")
    async def execute(request: ExecutionEnvelope, authorization: str = Header(default="")):
        authenticate(authorization)
        if lock.locked():
            raise HTTPException(409, "EXECUTOR_BUSY")
        async with lock:
            fingerprint = hmac.new(
                token.encode(), request.model_dump_json().encode(), hashlib.sha256
            ).hexdigest()
            row = db.execute(
                "SELECT request_hmac FROM executions WHERE task_id=?", (request.task_id,)
            ).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
                return receipt(
                    request,
                    "UNKNOWN",
                    error="EXECUTION_ALREADY_ATTEMPTED",
                    billed=0,
                )
            # Durable send marker precedes the effect. Never replay effects after a process crash.
            db.execute(
                "INSERT INTO executions VALUES (?,?,'STARTED')", (request.task_id, fingerprint)
            )
            backend = dsh_executor if request.capability == "dsh.run" else file_executor
            result = receipt(request, "FAILED", error="CAPABILITY_DISABLED")
            try:
                if backend:
                    result = await backend.execute(request)
            except Exception:
                result = receipt(
                    request,
                    "UNKNOWN",
                    error="EXECUTION_UNCERTAIN",
                    billed=0,
                )
            db.execute(
                "UPDATE executions SET state=? WHERE task_id=?", (result.status, request.task_id)
            )
            return result

    # Stateless JSON MCP endpoint: the execute tool requires the full already-approved envelope.
    @app.post("/mcp")
    async def mcp(body: dict, authorization: str = Header(default="")):
        authenticate(authorization)
        method, rpc_id = body.get("method"), body.get("id")
        if method == "notifications/initialized":
            from fastapi.responses import Response

            return Response(status_code=202)
        if method == "initialize":
            value = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "companion-scoped-executor", "version": "1.0"},
            }
        elif method == "tools/list":
            value = {
                "tools": [
                    {
                        "name": "execute_scoped",
                        "description": (
                            "Execute one immutable, owner-approved tool envelope; "
                            "never retry UNKNOWN."
                        ),
                        "inputSchema": ExecutionEnvelope.model_json_schema(),
                    }
                ]
            }
        elif method == "tools/call" and body.get("params", {}).get("name") == "execute_scoped":
            request = ExecutionEnvelope.model_validate(body["params"]["arguments"])
            result = await execute(request, authorization)
            value = {
                "content": [{"type": "text", "text": result.model_dump_json()}],
                "isError": result.status != "SUCCEEDED",
            }
        else:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        return {"jsonrpc": "2.0", "id": rpc_id, "result": value}

    return app


def app_from_env():
    return create_executor_app(
        os.environ["EXECUTOR_TOKEN"],
        os.environ.get("EXECUTOR_STATE", "/state/executor.sqlite3"),
        file_executor=FileExecutor(
            json.loads(os.environ.get("EXECUTOR_WORKSPACES", "{}")),
            os.environ.get("EXECUTOR_ALLOW_CHANGES") == "true",
        ),
        dsh_executor=DSHExecutor(
            os.environ.get("BUDGET_PROXY_URL", "http://budget-proxy:8091"),
            os.environ.get("BUDGET_PROXY_ADMIN_TOKEN", ""),
            os.environ.get("EXECUTOR_SCRATCH", "/scratch"),
            os.environ.get("EXECUTOR_DSH_ENABLED") == "true",
        ),
    )
