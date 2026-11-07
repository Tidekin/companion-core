import copy
import hashlib
import json
from datetime import datetime, timedelta

from companion.source_grants import SourceGrantRegistration
from source_reader.accounts import BILIBILI_COMMIT, HEYBOX_COMMIT
from tests.support.heybox_reader import POST
from tests.support.source_reader import NOW

NOW_DT = datetime.fromisoformat(NOW)


SECRET = "fixture-only-isolated-account-cookie"


def grant(tmp_path, source="heybox", now=NOW_DT, **updates):
    data = {
        "enabled": True,
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "cookies": {"pkey" if source == "heybox" else "SESSDATA": SECRET},
        "upstream_commit": HEYBOX_COMMIT if source == "heybox" else BILIBILI_COMMIT,
    }
    if source == "heybox":
        data.update(account_id="123", device_id="a" * 32)
    data.update(updates)
    path = tmp_path / (source + ".json")
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return path


def registered_grant(core, tmp_path, source="heybox"):
    adapter = "bili" if source == "bilibili" else "heybox"
    grant_id = "grant_" + ("1" if adapter == "heybox" else "2") * 32
    previous = core.store.one(
        "SELECT MAX(epoch) n FROM source_grant_revisions WHERE grant_id=?", (grant_id,)
    )
    revision = (previous["n"] or 0) + 1
    authority = {
        "grant_id": grant_id,
        "epoch": revision,
        "fingerprint": hashlib.sha256(f"fixture-{adapter}-{revision}".encode()).hexdigest(),
    }
    path = grant(
        tmp_path, source, now=datetime.fromisoformat(core.clock()), authorization=authority
    )
    core.observations.grants.register(
        SourceGrantRegistration(
            adapter_id=adapter,
            expires_at=json.loads(path.read_text("utf-8"))["expires_at"],
            **authority,
        )
    )
    return path


def native_post():
    post = copy.deepcopy(POST)
    row = post["result"].pop("link")
    row["content"] = row.pop("text")
    row["link_id"] = row.pop("linkid")
    post["result"]["post"] = row
    return post
