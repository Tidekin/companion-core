"""Images attached in the native client are signed by hash and reach the chat model."""

import base64
import io
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image

from companion import owner_authority, paid_budget
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion.store import after
from tests.conftest import run
from tests.support.live_image_input import image_parts, options
from tests.support.native_chat import enroll, packet
from tests.support.owner_authority import activate
from tests.support.owner_console import client as client
from tests.support.paid_model import approved_live


def photo(color="blue", size=(8, 6), fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format=fmt)
    return out.getvalue()


def authorize(core, tmp_path):
    scope = options()
    for name, value in scope["core_options"].items():
        setattr(core.settings, name, value)
    approved_live(core, tmp_path, **scope["paid_options"])
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    core.settings.rich_input_scope = "references_v1"
    key = Ed25519PrivateKey.generate()
    policy = OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id,
        purposes=["chat", "content", "history", "image"], authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies={"image": owner_authority.capability_hashes(core.settings)["image"]},
        history={"scope": "owner_private", "not_before": after(core.clock(), -3600),
                 "max_turns": 16, "max_utf8_bytes": 64000},
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": paid_budget.sha([core.settings.owner_qq_id,
                                                     core.settings.bot_qq_id]),
                 "protocol_epoch": "native-image-test"})
    activate(core, policy)


def test_a_native_image_reaches_the_model_and_its_preview_follows_the_message(
    client, core, tmp_path,
):
    from tests.sdk_reader_fixture import answer, response, worker_for

    authorize(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        return response(answer('这只猫好圆。'), stream=requests[-1].get('stream'))

    original = photo()

    async def scenario():
        async with worker_for(core, provider) as worker:
            sent = client.post('/v1/console/chat/messages', headers=headers,
                               json=packet(core, key, authority, '你看',
                                           images=[('image/png', original)]))
            assert sent.status_code == 202, sent.text
            event_id, job_id = sent.json()['event_id'], sent.json()['job_id']
            assert await worker.run_once('native-image', job_id=job_id)
            assert image_parts(requests[-1]), 'the model saw no image'
            turn = client.get('/v1/console/chat/messages/' + event_id).json()['items'][0]
            assert turn['images'] == [{'slot': 0}]
            assert turn['replies'][0]['text'] == '这只猫好圆。'
            preview = client.get(f'/v1/console/chat/messages/{event_id}/images/0',
                                 headers=headers)
            assert preview.status_code == 200
            assert preview.headers['content-type'] == 'image/jpeg'
            with Image.open(io.BytesIO(preview.content)) as image:
                assert image.size == (8, 6) and not image.info.get('exif')
            assert client.get(f'/v1/console/chat/messages/{event_id}/images/1',
                              headers=headers).status_code == 404
            return event_id

    event_id = run(scenario())
    from companion.content import erase_content

    erase_content(core.store, [event_id])
    from companion import native_images

    assert not list(native_images.directory(core.store).glob(event_id + '.*'))


def test_image_bytes_must_match_the_signed_hash_and_type(client, core, tmp_path):
    authorize(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    signed = packet(core, key, authority, '你看', images=[('image/png', photo())])
    swapped = {**signed, 'images': [{'mime_type': 'image/png', 'data_base64':
                                     base64.b64encode(photo('red')).decode()}]}
    result = client.post('/v1/console/chat/messages', headers=headers, json=swapped)
    assert result.status_code == 403 and 'NATIVE_INPUT_CHANGED' in result.text
    lying = packet(core, key, authority, '你看', images=[('image/png', photo(fmt='JPEG'))])
    result = client.post('/v1/console/chat/messages', headers=headers, json=lying)
    assert result.status_code == 400 and 'NATIVE_IMAGE_INVALID' in result.text
    unsigned = {**packet(core, key, authority, '你看'),
                'images': signed['images']}
    result = client.post('/v1/console/chat/messages', headers=headers, json=unsigned)
    assert result.status_code == 403
    image_only = packet(core, key, authority, '', images=[('image/png', photo())])
    assert client.post('/v1/console/chat/messages', headers=headers,
                       json=image_only).status_code == 202
