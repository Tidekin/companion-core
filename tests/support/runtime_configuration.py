import pytest
from pydantic import SecretStr

from companion import operator_config
from tests.conftest import ingest
from tests.support.owner_console import PASSWORD


@pytest.fixture
def hot(core):
    core.settings.operator_hot_reload = True
    core.settings.control_token = SecretStr(PASSWORD)
    core.store.configuration_worker = "worker"
    return core


def save(core, **changes):
    row = operator_config.latest(core.store)
    version = row["version"] if row else 0
    return operator_config.update(core.store, core.settings,
        operator_config.ConfigurationChange(expected_version=version, changes=changes),
        f"configuration-{version + 1}", core.clock())


def task(core, number=1):
    accepted = ingest(core, number=number)
    return core.claim_job("configuration-worker", job_id=accepted["job_id"])
