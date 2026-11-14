"""The Bridge and Core images must ship byte-identical delivery protocol packages.

A release refuses to pair images whose protocol digests differ, so a change here
needs a Bridge release in the same plan. Update PINNED only together with that.
"""

import hashlib
from pathlib import Path

import companion_delivery_protocol

# Digest currently carried by the deployed Bridge (scripts/release_plan.PROTOCOL_DIGEST).
PINNED = "d0be644c83ba1145ad428b7e6fc88922425255c0c18e9d71b446778983e03db5"


def digest():
    root = Path(companion_delivery_protocol.__file__).parent
    value = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        value.update(path.relative_to(root).as_posix().encode() + b"\0"
                     + path.read_bytes() + b"\0")
    return value.hexdigest()


def test_delivery_protocol_matches_the_deployed_bridge():
    assert digest() == PINNED, (
        "companion_delivery_protocol changed; Core and Bridge must be released together. "
        "Move Core-only helpers out of the package, or plan a Bridge release and update PINNED.")


def test_pin_uses_the_release_digest():
    from scripts.release_plan import PROTOCOL_DIGEST

    namespace = {}
    code = PROTOCOL_DIGEST.replace("print(h.hexdigest())", "result = h.hexdigest()")
    exec(code, namespace)  # noqa: S102 - the release script's own fixed snippet
    assert namespace["result"] == digest()
