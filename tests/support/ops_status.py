from companion.config import Settings


def settings(tmp_path):
    return Settings(owner_qq_id="10001", bot_qq_id="10002", bridge_token="synthetic-token-" * 3,
                    database=str(tmp_path / "core.db"),
                    erasure_ledger=str(tmp_path / "erasures.jsonl"), proactive_enabled=False)
