import socket
import ssl
import time
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from companion import observe, ops_status
from companion.contracts import CoreError
from tests.conftest import ingest, run
from tests.support.observations import records, status_server


def test_real_localhost_samples_use_wall_and_monotonic_time_without_worker_claim(core, tmp_path):
    output = tmp_path / "observation.jsonl"
    with status_server(core) as (url, requests, instance):
        collect = observe.http_collector(core.settings, url)
        started = time.monotonic_ns()
        summary = observe.observe(collect, output, samples=3, interval_seconds=0.02,
                                  max_gap_seconds=5)
        elapsed = (time.monotonic_ns() - started) / 1e9
    data = records(output)
    samples = data[1:-1]
    assert summary["failures"] == summary["discontinuities"] == 0
    assert summary["worker_health_proven"] is False
    assert 0 < summary["clear_segment_observed_seconds"] <= elapsed < 10
    assert data[0]["evidence_type"] == "local_synthetic"
    assert len({row["run_id"] for row in data}) == 1
    assert all(row["segment"] == 1 and row["api_instance"] == instance[0] for row in samples)
    assert all("+00:00" in row["observed_at"] for row in samples)
    assert len(requests) == 3
    assert all(path == "/internal/v1/ops/status?summary_only=true" for path, _, _ in requests)
    assert all(token == "Bearer " + core.settings.bridge_token.get_secret_value()
               for _, token, _ in requests)
    assert url.removeprefix("http://") == requests[0][2]
    text = output.read_text("utf-8")
    assert url not in text and core.settings.bridge_token.get_secret_value() not in text
    assert core.settings.database.get_secret_value() not in text


def test_remote_missing_schema_head_is_an_observed_blocker_not_a_clear_sample(
    core, tmp_path, monkeypatch,
):
    def unavailable():
        raise CoreError("SCHEMA_HEAD_UNAVAILABLE", 503)

    monkeypatch.setattr(ops_status, "schema_head", unavailable)
    output = tmp_path / "blocked-observation.jsonl"
    with status_server(core) as (url, _, _):
        result = observe.observe(observe.http_collector(core.settings, url), output,
                                 samples=1, interval_seconds=0.01)
    sample = records(output)[1]
    assert sample["snapshot"]["blockers"] == ["SCHEMA_HEAD_UNAVAILABLE"]
    assert result["failures"] == 0
    assert result["clear_segment_observed_seconds"] == 0
    assert sample["segment"] is None


def test_failure_restart_and_long_gap_split_segments_not_continuous_health(tmp_path):
    number = 0

    def collect():
        nonlocal number
        number += 1
        if number == 2:
            raise RuntimeError("private response body")
        if number == 4:
            time.sleep(0.08)
        return {"blockers": []}, ("a" if number < 4 else "b") * 32

    output = tmp_path / "gaps.jsonl"
    result = observe.observe(collect, output, samples=5, interval_seconds=0.01,
                             max_gap_seconds=0.05)
    data = records(output)[1:-1]
    assert data[1]["error"] == "SAMPLE_FAILED" and data[1]["segment"] is None
    assert data[2]["segment"] != data[0]["segment"]
    assert "API_INSTANCE_CHANGED" in data[3]["discontinuities"]
    assert "SAMPLE_GAP" in data[4]["discontinuities"]
    assert result["failures"] == 1 and result["clear_segment_observed_seconds"] == 0
    assert "private response body" not in output.read_text("utf-8")


def test_observed_blockers_never_count_toward_clear_segment(tmp_path):
    output = tmp_path / "blocked.jsonl"
    result = observe.observe(lambda: ({"blockers": ["MODEL_USAGE_UNSETTLED"]}, None), output,
                             samples=2, interval_seconds=0.001)
    assert result["clear_segment_observed_seconds"] == 0
    assert all(row["segment"] is None for row in records(output)[1:-1])
    assert all(row["restart_detection"] == "UNAVAILABLE" for row in records(output)[1:-1])


def test_new_invocations_cannot_append_or_join_evidence(tmp_path):
    first, second = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    def collect():
        return {"blockers": []}, None

    one = observe.observe(collect, first, samples=1)
    with pytest.raises(FileExistsError):
        observe.observe(collect, first, samples=1)
    two = observe.observe(collect, second, samples=1)
    assert one["run_id"] != two["run_id"]
    assert one["clear_segment_observed_seconds"] == two["clear_segment_observed_seconds"] == 0


def test_nonloopback_ip_and_external_without_authorization_make_no_http(core, monkeypatch):
    calls = []
    monkeypatch.setattr(observe.httpx, "AsyncClient", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match="LOOPBACK"):
        observe.http_collector(core.settings, "http://192.168.1.5:8765")()
    with pytest.raises(ValueError, match="AUTHORIZATION"):
        observe.http_collector(core.settings, "https://192.168.1.5", allow_external=True)
    assert calls == []


@pytest.mark.parametrize("url", ["https://user:secret@host", "http://localhost?key=secret",
                                "http://localhost/other", "file:///private"])
def test_http_collector_rejects_non_origin_urls(core, url):
    with pytest.raises(ValueError):
        observe.http_collector(core.settings, url)


def test_http_failure_is_a_recorded_failure_not_api_or_worker_health(core, tmp_path):
    with status_server(core, fail=True) as (url, requests, _):
        output = tmp_path / "failed.jsonl"
        result = observe.observe(observe.http_collector(core.settings, url), output, samples=1)
    assert len(requests) == result["failures"] == 1
    assert records(output)[1]["snapshot"] is None
    assert result["worker_health_proven"] is False


def test_stale_http_snapshot_is_a_failure_not_a_continuous_interval(core, tmp_path):
    with status_server(core, stale=True) as (url, requests, _):
        output = tmp_path / "stale.jsonl"
        result = observe.observe(observe.http_collector(core.settings, url), output, samples=1)
    assert len(requests) == result["failures"] == 1
    assert result["clear_segment_observed_seconds"] == 0


def test_runtime_evidence_requires_explicit_authorization_reference(tmp_path):
    with pytest.raises(ValueError, match="AUTHORIZATION"):
        observe.observe(lambda: ({"blockers": []}, None), tmp_path / "runtime.jsonl",
                        samples=1, evidence_type="authorized_runtime")
    assert not (tmp_path / "runtime.jsonl").exists()


def test_wall_clock_jump_is_a_gap_and_does_not_add_fictional_duration(tmp_path, monkeypatch):
    calls = 0

    class JumpingClock(datetime):
        @classmethod
        def now(cls, timezone):
            nonlocal calls
            calls += 1
            return datetime.now(UTC) + timedelta(seconds=10 if calls >= 3 else 0)

    monkeypatch.setattr(observe, "datetime", JumpingClock)
    output = tmp_path / "clock.jsonl"
    result = observe.observe(lambda: ({"blockers": []}, None), output,
                             samples=3, interval_seconds=0.001)
    data = records(output)[1:-1]
    assert "WALL_CLOCK_DISCONTINUITY" in data[1]["discontinuities"]
    assert data[1]["segment"] is None and data[2]["segment"] != data[0]["segment"]
    assert result["clear_segment_observed_seconds"] == 0


def test_slow_last_collection_is_not_an_unobserved_clear_interval(tmp_path):
    def collect():
        time.sleep(0.03)
        return {"blockers": []}, None

    output = tmp_path / "slow.jsonl"
    result = observe.observe(collect, output, samples=1, interval_seconds=0.001,
                             max_gap_seconds=0.01)
    assert records(output)[1]["discontinuities"] == ["COLLECTION_GAP"]
    assert result["clear_segment_observed_seconds"] == 0


def test_interrupted_observation_leaves_explicitly_incomplete_evidence(tmp_path):
    def collect():
        raise KeyboardInterrupt

    output = tmp_path / "interrupted.jsonl"
    with pytest.raises(KeyboardInterrupt):
        observe.observe(collect, output, samples=1)
    assert [row["kind"] for row in records(output)] == ["observation_start"]


@pytest.mark.parametrize("options", [{"samples": 0}, {"samples": 100001},
                                    {"interval_seconds": float("nan")},
                                    {"interval_seconds": 0}, {"max_gap_seconds": 0},
                                    {"authorization_sha256": "private raw string"}])
def test_invalid_bounds_do_not_create_evidence(tmp_path, options):
    output = tmp_path / "invalid.jsonl"
    with pytest.raises(ValueError):
        observe.observe(lambda: ({"blockers": []}, None), output, **options)
    assert not output.exists()


def test_http_snapshot_only_persists_known_safe_fields(core, tmp_path):
    marker = "SYNTHETIC_PRIVATE_MESSAGE_ENDPOINT_KEY_PATH"

    def mutate(snapshot):
        snapshot["raw_message"] = marker
        snapshot["endpoint"] = "http://SYNTHETIC_PRIVATE_ENDPOINT/"
        snapshot["database"]["raw_path"] = marker
        snapshot["resources"]["private_resource"] = marker
        snapshot["jobs"]["private_detail"] = marker

    output = tmp_path / "projected.jsonl"
    with status_server(core, mutate=mutate) as (url, _, _):
        summary = observe.observe(observe.http_collector(core.settings, url), output, samples=1)
    assert summary["failures"] == 0
    text = output.read_text("utf-8")
    assert marker not in text and "SYNTHETIC_PRIVATE_ENDPOINT" not in text
    assert records(output)[1]["snapshot"]["jobs"]["active"] == 0


@pytest.mark.parametrize("change", ["private_blocker", "negative_queue", "boolean_count",
                                   "unknown_state", "infinite_age", "negative_budget",
                                   "calibration_type"])
def test_invalid_remote_aggregate_is_a_failure_without_snapshot(core, tmp_path, change):
    marker = "SYNTHETIC_PRIVATE_MESSAGE_ENDPOINT_KEY_PATH"

    def mutate(snapshot):
        if change == "private_blocker":
            snapshot["blockers"] = [marker]
        elif change == "negative_queue":
            snapshot["jobs"]["active"] = -123
        elif change == "boolean_count":
            snapshot["jobs"]["active"] = True
        elif change == "unknown_state":
            snapshot["jobs"]["states"] = {marker: 1}
        elif change == "infinite_age":
            snapshot["jobs"]["oldest_age_seconds"] = float("inf")
        elif change == "negative_budget":
            snapshot["budget"]["unsettled_calls"] = -1
        else:
            snapshot["budget"]["calibration"]["calibrated_calls"] = marker

    output = tmp_path / "rejected.jsonl"
    with status_server(core, mutate=mutate) as (url, _, _):
        summary = observe.observe(observe.http_collector(core.settings, url), output, samples=1)
    assert summary["failures"] == 1
    assert records(output)[1]["snapshot"] is None
    assert marker not in output.read_text("utf-8")


def test_http_total_deadline_cancels_slow_drip_before_entire_body(core, tmp_path):
    output = tmp_path / "drip.jsonl"
    with status_server(core, delays=(2.1, 2.1, 2.1)) as (url, _, _):
        started = time.monotonic()
        summary = observe.observe(observe.http_collector(core.settings, url), output, samples=1)
        elapsed = time.monotonic() - started
    assert summary["failures"] == 1
    assert elapsed < 5.8
    assert records(output)[1]["snapshot"] is None


def test_remote_freshness_is_checked_at_receive_completion(core, tmp_path):
    def mutate(snapshot):
        snapshot["observed_at"] = (datetime.now(UTC) - timedelta(seconds=4.8)).isoformat()

    output = tmp_path / "aged-during-read.jsonl"
    with status_server(core, mutate=mutate, delays=(0.6,)) as (url, _, _):
        summary = observe.observe(observe.http_collector(core.settings, url), output, samples=1)
    assert summary["failures"] == 1
    assert records(output)[1]["snapshot"] is None


@pytest.mark.parametrize("ipv6", [False, True])
def test_literal_loopback_never_needs_dns(core, monkeypatch, ipv6):
    with status_server(core, ipv6=ipv6) as (url, requests, _):
        def forbidden(*_args, **_kwargs):
            pytest.fail("IP literal collection must not dispatch DNS work")

        monkeypatch.setattr(socket, "getaddrinfo", forbidden)
        result, _ = observe.http_collector(core.settings, url)()
    assert result["jobs"]["active"] == 0 and len(requests) == 1
    assert requests[0][2] == url.removeprefix("http://")


@pytest.mark.parametrize("url", ["http://localhost:8765", "https://synthetic.invalid",
                                "http://127.1", "http://2130706433", "http://[::1%lo]"])
def test_domains_and_ambiguous_hosts_are_rejected_without_http(core, monkeypatch, url):
    calls = []
    monkeypatch.setattr(observe.httpx, "AsyncClient", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match="IP_LITERAL"):
        observe.http_collector(core.settings, url)
    assert calls == []


@pytest.mark.parametrize("matches", [False, True])
def test_https_requires_trusted_certificate_with_matching_ip_san(
    core, tmp_path, monkeypatch, matches,
):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-observer")])
    now = datetime.now(UTC)
    san = (x509.IPAddress(ip_address("127.0.0.1")) if matches
           else x509.DNSName("synthetic.invalid"))
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path / "certificate.pem", tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(certfile, keyfile)
    client_tls = ssl.create_default_context(cafile=str(certfile))
    assert client_tls.check_hostname and client_tls.verify_mode == ssl.CERT_REQUIRED
    monkeypatch.setattr(observe.ssl, "create_default_context", lambda: client_tls)
    with status_server(core, tls=server_tls) as (url, requests, _):
        output = tmp_path / "tls.jsonl"
        summary = observe.observe(observe.http_collector(core.settings, url), output, samples=1)
    assert summary["failures"] == (0 if matches else 1)
    assert len(requests) == (1 if matches else 0)


def test_valid_remote_unknown_counts_are_observed_as_blocked_not_rejected(core, tmp_path):
    ingest(core)
    assert run(core.run_once("synthetic-ops-worker"))
    core.store.execute("UPDATE outbox SET state='DELIVERY_UNCERTAIN'")
    core.store.execute("UPDATE model_usage SET state='UNKNOWN'")
    output = tmp_path / "known-blocked.jsonl"
    with status_server(core) as (url, _, _):
        summary = observe.observe(observe.http_collector(core.settings, url), output, samples=1)
    assert summary["failures"] == 0 and summary["discontinuities"] == 1
    snapshot = records(output)[1]["snapshot"]
    assert snapshot["budget"]["unknown_calls"] == snapshot["budget"]["unsettled_calls"] == 1
    assert snapshot["deliveries"]["states"] == {"DELIVERY_UNCERTAIN": 1}
    assert "MODEL_USAGE_UNSETTLED" in snapshot["blockers"]
