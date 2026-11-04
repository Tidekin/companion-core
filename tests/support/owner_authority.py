"""Activate a synthetic owner grant through the real preview and journal path."""

from companion import owner_authority as authority
from companion.owner_authority_protocol import AuthorityChange, AuthorityPreview


def activate(core, policy, *, revision=0, operation="operation-activate-1"):
    query = AuthorityPreview(expected_revision=revision, policy=policy)
    preview = authority.preview(core.store, core.settings, core.clock, query)
    request = AuthorityChange(operation_id=operation, **query.model_dump(),
                              preview_sha256=preview["preview_sha256"])
    return authority.activate(core.store, core.settings, core.clock, request), request
