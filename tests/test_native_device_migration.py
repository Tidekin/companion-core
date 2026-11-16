"""The real schema upgrade seeds existing credentials without changing archived grants."""

import json

import psycopg

from companion.postgres import migrate
from companion_delivery_protocol.native_chat import device_identity
from tests.postgres_fixture import isolated_database


def test_upgrade_seeds_current_devices_and_preserves_exact_legacy_policy():
    devices = dict(device_identity(key) for key in ('1'*64, '2'*64))
    policy = json.dumps({'native_devices': devices}, ensure_ascii=False, indent=3)
    with isolated_database() as info:
        migrate(info, revision='pg0010_care_plans')
        with psycopg.connect(info.get_secret_value()) as db:
            db.execute("INSERT INTO owner_authority_revisions (grant_id,revision,operation_id,"
                "request_sha256,state,policy_json,policy_sha256,restore_generation,record_sha256,"
                "journal_id,journal_path,created_at) VALUES ('original',7,'original','request',"
                "'ACTIVE',%s,'original-policy','initial','original-record','journal','original',"
                "'2026-11-15T00:00:00+00:00')", (policy,))
            db.execute("INSERT INTO runtime_meta(key,value) VALUES "
                       "('owner_authority_current','original-record')")
            before = db.execute('SELECT * FROM owner_authority_revisions').fetchall()
        migrate(info)
        with psycopg.connect(info.get_secret_value()) as db:
            assert db.execute('SELECT * FROM owner_authority_revisions').fetchall() == before
            rows = db.execute('SELECT d.device_id,r.identity_json,r.state,r.version '
                              'FROM native_devices d JOIN native_device_revisions r '
                              'USING(revision_id)').fetchall()
            assert {key: json.loads(identity) for key, identity, _, _ in rows} == devices
            assert all((state, version) == ('ACTIVE', 1) for _, _, state, version in rows)
        migrate(info)
        with psycopg.connect(info.get_secret_value()) as db:
            assert db.execute('SELECT count(*) FROM native_device_revisions').fetchone() == (2,)
