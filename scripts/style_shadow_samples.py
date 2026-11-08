"""Print the live style segment that approved candidates WOULD add, offline and read-only.

Opens a read-only PostgreSQL snapshot, reads approved, unexpired style candidates and
whether their sources are still live, then prints the system-prompt segment `style_live`
would append for a few synthetic owner messages. It ignores the `style_live` switch and owner
authority purposes on purpose, makes no model call and sends nothing.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion import style_observation  # noqa: E402

SAMPLES = (
    "今天在楼下看到一只猫把自己卡在纸箱里",
    "下班路上看到有人遛一只穿雨衣的柯基，笑死",
    "这个新出的手机我觉得一般，价格也离谱",
    "帮我查一下明天的天气",
    "好的",
    "先这样，我去洗澡了",
    "今天去医院拿复查结果，有点担心",
)


class ReadOnlyStore:
    """The two read methods `style_observation.live_segment` uses, over a read-only handle."""

    def __init__(self, db):
        self.db = db

    def rows(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None


def database_connection(value):
    if value:
        return value
    from companion.config import Settings

    return Settings.load(apply_operator_config=False).database.get_secret_value()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="Core PostgreSQL DSN; default: database from COMPANION_CONFIG")
    parser.add_argument("--now", help="ISO time; default: current UTC time")
    parser.add_argument("--text", action="append", help="message to try (repeatable)")
    args = parser.parse_args(argv)
    now = args.now or datetime.now(UTC).isoformat(timespec="microseconds")
    with psycopg.connect(database_connection(args.db), row_factory=dict_row,
                         options="-c default_transaction_read_only=on") as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        store = ReadOnlyStore(db)
        for text in args.text or SAMPLES:
            added, used, decision = style_observation.live_segment(store, now, text)
            print(f"== {text}")
            print(f"surface_intent={decision.surface_intent} "
                  f"candidates={[c.candidate_id for c in used]} "
                  f"units={style_observation.units(added)}")
            print(added.strip() or "（无追加段落）")
            print()


if __name__ == "__main__":
    main()
