"""Receipt maintenance must not stall a segmented sender behind a read-only WAL snapshot."""

import sqlite3
import time
from contextlib import closing

from companion.config import SegmentedReplyConfig
from tests.conftest import run
from tests.native_continuous_support import answer, frame, host


def test_result_checkpoint_does_not_wait_for_an_observer(core, tmp_path, record_property):
    async def source(ws):
        await ws.send(frame())
        await ws.wait_closed()

    async def scenario():
        policy = SegmentedReplyConfig(max_segments="unlimited", mode="semantic", interval_ms=600)
        async with host(core, tmp_path, source, policy=policy, ticking=False,
                        model_reply=lambda _: answer("第一段\n第二段")) as (active, _, sends, _):
            await active.capture()
            await active.flush()
            assert await core.run_once()
            usage = core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')
            active.db.execute("PRAGMA busy_timeout=2000")
            database = active.spool.directory / "spool.sqlite3"
            uri = database.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as view:
                view.execute("PRAGMA query_only=ON")
                view.execute("BEGIN")
                assert view.execute("SELECT COUNT(*) FROM window_attempts").fetchone()[0] == 0
                started = time.perf_counter()
                assert await active.pump()
                elapsed = time.perf_counter() - started
                record_property("first_segment_seconds", elapsed)
                assert elapsed < 1.5, {"receipt_seconds": elapsed, "busy_timeout_ms": 2000}
                assert len(sends) == 1 and active.attempts() == {"ACK": 1}
                assert view.execute("SELECT COUNT(*) FROM window_attempts").fetchone()[0] == 0
                core.clock.advance(0.5)
                assert not await active.pump() and len(sends) == 1
                core.clock.advance(0.1)
                started = time.perf_counter()
                assert await active.pump()
                elapsed = time.perf_counter() - started
                record_property("second_segment_seconds", elapsed)
                assert elapsed < 1.5
                assert len(sends) == 2 and active.attempts() == {"ACK": 2}
            active.restart()
            assert active.attempts() == {"ACK": 2}
            assert not await active.pump() and len(sends) == 2
            assert core.store.rows("SELECT state FROM outbox ORDER BY segment_index") == [
                {"state": "SENT"}, {"state": "SENT"}]
            assert core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence') == usage
            assert active.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    run(scenario())
