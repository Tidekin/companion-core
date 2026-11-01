"""One supervised provider process with bounded pipes and no Core credential."""

import argparse
import asyncio
import base64
import ctypes
import json
import os
import signal
import struct
import sys
from pathlib import Path

from companion.voice_protocol import (
    MAX_INPUT_BYTES,
    MAX_RESULT_BODY_BYTES,
    VoiceAudioResult,
    VoiceProtocolError,
    VoiceTranscriptResult,
    canonical_bytes,
)

MAX_CHILD_INPUT = 4 * ((MAX_INPUT_BYTES + 2) // 3) + 131072
MAX_CHILD_OUTPUT = MAX_RESULT_BODY_BYTES + 8192
TTS_NOT_STARTED_CODES = frozenset(
    {
        "VOICE_REFERENCE_UNAVAILABLE",
        "VOICE_REFERENCE_INVALID",
        "VOICE_TEXT_INVALID",
        "VOICE_BUSY",
        "VOICE_TTS_UNAVAILABLE",
    }
)


def process_identity(pid: int) -> str | None:
    """PID plus OS creation identity prevents confusing a recycled PID with our child."""
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            return f"win:{created.dwHighDateTime}:{created.dwLowDateTime}"
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return "linux:" + fields[19]
        except (OSError, IndexError):
            return None
    return None


def process_has_exited(pid: int, identity: str) -> bool:
    if not isinstance(pid, int) or pid <= 0 or not identity:
        return False
    current = process_identity(pid)
    if current is not None and current != identity:
        return True
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return fields[0] == "Z"
        except FileNotFoundError:
            return True
        except (OSError, IndexError):
            return False
    return False


class _WindowsJob:
    """The kernel terminates assigned processes when the parent loses this handle."""

    def __init__(self, pid: int, *, memory_bytes=None):
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong),
                ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD),
                ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t),
                ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IOCounts(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", IOCounts),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel, self.handle = kernel, kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise VoiceProtocolError("VOICE_PROCESS_CONTAINMENT_UNAVAILABLE")
        limits = ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_bytes is not None:
            limits.basic.flags |= 0x00000200  # JOB_OBJECT_LIMIT_JOB_MEMORY
            limits.job_memory = memory_bytes
        process = None
        try:
            if not kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise VoiceProtocolError("VOICE_PROCESS_CONTAINMENT_UNAVAILABLE")
            process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process or not kernel.AssignProcessToJobObject(self.handle, process):
                raise VoiceProtocolError("VOICE_PROCESS_CONTAINMENT_UNAVAILABLE")
        except Exception:
            self.close()
            raise
        finally:
            if process:
                kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _child_parent_guard(parent_pid: int):
    if os.name == "nt":
        # Parent assigns the Job Object before it sends the first input frame.
        return
    if not sys.platform.startswith("linux"):
        raise VoiceProtocolError("VOICE_PROCESS_PLATFORM_UNSUPPORTED")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        raise VoiceProtocolError("VOICE_PROCESS_CONTAINMENT_UNAVAILABLE")


def _environment(workspace: Path, temporary: Path) -> dict[str, str]:
    # Explicit allowlist: node/owner/Bridge/API credentials never enter child environment.
    result = {
        name: os.environ[name]
        for name in (
            "SystemRoot",
            "WINDIR",
            "COMSPEC",
            "PATH",
            "PATHEXT",
            "LD_LIBRARY_PATH",
            "CUDA_PATH",
            "CUDA_HOME",
            "CUDA_VISIBLE_DEVICES",
        )
        if name in os.environ
    }
    result.update(
        PYTHONPATH=str(workspace),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
        TEMP=str(temporary),
        TMP=str(temporary),
        TMPDIR=str(temporary),
        HF_HUB_OFFLINE="1",
        HF_DATASETS_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HOME=str(temporary / "hf"),
        XDG_CACHE_HOME=str(temporary / "cache"),
        XDG_CONFIG_HOME=str(temporary / "config"),
        APPDATA=str(temporary / "appdata"),
        LOCALAPPDATA=str(temporary / "localappdata"),
        MPLCONFIGDIR=str(temporary / "matplotlib"),
        TORCH_HOME=str(temporary / "torch"),
        NUMBA_CACHE_DIR=str(temporary / "numba"),
    )
    return result


class ProcessSupervisor:
    def __init__(self, workspace: Path, temporary: Path, *, command: tuple[str, ...] | None = None):
        self.workspace, self.temporary = workspace, temporary
        # Injection is a Python test seam; no remote or local-config command field exists.
        self.command = command
        self.process = None
        self._job = None
        self.identity = None
        self.on_started = None
        self.output_limit = MAX_CHILD_OUTPUT
        self.memory_bytes = None

    async def start(self, startup_seconds: float):
        self.temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
        command = self.command or (
            sys.executable,
            "-B",
            "-m",
            "companion_voice_node.supervisor",
            "--child",
            "--parent-pid",
            str(os.getpid()),
        )
        options = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.workspace,
            env=_environment(self.workspace, self.temporary),
            **options,
        )
        try:
            if os.name == "nt":
                self._job = _WindowsJob(self.process.pid, memory_bytes=self.memory_bytes)
            self.identity = process_identity(self.process.pid)
            if self.identity is None:
                raise VoiceProtocolError("VOICE_PROCESS_IDENTITY_UNAVAILABLE")
            if self.on_started is not None:
                self.on_started(self.process.pid, self.identity)
            message = await asyncio.wait_for(self._read(), startup_seconds)
            if message != {"status": "READY"}:
                raise VoiceProtocolError("VOICE_PROCESS_START_FAILED")
        except BaseException:
            await self.stop()
            raise

    async def dispatch(self, packet: dict):
        data = canonical_bytes(packet)
        if len(data) > MAX_CHILD_INPUT:
            raise VoiceProtocolError("VOICE_PROCESS_INPUT_TOO_LARGE")
        self.process.stdin.write(struct.pack("!I", len(data)) + data)
        await self.process.stdin.drain()
        self.process.stdin.close()

    async def _read(self) -> dict:
        try:
            header = await self.process.stdout.readexactly(4)
            length = struct.unpack("!I", header)[0]
            if not 0 < length <= self.output_limit:
                raise VoiceProtocolError("VOICE_PROCESS_OUTPUT_TOO_LARGE")
            result = json.loads(await self.process.stdout.readexactly(length))
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (asyncio.IncompleteReadError, ValueError):
            raise VoiceProtocolError("VOICE_PROCESS_OUTPUT_INVALID") from None

    async def result(self) -> dict:
        return await self._read()

    async def finish(self) -> bool:
        try:
            await asyncio.wait_for(self.process.wait(), 2)
        except TimeoutError:
            return await self.stop()
        self._close_job()
        return self.process.returncode == 0

    async def stop(self) -> bool:
        if self.process is None:
            return True
        if self.process.returncode is None:
            try:
                if os.name == "nt":
                    self.process.terminate()
                else:
                    os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 1)
            except TimeoutError:
                try:
                    if os.name == "nt":
                        self.process.kill()
                    else:
                        os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.process.wait(), 2)
                except TimeoutError:
                    self._close_job()
                    return False
        self._close_job()
        return self.process.returncode is not None

    def _close_job(self):
        if self._job is not None:
            self._job.close()
            self._job = None


def execute_provider(packet: dict) -> dict:
    """Only local provider configuration and input bytes cross this boundary."""
    from companion_voice_node.providers import (
        FasterWhisperConfig,
        FasterWhisperProvider,
        LocalVoiceError,
        OpenAISpeechConfig,
        OpenAISpeechProvider,
    )

    try:
        operation = packet["operation"]
        data = base64.b64decode(packet["input_base64"], validate=True)
        if operation == "tts":
            config = OpenAISpeechConfig.model_validate(packet["provider"])
            audio = asyncio.run(OpenAISpeechProvider(config).synthesize(data.decode("utf-8")))
            payload = VoiceAudioResult(
                audio_base64=base64.b64encode(audio.audio).decode(),
                audio_sha256=audio.sha256,
                sample_rate=audio.sample_rate,
                channels=audio.channels,
                sample_width=audio.sample_width,
                frames=audio.frames,
                duration_seconds=audio.duration_seconds,
            )
        elif operation == "asr":
            config = FasterWhisperConfig.model_validate(packet["provider"])
            result = FasterWhisperProvider(config).transcribe(
                data,
                source_partial=packet["source_partial"],
            )
            payload = VoiceTranscriptResult(
                text=result.text,
                segments=[
                    {
                        "start_seconds": segment.start_seconds,
                        "end_seconds": segment.end_seconds,
                        "text": segment.text,
                    }
                    for segment in result.segments
                ],
                language=result.language,
                language_probability=result.language_probability,
                duration_seconds=result.duration_seconds,
                partial=result.partial,
            )
        elif operation == "sing":
            from companion_voice_node.singing_provider import execute

            payload = execute(packet)
        else:
            raise ValueError
        return {"status": "SUCCEEDED", "payload": payload.model_dump(mode="json")}
    except LocalVoiceError as error:
        not_started = error.code in TTS_NOT_STARTED_CODES
        return {
            "status": "FAILED",
            "error_code": error.code,
            "remote_state": "NOT_STARTED" if not_started else "UNKNOWN",
        }
    except Exception:
        return {
            "status": "FAILED",
            "error_code": "VOICE_PROCESS_EXECUTION_FAILED",
            "remote_state": "UNKNOWN",
        }


def child_entry(parent_pid: int, *, runner=execute_provider) -> int:
    """Framed stdout is reserved; providers cannot print content into the parent log."""
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, sys.stdout.fileno())
    os.close(null)

    output_limit = MAX_CHILD_OUTPUT

    def send(value):
        data = canonical_bytes(value)
        if len(data) > output_limit:
            raise ValueError
        frame = struct.pack("!I", len(data)) + data
        offset = 0
        while offset < len(frame):
            written = output.write(frame[offset:])
            if not written:
                raise OSError
            offset += written

    try:
        _child_parent_guard(parent_pid)
        send({"status": "READY"})
        header = sys.stdin.buffer.read(4)
        if len(header) != 4:
            return 1
        length = struct.unpack("!I", header)[0]
        if not 0 < length <= MAX_CHILD_INPUT:
            return 1
        data = sys.stdin.buffer.read(length)
        if len(data) != length:
            return 1
        packet = json.loads(data)
        if packet.get("operation") == "sing":
            from companion_delivery_protocol.singing import MAX_RESULT_BODY_BYTES as song_limit

            output_limit = song_limit + 8192
        send(runner(packet))
        return 0
    except Exception:
        return 1
    finally:
        output.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    arguments = parser.parse_args()
    raise SystemExit(child_entry(arguments.parent_pid))
