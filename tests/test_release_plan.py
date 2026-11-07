"""Release plan: probe on a copy, classification and Compose inspection, without Docker."""

import hashlib
import sqlite3
from datetime import timedelta

import pytest

from companion import owner_authority, release_probe
from companion.contracts import CoreError
from scripts import release_plan as plan
from tests.postgres_fixture import isolated_database
from tests.support.owner_authority import activate
from tests.test_owner_authority import prepare


def test_probe_on_current_schema_needs_no_migration(core):
    result = release_probe.probe(load=lambda: core.settings)
    assert result["migration"] == {"needed": False}
    assert result["before"]["schema"] == result["before"]["expected_schema"]
    assert result["authority"]["state"] == "ABSENT"


def test_probe_migrates_only_the_copy_it_is_given_and_times_it(core, tmp_path):
    original = release_probe.snapshot(core.settings)["database"]["schema"]
    with isolated_database() as empty:
        settings = core.settings.model_copy(update={"database": empty,
            "state_directory": str(tmp_path / "copy-state")})
        result = release_probe.probe(load=lambda: settings)
        assert result["migration"]["needed"] and result["migration"]["ok"]
        assert result["migration"]["seconds"] >= 0
        assert result["before"]["schema"] is None
        assert result["after"]["schema"] == result["before"]["expected_schema"]
    assert release_probe.snapshot(core.settings)["database"]["schema"] == original


def test_probe_reports_whether_the_stored_policy_survives(core, tmp_path):
    activate(core, prepare(core, tmp_path))
    result = release_probe.probe(load=lambda: core.settings)
    assert result["authority"]["state"] == "ACTIVE"
    assert result["authority"]["policy_problem"] is None
    paid = core.settings.paid_model
    core.settings.paid_model = paid.model_copy(update={
        "expires_at": paid.expires_at + timedelta(seconds=30)})
    drifted = release_probe.probe(load=lambda: core.settings)
    assert drifted["authority"]["policy_problem"] == "OWNER_AUTHORITY_PAID_POLICY_CHANGED"
    # current() raises exactly what policy_problem reports.
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PAID_POLICY_CHANGED"):
        owner_authority.current(core.store, core.settings, core.clock)


def probe(*, needed=False, ok=True, blockers=(), problem=None, state="ACTIVE"):
    before = {"schema": "0048" if needed else "0049", "expected_schema": "0049",
              "blockers": list(blockers) + (["MIGRATION_REQUIRED"] if needed else []),
              "active_jobs": 0, "open_deliveries": 0, "uncertain_deliveries": 0,
              "unknown_model_calls": 2}
    value = {"before": before, "migration": {"needed": needed, "ok": ok, "seconds": 1.5},
             "authority": {"state": state, "revision": 33, "policy_problem": problem}}
    if needed and ok:
        value["after"] = {**before, "schema": "0049", "blockers": list(blockers)}
    return value


CORE = {"core": {"from": "a", "to": "b", "services": ["companion-api"]}}
BRIDGE = {"bridge": {"from": "c", "to": "d", "services": ["bridge"]}}


@pytest.mark.parametrize(("changed", "value", "policy", "expected"), [
    ({}, None, None, "NO_CHANGE"),
    ({"ui": {"from": "u", "to": "v", "services": ["console-ui"]}}, None, None, "EDGE"),
    (BRIDGE, None, None, "NOT_READY"),
    (CORE, probe(), None, "A"),
    (CORE, probe(needed=True), None, "B"),
    (CORE, probe(problem="OWNER_AUTHORITY_CAPABILITY_CHANGED"), None, "C"),
    (CORE, probe(), "renew paid approval", "C"),
    ({}, probe(), "add evaluation purpose", "C"),
    (CORE, probe(needed=True, ok=False), None, "NOT_READY"),
    (CORE, probe(blockers=["ERASURE_REPLAY_REQUIRED"]), None, "NOT_READY"),
    (CORE, probe(state="UNREADABLE"), None, "NOT_READY"),
    (CORE, {"error": "PROBE_UNAVAILABLE"}, None, "NOT_READY"),
    (CORE, None, None, "NOT_READY"),
])
def test_classification(changed, value, policy, expected):
    assert plan.classify(changed, value, policy)[0] == expected


def test_report_lists_steps_and_measured_facts():
    value = probe(needed=True)
    kind, reasons = plan.classify(CORE, value)
    text = plan.report({"class": kind, "reasons": reasons, "changes": CORE, "probe": value})
    assert text.startswith("# 发布预检：B")
    assert "副本上耗时 1.5 秒" in text and "UNKNOWN 模型调用 2" in text
    assert "companion init" in text


def inspected(service, image, mounts=(), state="running"):
    return {"Name": f"/companion-main-{service}-1", "Image": image,
            "State": {"Status": state},
            "Config": {"Labels": {"com.docker.compose.service": service}, "User": "10001:10001",
                       "Entrypoint": ["/app/.venv/bin/python", "-m", "companion"],
                       "Env": ["COMPANION_CONFIG=/private/core.toml", "COMPANION_TELEMETRY=1",
                               "PATH=/usr/bin"]},
            "Mounts": list(mounts)}


def test_services_and_changes_follow_the_running_images():
    current = plan.services([
        inspected("companion-api", "core1", [{"Type": "bind", "Source": "/data",
                                              "Destination": "/data", "RW": True}]),
        inspected("companion-worker", "core1", state="exited"),
        inspected("bridge", "bridge1"),
        inspected("console-ui", "ui1"),
    ])
    assert current["companion-api"]["env"] == ["COMPANION_CONFIG=/private/core.toml"]
    changed = plan.changes(current, {"core": "core2", "ui": "ui1"})
    assert changed == {"core": {"from": "core1", "to": "core2",
                                "services": ["companion-api", "companion-worker"]}}
    with pytest.raises(ValueError, match="reader"):
        plan.changes(current, {"reader": "r2"})


def test_copy_folds_wal_into_the_copy_and_skips_side_files(tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    (source / "nested").mkdir(parents=True)
    db = sqlite3.connect(source / "core.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t(v)")
    db.execute("INSERT INTO t VALUES ('committed in wal')")
    db.commit()
    (source / "nested" / "journal.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
    plan.copy_tree(source, target)
    db.close()
    copy = sqlite3.connect(target / "core.sqlite3")
    assert copy.execute("SELECT v FROM t").fetchall() == [("committed in wal",)]
    copy.close()
    assert (target / "nested" / "journal.jsonl").read_text("utf-8") == '{"seq":1}\n'
    assert not any(p.name.endswith(("-wal", "-shm")) for p in target.rglob("*"))


@pytest.mark.parametrize(("closed", "expected"), [(True, "R"), (False, "NOT_READY")])
def test_closed_unknown_calls_do_not_block_a_release(closed, expected):
    value = {**probe(blockers=["MODEL_USAGE_UNSETTLED"]), "unknown_closed": closed}
    assert plan.classify(CORE, value, rolling=True)[0] == expected


def test_unknown_closure_is_read_from_the_live_journal(tmp_path):
    import json

    journal = tmp_path / "budget.jsonl"
    db = sqlite3.connect(tmp_path / "companion.sqlite3")
    db.execute("CREATE TABLE model_usage(request_id TEXT, state TEXT)")
    db.execute("CREATE TABLE model_admissions(request_id TEXT, journal_path TEXT)")
    for request_id in ("model_a", "model_b"):
        db.execute("INSERT INTO model_usage VALUES(?, 'UNKNOWN')", (request_id,))
        db.execute("INSERT INTO model_admissions VALUES(?, ?)", (request_id, str(journal)))
    db.commit()
    db.close()
    lines = [{"kind": "settle", "request_id": "model_a"},
             {"kind": "model_local_close", "request_id": "model_a"},
             {"kind": "settle", "request_id": "model_b"}]
    journal.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    api = {"mounts": [{"type": "bind", "source": str(tmp_path)}]}
    assert plan.closed_unknowns(api) is False
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "model_unknown_carry", "request_id": "model_b"}) + "\n")
    assert plan.closed_unknowns(api) is True


GOOD_EDGE = {"custody": {"state": "ACTIVE", "stop_requested": False, "unsettled_sends": {},
                         "held_inputs": 0, "pending_inputs": 0},
             "protocol": {"bridge": "1" * 64, "core": "1" * 64}}


@pytest.mark.parametrize(("change", "reason"), [
    (None, None),
    ({"custody": {**GOOD_EDGE["custody"], "state": "STOPPED"}}, "BRIDGE_CUSTODY_NOT_ACTIVE"),
    ({"custody": {**GOOD_EDGE["custody"], "stop_requested": True}},
     "BRIDGE_CUSTODY_NOT_ACTIVE"),
    ({"custody": {**GOOD_EDGE["custody"], "unsettled_sends": {"IO_STARTED": 1}}},
     "BRIDGE_SEND_UNSETTLED"),
    ({"protocol": {"bridge": "2" * 64, "core": "1" * 64}}, "BRIDGE_CORE_PROTOCOL_MISMATCH"),
    ({"protocol": {"bridge": None, "core": "1" * 64}}, "BRIDGE_CORE_PROTOCOL_MISMATCH"),
])
def test_a_bridge_only_release_needs_settled_custody_and_the_same_protocol(change, reason):
    edge = {**GOOD_EDGE, **(change or {})}
    kind, reasons = plan.classify(BRIDGE, None, edge=edge)
    assert (kind, reasons) == (("EDGE", []) if reason is None else ("NOT_READY", [reason]))


def test_a_bridge_released_with_core_still_needs_the_new_core_protocol():
    edge = {**GOOD_EDGE, "protocol": {"bridge": "2" * 64, "core": "1" * 64}}
    changed = {**CORE, **BRIDGE}
    assert plan.classify(changed, probe(), edge=edge) == (
        "NOT_READY", ["BRIDGE_CORE_PROTOCOL_MISMATCH"])
    # With STOP an unsettled Bridge custody is handed off, not blocking.
    stopped = {**GOOD_EDGE, "custody": {"state": "STOPPED"}}
    assert plan.classify(changed, probe(), edge=stopped)[0] == "A"


def test_bridge_custody_reads_the_spool_without_writing(tmp_path):
    db = sqlite3.connect(tmp_path / "spool.sqlite3")
    db.executescript(
        "CREATE TABLE inbox(key,state);CREATE TABLE owner_runtime(id,state);"
        "CREATE TABLE window_attempts(message_id,state);"
        "CREATE TABLE held_inputs(event_id,state);"
        "INSERT INTO owner_runtime VALUES(1,'ACTIVE');"
        "INSERT INTO window_attempts VALUES('m1','ACK'),('m2','IO_STARTED');"
        "INSERT INTO held_inputs VALUES('e1','HELD'),('e2','PROMOTED');"
        "INSERT INTO inbox VALUES('e3','pending');")
    db.commit()
    db.close()
    bridge = {"mounts": [{"type": "bind", "source": str(tmp_path), "rw": True}]}
    assert plan.bridge_custody(bridge) == {
        "state": "ACTIVE", "stop_requested": False, "unsettled_sends": {"IO_STARTED": 1},
        "held_inputs": 1, "pending_inputs": 1}
    (tmp_path / ("owner-" + "a" * 64 + ".stop")).write_text("OWNER_STOP\n")
    assert plan.bridge_custody(bridge)["stop_requested"] is True


def test_versions_record_revision_labels_and_read_only_config_hashes(tmp_path):
    config = tmp_path / "bridge.toml"
    config.write_bytes(b"a = 1\n")
    item = inspected("bridge", "bridge1", [
        {"Type": "bind", "Source": str(config), "Destination": "/private/bridge.toml",
         "RW": False},
        {"Type": "bind", "Source": str(tmp_path), "Destination": "/data", "RW": True}])
    item["Config"]["Labels"]["com.companion.revision"] = "833fa25f6227"
    assert plan.versions([item]) == {"bridge": {
        "image": "bridge1", "revision": {"com.companion.revision": "833fa25f6227"},
        "configs": {"/private/bridge.toml": hashlib.sha256(b"a = 1\n").hexdigest()}}}
