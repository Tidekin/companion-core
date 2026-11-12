"""Owner-uploaded avatars: checked, re-encoded once, chosen atomically, expired safely."""

import base64
import io
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image

from companion import companion_avatar as avatar
from companion import companion_profile as profile
from companion.contracts import CoreError
from companion.profile_config import ProfileConfig
from tests.support.native_chat import enroll
from tests.support.owner_console import client as client
from tests.support.owner_input import captured


def image(size=(800, 600), fmt="JPEG", *, exif=True):
    picture = Image.new("RGB", size, (200, 120, 80))
    out = io.BytesIO()
    extra = {}
    if exif:
        data = Image.Exif()
        data[0x010F] = "SecretCamera"  # Make
        data[0x0112] = 6  # Orientation: rotate 90° on display
        extra["exif"] = data.tobytes()
    picture.save(out, format=fmt, **extra)
    return out.getvalue()


def upload(core, data=None, mime="image/jpeg", operation="op-avatar-1"):
    with core.store.transaction():
        return avatar.create(core.store, core.clock(), data=data or image(), mime=mime,
                             operation_id=operation)


def companion_update(core, revision, *, name=None, asset=None, call="call-1"):
    with core.store.transaction():
        return profile.update_by_companion(
            core.store, core.settings, core.clock(), display_name=name,
            avatar_asset_id=asset, expected_revision=revision, note=None, job_id="job-1",
            tool_call_id=call)


def test_processing_crops_reencodes_and_strips_metadata():
    png, width, height = avatar.process(image(), "image/jpeg")
    with Image.open(io.BytesIO(png)) as result:
        assert result.format == "PNG" and result.size == (512, 512) == (width, height)
        assert not result.getexif() and "exif" not in result.info
    assert b"SecretCamera" not in png


@pytest.mark.parametrize(("data", "mime", "reason"), [
    (b"", "image/png", "AVATAR_EMPTY"),
    (b"x" * (avatar.MAX_UPLOAD_BYTES + 1), "image/png", "AVATAR_TOO_LARGE"),
    (b"not an image at all", "image/png", "AVATAR_IMAGE_INVALID"),
    (None, "image/gif", "AVATAR_FORMAT_UNSUPPORTED"),
    # The declared type must be the decoded type.
    (None, "image/png", "AVATAR_IMAGE_INVALID"),
], ids=["empty", "too-large", "garbage", "gif", "mime-mismatch"])
def test_processing_fails_closed(data, mime, reason):
    with pytest.raises(ValueError, match=reason):
        avatar.process(image() if data is None else data, mime)


def test_rejected_upload_stores_nothing(core):
    with pytest.raises(CoreError, match="AVATAR_IMAGE_INVALID"):
        upload(core, data=b"\x89PNG broken", mime="image/png")
    assert core.store.rows("SELECT * FROM companion_avatar_assets") == []
    root = avatar.directory(core.store)
    assert not root.exists() or list(root.iterdir()) == []


def test_upload_is_idempotent_and_bytes_are_verified(core):
    first = upload(core)
    assert avatar.ASSET_ID.fullmatch(first["asset_id"]) and first["state"] == "READY"
    assert upload(core)["asset_id"] == first["asset_id"]
    assert len(core.store.rows("SELECT 1 FROM companion_avatar_assets")) == 1
    data = avatar.read_bytes(core.store, first["asset_id"])
    assert data.startswith(b"\x89PNG")
    # A changed file is never served as the recorded asset.
    avatar._path(core.store, first["asset_id"]).write_bytes(data + b"tampered")
    with pytest.raises(CoreError, match="PROFILE_ASSET_UNAVAILABLE"):
        avatar.read_bytes(core.store, first["asset_id"])
    with pytest.raises(CoreError, match="PROFILE_ASSET_UNAVAILABLE"):
        avatar.read_bytes(core.store, "../../etc/passwd")


def test_companion_changes_name_and_avatar_in_one_revision(core):
    core.settings.profile = ProfileConfig(enabled=True, name_cooldown_seconds=0)
    asset = upload(core)["asset_id"]
    assert companion_update(core, 0, asset=asset)["status"] == "revision_conflict"
    missing = companion_update(core, 1, name="小满", asset="avt_" + "0" * 32, call="c-miss")
    assert missing["status"] == "asset_not_ready"
    assert profile.current(core.store, core.settings)["display_name"] == "伙伴"
    both = companion_update(core, 1, name="小满", asset=asset, call="c-both")
    assert both["status"] == "confirmed" and both["profile"] == {
        "revision": 2, "display_name": "小满", "avatar_asset_id": asset}
    change = profile.changes(core.store)[0]
    assert (change["fields"], change["old_avatar_asset_id"], change["new_avatar_asset_id"]) == (
        "both", None, asset)
    # Avatar quota is separate from the name quota and also fails without writing.
    other = upload(core, operation="op-avatar-2")["asset_id"]
    limited = companion_update(core, 2, asset=other, call="c-again")
    assert limited["status"] == "rate_limited" and limited["field"] == "avatar"
    assert companion_update(core, 2, call="c-empty")["reason"] == "NOTHING_REQUESTED"


def test_owner_switch_blocks_only_the_companion_avatar(core):
    core.settings.profile = ProfileConfig(enabled=True)
    asset = upload(core)["asset_id"]
    with core.store.transaction():
        profile.update_settings(core.store, core.settings, core.clock(),
                                expected_settings_revision=0, auto_avatar=False)
    blocked = companion_update(core, 1, asset=asset)
    assert blocked["status"] == "disabled_by_user" and blocked["field"] == "avatar"
    with core.store.transaction():
        result = profile.set_avatar(core.store, core.settings, core.clock(), asset_id=asset,
                                    expected_revision=1, operation_id="owner-avatar-1")
    assert result["avatar_asset_id"] == asset and result["revision"] == 2
    assert profile.changes(core.store)[0]["actor_type"] == "owner"


def test_cleanup_keeps_the_current_avatar_and_restore_fails_closed(core):
    core.settings.profile = ProfileConfig(enabled=True)
    old = upload(core)["asset_id"]
    with core.store.transaction():
        profile.set_avatar(core.store, core.settings, core.clock(), asset_id=old,
                           expected_revision=0, operation_id="owner-avatar-1")
    old_change = profile.changes(core.store)[0]["change_id"]
    new = upload(core, operation="op-avatar-2")["asset_id"]
    with core.store.transaction():
        profile.set_avatar(core.store, core.settings, core.clock(), asset_id=new,
                           expected_revision=2, operation_id="owner-avatar-2")
    later = (datetime.fromisoformat(core.clock()) + timedelta(days=181)).isoformat()
    with core.store.transaction():
        assert avatar.cleanup(core.store, later) == 1
    assert avatar.ready(core.store, new) and avatar.ready(core.store, old) is None
    assert not avatar._path(core.store, old).exists()
    # History stays readable; restoring the erased image is refused, never half-applied.
    with pytest.raises(CoreError, match="PROFILE_ASSET_UNAVAILABLE"), core.store.transaction():
        profile.restore(core.store, core.settings, core.clock(), change_id=old_change,
                        expected_revision=3, pause_name=False, operation_id="restore-avatar")
    assert profile.current(core.store, core.settings)["avatar_asset_id"] == new


def test_a_real_photo_sized_upload_passes_the_api_body_limit(client, core, tmp_path):
    """A detailed photo is far above the default 128 KiB API body limit."""
    import os

    captured(core, tmp_path)
    headers, _ = enroll(client, Ed25519PrivateKey.generate())
    noise = Image.frombytes("RGB", (1024, 768), os.urandom(1024 * 768 * 3))
    out = io.BytesIO()
    noise.save(out, format="PNG")
    data = out.getvalue()
    assert 1_000_000 < len(data) < avatar.MAX_UPLOAD_BYTES
    response = client.post("/v1/console/chat/profile/avatars", headers=headers, json={
        "mime_type": "image/png", "data_base64": base64.b64encode(data).decode(),
        "operation_id": "upload-photo-1", "expected_revision": 0})
    assert response.status_code == 200, response.text
    oversized = client.post("/v1/console/chat/profile/avatars", headers=headers, json={
        "mime_type": "image/png", "operation_id": "upload-photo-2",
        "data_base64": "A" * (8 * 1024 * 1024)})
    assert oversized.status_code == 413


def test_native_routes_upload_set_and_serve_the_avatar(client, core, tmp_path):
    captured(core, tmp_path)
    headers, _ = enroll(client, Ed25519PrivateKey.generate())
    base = "/v1/console/chat/profile"
    body = {"mime_type": "image/jpeg", "data_base64": base64.b64encode(image()).decode(),
            "operation_id": "upload-op-1", "expected_revision": 5}
    assert client.post(base + "/avatars", json=body).status_code == 403
    conflict = client.post(base + "/avatars", json=body, headers=headers)
    assert conflict.status_code == 409, conflict.text
    # The failed transaction stored neither the asset row nor a revision.
    assert core.store.rows("SELECT 1 FROM companion_avatar_assets") == []
    root = avatar.directory(core.store)
    assert not root.exists() or list(root.iterdir()) == []
    response = client.post(base + "/avatars", json={**body, "expected_revision": 0},
                           headers=headers)
    assert response.status_code == 200, response.text
    asset = response.json()["asset"]["asset_id"]
    assert response.json()["profile"]["avatar_asset_id"] == asset
    listed = client.get(base + "/avatars", headers=headers).json()["avatars"]
    assert [item["asset_id"] for item in listed] == [asset]
    served = client.get(base + "/assets/" + asset, headers=headers)
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert served.content == avatar.read_bytes(core.store, asset)
    assert client.get(base + "/assets/avt_" + "1" * 32, headers=headers).status_code == 410
    bad = client.post(base + "/avatars", headers=headers, json={
        **body, "data_base64": "!!!!", "operation_id": "upload-op-2"})
    assert bad.status_code == 400
