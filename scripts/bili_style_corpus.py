"""Build the CC-STYLE-01 corpus from a query-only copy of existing observations.

Reads the database read-only. Writes the private corpus (with comment text) and the
report (statistics only) into a new directory under this worktree's `.local`.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion import style_corpus  # noqa: E402


def rows(database):
    if not database:
        from companion.config import Settings

        database = Settings.load(apply_operator_config=False).database.get_secret_value()
    with psycopg.connect(database, row_factory=dict_row,
                         options="-c default_transaction_read_only=on") as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return [dict(row) for row in db.execute(
            "SELECT event_id,source_kind,canonical_url,evidence_json,redacted,created_at "
            "FROM observations WHERE canonical_url LIKE 'https://www.bilibili.com/video/%' "
            "AND result_status IN ('FETCHED','WATCHED') ORDER BY created_at")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Core PostgreSQL DSN; default: COMPANION_CONFIG")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".local"):
        raise SystemExit("Corpus output must stay in this worktree's .local directory")
    output.mkdir(parents=True, exist_ok=False)
    corpus, report = style_corpus.build(rows(args.database), datetime.now(UTC))
    for name, value in (("corpus.private.json", corpus), ("report.json", report)):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
