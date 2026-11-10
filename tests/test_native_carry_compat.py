"""Native device grants retain real legacy carry evidence and stopped-custody gates."""

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import model_carry, owner_authority
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy
from companion_delivery_protocol.native_chat import device_identity
from tests.model_carry_support import migrated
from tests.support.model_carry_lifecycle import current_unknown, stop_current
from tests.support.owner_authority import activate


def device_policy(case):
    device_id, identity = device_identity(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex())
    return OwnerGrantPolicy.model_validate({**case.policy.model_dump(mode='json'),
                                           'native_devices': {device_id: identity}})


def test_pre_native_policy_bytes_and_carry_remain_valid(core, tmp_path, monkeypatch):
    original = OwnerGrantPolicy.model_dump
    def legacy(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        value.pop('native_devices', None)
        return value
    # Build the original admitted UNKNOWN and carry exactly as the pre-native version did.
    with monkeypatch.context() as patch:
        patch.setattr(OwnerGrantPolicy, 'model_dump', legacy)
        case = migrated(core, tmp_path)
    row = owner_authority._latest(core.store)
    assert 'native_devices' not in json.loads(row['policy_json'])
    before = core.store.rows('SELECT * FROM model_usage')
    prefix = Path(core.settings.paid_model.journal_path).read_bytes()
    assert case.policy.native_devices == {}
    with core.store.read_snapshot():
        saved = owner_authority.custody(core.store, core.settings, settlement=True)[
            'model_carries'][case.policy.unknown_carry_sha256]
        model_carry.authority_chain(core.store, saved, core.store.rows(
            'SELECT * FROM owner_authority_revisions WHERE revision>=3 ORDER BY revision'))
    assert owner_authority.current(core.store, core.settings, core.clock).authority.as_dict() == (
        case.successor['authority'])
    stop_current(core, case)
    active, _ = activate(core, case.policy, revision=4,
                         operation='same-policy-after-native-upgrade')
    assert active['authority']['revision'] == 5
    assert core.store.rows('SELECT * FROM model_usage') == before
    assert Path(core.settings.paid_model.journal_path).read_bytes().startswith(prefix)


def test_stopped_device_only_revision_retains_original_unknown(core, tmp_path):
    case = migrated(core, tmp_path)
    before = core.store.rows('SELECT * FROM model_usage')
    stop_current(core, case)
    active, _ = activate(core, device_policy(case), revision=4, operation='native-device-only')
    view = owner_authority.current(core.store, core.settings, core.clock)
    assert view.authority.as_dict() == active['authority']
    assert len(view.policy.native_devices) == 1
    assert core.store.rows('SELECT * FROM model_usage') == before
    assert before[0]['state'] == 'UNKNOWN'
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id='stop-before-native-device-removal', authority=active['authority']))
    activate(core, case.policy, revision=6, operation='remove-native-device-only')
    assert not owner_authority.current(core.store, core.settings, core.clock).policy.native_devices
    assert core.store.rows('SELECT * FROM model_usage') == before


def test_current_unknown_still_blocks_a_device_revision(core, tmp_path):
    case = migrated(core, tmp_path)
    current_unknown(core)
    stop_current(core, case)
    with pytest.raises(CoreError, match='OWNER_MODEL_CARRY_NOT_QUIESCENT'):
        activate(core, device_policy(case), revision=4, operation='reject-live-device-revision')


@pytest.mark.parametrize('extra', [{'max_reply_chars': 199}, {'source_retention_days': 12}])
def test_device_revision_cannot_change_other_policy_scope(core, tmp_path, extra):
    case = migrated(core, tmp_path)
    stop_current(core, case)
    before = Path(core.settings.paid_model.journal_path).read_bytes()
    with pytest.raises(CoreError):
        activate(core, device_policy(case).model_copy(update=extra), revision=4,
                 operation='reject-unrelated-device-scope')
    assert Path(core.settings.paid_model.journal_path).read_bytes() == before
