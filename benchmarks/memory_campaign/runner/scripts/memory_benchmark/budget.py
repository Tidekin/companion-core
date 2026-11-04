"""Append-only run budget around the unchanged native paid-admission journal."""

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from filelock import FileLock


class Budget:
    def __init__(self, path, *, base_url, pricing, total_cny, inference_cny, concurrency=4):
        self.path = Path(path)
        self.lock = FileLock(str(self.path) + ".lock")
        self.base_url = base_url.rstrip("/")
        self.pricing = pricing
        self.total_cny, self.inference_cny = total_cny, inference_cny
        self.semaphore = asyncio.Semaphore(concurrency)

    @classmethod
    def from_context(cls, config):
        return cls(
            Path(config.get("campaign_budget_path", Path(config["run_dir"]) / "usage.jsonl")),
            base_url=config["provider"]["base_url"],
            pricing=config["pricing"],
            concurrency=config["concurrency"],
            **config["budget"],
        )

    def append(self, row):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def snapshot(self):
        with self.lock:
            records = (
                [json.loads(s) for s in self.path.read_text("utf-8").splitlines()]
                if self.path.exists()
                else []
            )
            latest = {}
            for row in records:
                latest.setdefault(row["id"], {}).update(row)
            return sum(r["charged_or_held_cny"] for r in latest.values()), latest

    def reserve(self, payload, phase):
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        tokens = len(encoded) + encoded.count(b"data:image/") * 2000 + 2048
        output = payload.get("max_tokens", payload.get("max_completion_tokens", 4096))
        rates = self.pricing
        maximum = (tokens * rates["input_rate"] + output * rates["output_rate"]) * rates[
            "safety_factor"
        ] / 1_000_000 + 0.00001
        cap = self.total_cny if phase == "judge" else self.inference_cny
        with self.lock:
            used, _ = self.snapshot()
            if used + maximum > cap:
                raise RuntimeError("MEMORY_BENCHMARK_CNY_CAP")
            request_id = uuid.uuid4().hex
            self.append(
                {
                    "id": request_id,
                    "state": "RESERVED",
                    "phase": phase,
                    "charged_or_held_cny": maximum,
                    "payload_sha256": hashlib.sha256(encoded).hexdigest(),
                    "at": datetime.now(UTC).isoformat(),
                }
            )
        return request_id, maximum

    def unknown(self, request_id, maximum, reason):
        with self.lock:
            self.append(
                {
                    "id": request_id,
                    "state": "UNKNOWN",
                    "charged_or_held_cny": maximum,
                    "reason": reason,
                    "at": datetime.now(UTC).isoformat(),
                }
            )

    def settle(self, request_id, maximum, value):
        usage = value.get("usage", {})
        if not all(
            type(usage.get(k)) is int and usage[k] >= 0
            for k in ("prompt_tokens", "completion_tokens")
        ):
            self.unknown(request_id, maximum, "MISSING_OR_INVALID_USAGE")
            return
        cached = usage.get(
            "prompt_cache_hit_tokens",
            usage.get("prompt_tokens_details", {}).get("cached_tokens", 0),
        )
        cached = min(usage["prompt_tokens"], max(0, cached)) if type(cached) is int else 0
        rates = self.pricing
        amount = (
            (
                (usage["prompt_tokens"] - cached) * rates["input_rate"]
                + cached * rates["cached_input_rate"]
                + usage["completion_tokens"] * rates["output_rate"]
            )
            * rates["safety_factor"]
            / 1_000_000
        )
        with self.lock:
            self.append(
                {
                    "id": request_id,
                    "state": "SETTLED",
                    "charged_or_held_cny": amount,
                    "reservation_cny": maximum,
                    "usage": usage,
                    "model": value.get("model"),
                    "response_id": value.get("id"),
                    "at": datetime.now(UTC).isoformat(),
                }
            )
        if amount > maximum:
            raise RuntimeError("PROVIDER_USAGE_EXCEEDED_RESERVATION")


class MeteredTransport(httpx.AsyncBaseTransport):
    def __init__(self, budget, phase, *, inner=None):
        self.budget, self.phase = budget, phase
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        expected = urlparse(self.budget.base_url + "/chat/completions")
        if str(request.url).split("?")[0] != expected.geturl() or request.url.query:
            raise RuntimeError("MEMORY_BENCHMARK_ENDPOINT_MISMATCH")
        async with self.budget.semaphore:
            phase = self.phase() if callable(self.phase) else self.phase
            request_id, maximum = self.budget.reserve(json.loads(request.content), phase)
            try:
                response = await self.inner.handle_async_request(request)
                await response.aread()
                if response.status_code == 200:
                    try:
                        value = response.json()
                    except ValueError:
                        self.budget.unknown(request_id, maximum, "NON_JSON_RESPONSE")
                    else:
                        self.budget.settle(request_id, maximum, value)
                else:
                    self.budget.unknown(request_id, maximum, "HTTP_" + str(response.status_code))
                return response
            except BaseException as error:
                # Preserve cancellation/transport uncertainty; no automatic API retry.
                _, latest = self.budget.snapshot()
                if latest[request_id]["state"] == "RESERVED":
                    self.budget.unknown(request_id, maximum, type(error).__name__)
                raise

    async def aclose(self):
        await self.inner.aclose()
