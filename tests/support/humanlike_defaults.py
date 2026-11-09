from companion.config import Settings


def plain(**values):
    values.setdefault("database", "dbname=synthetic")
    return Settings(owner_qq_id="10001", bot_qq_id="10002", bridge_token="x" * 32, **values)
