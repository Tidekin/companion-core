"""Owner-approved device credentials, independent of the paid execution authority."""

import json
from datetime import datetime

from companion.contracts import CoreError
from companion.owner_authority_protocol import CaptureIdentity
from companion.store import encode, uid
from companion_delivery_protocol import digest
from companion_delivery_protocol.native_chat import device_identity, source_identity

# One-time migration/bootstrap of the currently approved identities. Archived
# policy bytes remain untouched: old capture signatures still name those bytes.
SEED_LEGACY = """
WITH current_policy AS (
 SELECT r.* FROM owner_authority_revisions r JOIN runtime_meta p
 ON p.key='owner_authority_current' AND p.value=r.record_sha256
 WHERE r.policy_redacted=0
), identities AS (
 SELECT r.grant_id,r.restore_generation,r.created_at,d.key AS device_id,d.value AS identity
 FROM current_policy r CROSS JOIN LATERAL
 jsonb_each(COALESCE(r.policy_json::jsonb->'native_devices','{}'::jsonb)) d
), seeded AS (
 INSERT INTO native_device_revisions
 (revision_id,device_id,version,state,identity_json,grant_id,restore_generation,operation_id,created_at)
 SELECT 'ndev_'||md5(grant_id||restore_generation||device_id),device_id,1,'ACTIVE',identity::text,
 grant_id,restore_generation,'legacy_'||md5(grant_id||restore_generation||device_id),created_at
 FROM identities WHERE NOT EXISTS
 (SELECT 1 FROM native_devices n WHERE n.device_id=identities.device_id)
 ON CONFLICT DO NOTHING RETURNING device_id,revision_id
)
INSERT INTO native_devices(device_id,revision_id)
SELECT device_id,revision_id FROM seeded ON CONFLICT DO NOTHING
"""


def seed_legacy(store):
    store.execute(SEED_LEGACY)


def _row(store, device_id):
    return store.one("SELECT r.* FROM native_devices d JOIN native_device_revisions r "
                     "USING(revision_id) WHERE d.device_id=%s", (device_id,))


def _scope(row, view):
    return bool(row and row['grant_id'] == view.authority.grant_id
                and row['restore_generation'] == view.authority.restore_generation)


def active(store, device_id, view):
    row = _row(store, device_id)
    return row if _scope(row, view) and row['state'] == 'ACTIVE' else None


def current_identity(store, device_id, view):
    row = active(store, device_id, view)
    if not row:
        raise CoreError('NATIVE_DEVICE_APPROVAL_REQUIRED', 403)
    return CaptureIdentity.model_validate_json(row['identity_json'])


def historical_identity(store, capture, view):
    device_id = source_identity(capture.source_event_id)[0]
    # Historical legacy evidence is read with its original immutable policy.
    if capture.protocol_epoch == 'native-v1':
        return view.policy.native_devices.get(device_id)
    row = store.one("SELECT identity_json,created_at FROM native_device_revisions "
        "WHERE device_id=%s AND grant_id=%s AND restore_generation=%s AND state='ACTIVE' "
        "AND identity_json::jsonb->>'protocol_epoch'=%s LIMIT 1", (device_id,
        view.authority.grant_id, view.authority.restore_generation, capture.protocol_epoch))
    if not row or capture.occurred_at < datetime.fromisoformat(row['created_at']):
        return None
    return CaptureIdentity.model_validate_json(row['identity_json'])


def require_capture(store, capture, view):
    identity = current_identity(store, source_identity(capture.source_event_id)[0], view)
    if (capture.identity_sha256, capture.protocol_epoch) != (
            identity.identity_sha256, identity.protocol_epoch):
        raise CoreError('NATIVE_DEVICE_REGISTRATION_CHANGED', 403)


def _registered(row, view):
    return {'registered': True, 'device_id': row['device_id'],
            'device_revision': row['version'], 'capture_identity': json.loads(row['identity_json']),
            'authority': view.authority.as_dict()}


def preview(context, public_key):
    from companion import owner_authority

    device_id, _ = device_identity(public_key)
    view = owner_authority.current(context.store, context.settings, context.clock, purpose='chat')
    row = _row(context.store, device_id)
    if _scope(row, view) and row['state'] == 'ACTIVE':
        return _registered(row, view)
    if row and context.session['created_at'] <= row['created_at']:
        raise CoreError('NATIVE_DEVICE_RELOGIN_REQUIRED', 403)
    count = context.store.one("SELECT count(*) AS n FROM native_devices d "
        "JOIN native_device_revisions r USING(revision_id) WHERE r.state='ACTIVE' "
        "AND r.grant_id=%s AND r.restore_generation=%s",
        (view.authority.grant_id, view.authority.restore_generation))['n']
    if count >= 16:
        raise CoreError('NATIVE_DEVICE_LIMIT', 409)
    version = row['version'] if row else 0
    material = {'device_id': device_id, 'public_key': public_key, 'device_revision': version,
                'authority': view.authority.as_dict(), 'session': context.session['token_sha256']}
    return {'registered': False, 'device_id': device_id,
            'expected_revision': view.authority.revision, 'device_revision': version,
            'preview_sha256': digest(material)}


def authorize(context, body):
    from companion import owner_authority

    store = context.store
    with store.transaction(control_write=True):
        store.execute('SELECT pg_advisory_xact_lock(1129271877,1313097046)')
        value = preview(context, body.public_key)
        if value['registered']:
            return value
        if (body.expected_revision, body.preview_sha256) != (
                value['expected_revision'], value['preview_sha256']):
            raise CoreError('NATIVE_DEVICE_PREVIEW_CHANGED', 409)
        view = owner_authority.current(store, context.settings, context.clock, purpose='chat')
        revision_id = uid('ndev')
        epoch = 'native-v2-' + revision_id.removeprefix('ndev_')
        device_id, identity = device_identity(body.public_key)
        identity.update(protocol_epoch=epoch,
            identity_sha256=digest(['native.chat', device_id, body.public_key, epoch]))
        version = value['device_revision'] + 1
        store.insert('native_device_revisions', revision_id=revision_id, device_id=device_id,
            version=version, state='ACTIVE', identity_json=encode(identity),
            grant_id=view.authority.grant_id, restore_generation=view.authority.restore_generation,
            operation_id='enroll_'+context.session['token_sha256'], created_at=context.clock())
        store.execute("INSERT INTO native_devices(device_id,revision_id) VALUES (%s,%s) "
            "ON CONFLICT(device_id) DO UPDATE SET revision_id=EXCLUDED.revision_id",
            (device_id, revision_id))
        store.audit('native_device.enrolled', device_id, revision_id, context.clock(),
                    version=version)
        return _registered(_row(store, device_id), view)


def revoke(context, device_id):
    from companion import job_queue, owner_authority, transport

    store = context.store
    with store.transaction(control_write=True):
        store.execute('SELECT pg_advisory_xact_lock(1129271877,1313097046)')
        view = owner_authority.current(store, context.settings, context.clock, purpose='chat')
        row = _row(store, device_id)
        if not _scope(row, view):
            raise CoreError('NATIVE_DEVICE_NOT_FOUND', 404)
        if row['state'] == 'REVOKED':
            return {'device_id': device_id, 'state': 'REVOKED', 'version': row['version']}
        revision_id, now = uid('ndev'), context.clock()
        store.insert('native_device_revisions', revision_id=revision_id, device_id=device_id,
            version=row['version']+1, state='REVOKED', identity_json=row['identity_json'],
            grant_id=row['grant_id'], restore_generation=row['restore_generation'],
            operation_id='revoke_'+revision_id, created_at=now)
        store.execute('UPDATE native_devices SET revision_id=%s WHERE device_id=%s',
                      (revision_id, device_id))
        sources = ("SELECT event_id FROM events WHERE source='native.chat' "
                   "AND source_event_id LIKE %s")
        prefix = 'native:'+device_id+':%'
        job_queue.cancel_active(store, 'event_id IN ('+sources+')', (prefix,),
                                'NATIVE_DEVICE_REVOKED', now)
        transport.revoke_deliveries(store, 'origin_event_id IN ('+sources+
            ") AND state IN ('PENDING','LEASED','SENDING','RETRY_WAIT')", (prefix,),
                                    reason='NATIVE_DEVICE_REVOKED', now=now)
        store.audit('native_device.revoked', device_id, revision_id, now, version=row['version']+1)
        return {'device_id': device_id, 'state': 'REVOKED', 'version': row['version']+1}
