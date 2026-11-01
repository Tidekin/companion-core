"""Bind a local voice profile to private Core connection inputs, without connecting."""

import argparse
import json
import os
from pathlib import Path

from companion.voice_protocol import digest
from companion_voice_node.worker import VoiceNodeConfig, local_manifest


def configure(arguments):
    root = arguments.runtime_root.resolve(strict=True)
    workspace = arguments.workspace.resolve(strict=True)
    profile = json.loads(arguments.profile.read_text(encoding="utf-8-sig"))
    connection = {
        "node_id": "owner-local-4070",
        "core_origin": "https://core.example.invalid",
        "core_public_key": None,
        "credential": None,
    }
    if arguments.connection is not None:
        connection = json.loads(arguments.connection.read_text(encoding="utf-8-sig"))
        required = {"node_id", "core_origin", "core_public_key", "credential"}
        if not required <= set(connection) or set(connection) - required - {"core_connect_address"}:
            raise ValueError(
                "Connection file requires exactly node ID, HTTPS origin, key and token"
            )
    value = {
        **connection,
        "workspace": str(workspace),
        "state_directory": str(workspace / ".local/voice-node" / connection["node_id"]),
        "tts": profile["tts"],
        "asr": profile["asr"],
        "tts_service_config": profile.get("tts_service_config"),
        "tts_server_revision": profile.get("tts_server_revision"),
        "heartbeat_seconds": 3,
        "request_timeout_seconds": 10,
        "execution_timeout_seconds": 90,
        "startup_timeout_seconds": 30,
        "result_cache_ttl_seconds": 120,
    }
    if profile.get("singing") is not None:
        value.update(singing=profile["singing"], execution_timeout_seconds=600,
                     request_timeout_seconds=30, result_cache_ttl_seconds=300)
    if arguments.connection is None:
        # Connection fields are excluded from the resource manifest. Validate the
        # remaining typed fields with disposable, in-memory values; save no token.
        check = {**value, "core_public_key": "0" * 64, "credential": "manifest-only-" * 4}
        config = VoiceNodeConfig.model_validate(check)
        output = root / "node-config.template.json"
    else:
        config = VoiceNodeConfig.model_validate(value)
        output = root / "node.private.json"
    manifest = local_manifest(config)
    manifest_sha = digest(manifest)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    (root / "node-manifest.json").write_text(
        json.dumps({"manifest_sha256": manifest_sha, "manifest": manifest}, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "config_file": output.name,
                "manifest_sha256": manifest_sha,
                "connection_configured": arguments.connection is not None,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--connection", type=Path)
    args = parser.parse_args()
    try:
        configure(args)
    except (OSError, ValueError):
        raise SystemExit("VOICE_RUNTIME_CONFIGURATION_INVALID") from None
