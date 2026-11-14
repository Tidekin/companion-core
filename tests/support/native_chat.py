"""Signed native device API setup without importing a business test suite."""

import base64
import hashlib
import uuid

from companion_delivery_protocol.authority import capture_signing_payload
from companion_delivery_protocol.native_chat import capture
from tests.support.owner_console import ORIGIN, PASSWORD


def enroll(client, key):
    public_key = key.public_key().public_bytes_raw().hex()
    login = client.post('/v1/console/session', json={
        'password': PASSWORD, 'device_public_key': public_key}, headers={'Origin': ORIGIN})
    assert login.status_code == 200, login.text
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': login.json()['csrf_token'],
               'Idempotency-Key': 'enroll-native-device'}
    body = {'public_key': public_key}
    preview = client.post('/v1/console/chat/devices/preview', json=body, headers=headers)
    assert preview.status_code == 200, preview.text
    value = preview.json()
    if not value['registered']:
        result = client.post('/v1/console/chat/devices/authorize', headers=headers,
            json={**body, **{name: value[name]
                            for name in ('expected_revision', 'preview_sha256')}})
        assert result.status_code == 200, result.text
        value = result.json()
    assert value['registered'], value
    return headers, value['authority']


def packet(core, key, authority, text='原生文本输入', request_id=None, images=()):
    """images: (mime, bytes) pairs, signed by hash inside the message projection."""
    from companion.native_images import input_sha256

    value = capture(key.public_key().public_bytes_raw().hex(), authority,
                    request_id or uuid.uuid4().hex, text, core.clock())
    value['input_sha256'] = input_sha256(
        text, [hashlib.sha256(data).hexdigest() for _, data in images])
    value['signature'] = key.sign(capture_signing_payload(value)).hex()
    result = {'capture': value, 'text': text}
    if images:
        result['images'] = [{'mime_type': mime, 'data_base64': base64.b64encode(data).decode()}
                            for mime, data in images]
    return result
