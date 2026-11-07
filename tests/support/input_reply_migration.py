from pathlib import Path

from alembic.config import Config


def config_for(database):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + database.resolve().as_posix())
    return config
