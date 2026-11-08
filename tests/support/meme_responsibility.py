from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from companion.config import ImageInputPolicy
from tests.postgres_fixture import logical_state
from tests.support.meme_core import body
from tests.support.meme_service import claim, discovery_environment, reservation, result, submit


def snapshot(core):
    return {
        "database": logical_state(core.store),
        "journal": Path(core.settings.paid_model.journal_path).read_bytes(),
        "ledger": core.ledger.path.read_bytes(),
    }


@asynccontextmanager
async def liability_case(core, tmp_path, monkeypatch, *, cap=50000, daily_calls=30):
    paid = {"max_output_tokens": 4096, "daily_calls": daily_calls,
        "meme_input": ImageInputPolicy(
        approval_sha256="e" * 64, estimated_tokens_per_image=100,
        max_bytes_per_image=4000000, max_pixels_per_image=16000000,
        max_images_per_request=5)}
    async with discovery_environment(core, tmp_path, monkeypatch, paid=paid,
        meme_semantics_enabled=True, meme_caption_daily_micro_usd=cap) as values:
        event, _, (client, config, grant, now) = values
        submit(core, event)
        attempt = claim(core)
        reservation(core, attempt)
        result(core, attempt)
        payload = body(core, event, attempt, now)
        response = await client.post("/internal/v1/memes/captions", json=payload)
        assert response.status_code == 200, response.text
        job_id = response.json()["job_id"]
        yield SimpleNamespace(client=client, job_id=job_id, event=event, config=config,
            grant=grant, now=now, payload=payload, attempt=attempt,
            path="/internal/v1/memes/captions/" + job_id + "/responsibility")
