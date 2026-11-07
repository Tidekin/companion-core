"""Keep publicly rebuildable runtime images; never rebuild in an artifact consumer."""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import time
import zipfile
import zlib
from functools import lru_cache
from pathlib import Path

if __package__:
    from .verify_package import FORBIDDEN
else:
    from verify_package import FORBIDDEN

ROOT = Path(__file__).resolve().parents[1]
KIND = "PUBLIC_SOURCE_RUNTIME_ARTIFACT"
MANIFEST = "container-images.json"
COMPONENTS = {
    "core": {
        "tag": "companion-core:test", "recipe": "Dockerfile", "lock": "uv.lock",
        "copies": {"pyproject.toml", "uv.lock", "companion", "source_reader",
                   "companion_onebot_bridge", "companion_meme_protocol", "companion_meme_sources",
                   "companion_voice_node", "companion_delivery_protocol", "migrations", "db"},
    },
    "bridge": {
        "tag": "companion-onebot-bridge:review", "recipe": "deploy/onebot-bridge.Dockerfile",
        "lock": "deploy/onebot-bridge.lock",
        "copies": {"deploy/onebot-bridge.lock", "companion_onebot_bridge",
                   "companion_meme_protocol", "companion_meme_sources",
                   "companion_delivery_protocol", "scripts/probe_snowluma_direct.py"},
    },
    "reader": {
        "tag": "companion-source-reader:test", "recipe": "deploy/source-reader.Dockerfile",
        "lock": "uv.lock",
        "copies": {"pyproject.toml", "uv.lock", "source_reader", "companion/__init__.py",
                   "companion/source_policy.py", "companion/video_media_contract.py",
                   "companion/bili_video_protocol.py"},
    },
    "ui": {
        "tag": "companion-console-ui:test", "recipe": "deploy/console-ui.Dockerfile",
        "lock": None,
        "base": "nginx:1.31.8-alpine@sha256:"
                "1ef7ca94d7879df82421428ce13bd1eaa412404953859cf7a5b6512c25e67a60",
        "copies": {"deploy/console-ui.nginx.conf", "companion/console_static/index.html",
                   "companion/console_static/app.js", "companion/console_static/app.css",
                   "companion/console_static/icon.svg"},
    },
}
POSTGRES_IMAGE = ("postgres:16.15-trixie@sha256:"
                  "1beaa3084eee2676f20a9cd9928f0bf2c5bf674f8e49f81017912183d377c61c")
CORE_BASE = ("python:3.12-slim@sha256:"
             "a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1")
PG_CLIENT_COPIES = {
    ("/usr/lib/postgresql/16/bin/pg_dump", "/usr/lib/postgresql/16/bin/pg_restore",
     "/usr/local/bin/"),
    (*("/usr/lib/x86_64-linux-gnu/lib" + lib + ".so.*" for lib in (
        "pq", "zstd", "lz4", "gssapi_krb5", "ldap", "xxhash", "krb5", "k5crypto",
        "com_err", "krb5support", "lber", "sasl2", "keyutils",
    )), "/usr/local/lib/postgresql/"),
    ("/usr/share/doc/", "/usr/local/share/doc/postgresql-clients/"),
}
SAFE_ENV = {
    "PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "PYTHON_PIP_VERSION",
    "PYTHON_SETUPTOOLS_VERSION", "PYTHON_GET_PIP_URL", "PYTHON_GET_PIP_SHA256",
    "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "UV_LINK_MODE", "HF_HUB_DISABLE_XET",
    "PLAYWRIGHT_BROWSERS_PATH", "TMPDIR",
}
# Actual linux/amd64 config of the pinned official Nginx base, read with imagetools.
UI_BASE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "NGINX_VERSION": "1.31.8", "PKG_RELEASE": "1", "DYNPKG_RELEASE": "1",
    "NJS_VERSION": "1.0.2", "NJS_RELEASE": "1", "ACME_VERSION": "0.4.1",
}
PRIVATE_PARTS = FORBIDDEN | {".git", "logs", "private-stage", "keys", "secrets", "credentials"}
CANARY = b"SYNTHETIC_PACKAGING_CANARY_NOT_A_SECRET"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST_TYPES = {OCI_MANIFEST, "application/vnd.docker.distribution.manifest.v2+json"}
INDEX_TYPES = {OCI_INDEX, "application/vnd.docker.distribution.manifest.list.v2+json"}
CONFIG_TYPES = {"application/vnd.oci.image.config.v1+json",
                "application/vnd.docker.container.image.v1+json"}
LAYER_TYPES = {"application/vnd.oci.image.layer.v1.tar",
               "application/vnd.oci.image.layer.v1.tar+gzip",
               "application/vnd.docker.image.rootfs.diff.tar",
               "application/vnd.docker.image.rootfs.diff.tar.gzip"}


def run(arguments, *, root=ROOT, output=None, timeout=900):
    if arguments[0] == "docker":
        arguments = ["docker", "--context", local_context(), *arguments[1:]]
    result = subprocess.run(arguments, cwd=root, stdout=output or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Docker history/inspect and archive errors must not echo potential values.
        raise RuntimeError("CONTAINER_ARTIFACT_COMMAND_FAILED")
    return result.stdout


@lru_cache(maxsize=1)
def local_context():
    """Use the same local-daemon boundary as the existing dev smoke."""
    context = os.environ.get("COMPANION_CI_DOCKER_CONTEXT", "default")
    value = subprocess.run(["docker", "context", "inspect", context, "--format",
                            "{{json .Endpoints.docker.Host}}"], capture_output=True,
                           timeout=15, check=True)
    endpoint = json.loads(value.stdout)
    if not isinstance(endpoint, str) or not (
        re.fullmatch(r"npipe:////\./pipe/[^/\\?#\s\x00-\x1f\x7f]+", endpoint)
        or re.fullmatch(r"unix:///[^/\\?#\s\x00-\x1f\x7f][^\\?#\x00-\x1f\x7f]*", endpoint)
    ):
        raise ValueError("PUBLIC_IMAGE_LOCAL_CONTEXT_REQUIRED")
    return context


def digest_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def source_identity(root=ROOT):
    commit, tree = run(["git", "rev-parse", "HEAD", "HEAD^{tree}"], root=root).decode().splitlines()
    return {"commit": commit, "tree": tree}


def check_public_inputs(root=ROOT):
    """Fixed reviewed COPY inputs, rather than an inferred source dependency graph."""
    for spec in COMPONENTS.values():
        recipe = (root / spec["recipe"]).read_text("utf-8")
        if spec.get("base") and [line.strip() for line in recipe.splitlines()
                                 if line.lstrip().upper().startswith("FROM ")] != [
                                     "FROM " + spec["base"]]:
            raise ValueError("PUBLIC_IMAGE_UI_BASE_CHANGED")
        inputs = set()
        client_copies = []
        for line in re.sub(r"\\\r?\n", " ", recipe).splitlines():
            if line.upper().split(maxsplit=1)[:1] != ["COPY"]:
                continue
            tokens = shlex.split(line.strip())[1:]
            if spec is COMPONENTS["core"] and tokens[:1] == ["--from=postgres-client"]:
                client_copies.append(tuple(tokens[1:]))
                continue
            if len(tokens) < 2 or any(token.startswith("--") for token in tokens):
                raise ValueError("PUBLIC_IMAGE_COPY_SHAPE_CHANGED")
            inputs.update(tokens[:-1])
        if inputs != spec["copies"]:
            raise ValueError("PUBLIC_IMAGE_COPY_INPUTS_CHANGED")
        if spec is COMPONENTS["core"]:
            instructions = [line.strip() for line in recipe.splitlines()
                            if line.strip() and not line.lstrip().startswith("#")]
            stages = [line for line in instructions if line.upper().split(maxsplit=1)[0] == "FROM"]
            expected = [f"FROM {POSTGRES_IMAGE} AS postgres-client", f"FROM {CORE_BASE}"]
            if stages != expected or instructions[:2] != expected:
                raise ValueError("PUBLIC_IMAGE_PG_CLIENT_STAGE_CHANGED")
            if sorted(client_copies) != sorted(PG_CLIENT_COPIES):
                raise ValueError("PUBLIC_IMAGE_PG_CLIENT_COPIES_CHANGED")
        for name in inputs:
            target = root / name
            if not target.exists():
                raise ValueError("PUBLIC_IMAGE_COPY_INPUT_MISSING")
            paths = target.rglob("*") if target.is_dir() else [target]
            for path in paths:
                relative = path.relative_to(root)
                parts = tuple(part.casefold() for part in relative.parts)
                if (PRIVATE_PARTS.intersection(parts)
                        or any(part.startswith(".env.") for part in parts)
                        or path.suffix.lower() in {
                            ".db", ".sqlite", ".sqlite3", ".enc", ".key", ".pem",
                            ".p12", ".pfx", ".log"}
                        or any(part.endswith(".owner-images") for part in parts)):
                    raise ValueError("PUBLIC_IMAGE_PRIVATE_INPUT")
                if path.is_file() and CANARY in path.read_bytes():
                    raise ValueError("PUBLIC_IMAGE_PACKAGING_CANARY")


def prepare_context(destination, *, root=ROOT):
    """Build only the committed public snapshot, including when the checkout is dirty."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    archive_path = destination.parent / (destination.name + ".tar")
    with archive_path.open("wb") as output:
        run(["git", "archive", "--format=tar", "HEAD"], root=root, output=output)
    with tarfile.open(archive_path) as archive:
        archive.extractall(destination, filter="data")
    archive_path.unlink()
    check_public_inputs(destination)
    source = source_identity(root)
    write_json(destination.parent / "public-context.json", source)
    return source


def verify_context(context, commit, *, root=ROOT):
    """Git's tree and raw blob IDs bind the actual build directory to the commit."""
    expected, directories = {}, set()
    for entry in run(["git", "ls-tree", "-r", "-t", "-z", "--full-tree", commit],
                     root=root).split(b"\0"):
        if not entry:
            continue
        fields, name = entry.split(b"\t", 1)
        mode, kind, identity = fields.decode().split()
        name = name.decode("utf-8")
        if kind == "tree":
            directories.add(name)
        elif kind == "blob" and mode in {"100644", "100755"}:
            expected[name] = (mode, identity)
        else:
            raise ValueError("PUBLIC_IMAGE_CONTEXT_TYPE_UNSUPPORTED")
    files, actual_directories = {}, set()
    for path in context.rglob("*"):
        name = path.relative_to(context).as_posix()
        if path.is_symlink():
            raise ValueError("PUBLIC_IMAGE_CONTEXT_TYPE_CHANGED")
        if path.is_dir():
            actual_directories.add(name)
        elif path.is_file():
            files[name] = path
        else:
            raise ValueError("PUBLIC_IMAGE_CONTEXT_TYPE_CHANGED")
    if set(files) != set(expected) or actual_directories != directories:
        raise ValueError("PUBLIC_IMAGE_CONTEXT_PATHS_CHANGED")
    names = list(expected)
    # Bounded argv batches handle Windows spaces without filters or shell parsing.
    for offset in range(0, len(names), 32):
        batch = names[offset:offset + 32]
        actual = run(["git", "hash-object", "--no-filters", "--",
                      *(str(files[name]) for name in batch)], root=root).decode().splitlines()
        if actual != [expected[name][1] for name in batch]:
            raise ValueError("PUBLIC_IMAGE_CONTEXT_CONTENT_CHANGED")
        if os.name != "nt" and any(stat.S_IMODE(files[name].stat().st_mode) !=
                                   int(expected[name][0][-3:], 8) for name in batch):
            raise ValueError("PUBLIC_IMAGE_CONTEXT_FILE_MODE_CHANGED")
    # Recheck COPY/recipe and privacy policy at the actual build entry point too.
    check_public_inputs(context)


def build_component(name, context, cache, *, root=ROOT, runner=run):
    """Native BuildKit and Compose retain the existing build and smoke surfaces."""
    context, cache = Path(context).resolve(), Path(cache).resolve()
    source = json.loads((context.parent / "public-context.json").read_text())
    if source != source_identity(root):
        raise ValueError("PUBLIC_IMAGE_BUILD_SOURCE_CHANGED")
    verify_context(context, source["commit"], root=root)
    previous = cache / name
    exported = cache / (name + "-next")
    imported = previous / "index.json"
    cache_from = ["type=local,src=" + str(previous)] if imported.is_file() else []
    cache_to = "type=local,dest=" + str(exported) + ",mode=max"
    if name == "bridge":
        override = context.parent / "bridge-cache.json"
        write_json(override, {"services": {"onebot-bridge": {"build": {
            "cache_from": cache_from, "cache_to": [cache_to],
        }}}})
        command = ["docker", "compose", "-f", str(context / "deploy/compose.onebot-build.yaml"),
                   "-f", str(override), "build", "--builder", "companion-ci", "onebot-bridge"]
    else:
        spec = COMPONENTS[name]
        command = ["docker", "buildx", "build", "--builder", "companion-ci", "--load",
                   "--file", str(context / spec["recipe"]), "--tag", spec["tag"],
                   "--cache-to", cache_to]
        for value in cache_from:
            command += ["--cache-from", value]
        command.append(str(context))
    started = time.monotonic()
    runner(command)
    result = capture(name, root=context, runner=runner, identity=source)
    # Public snapshot is deliberately outside .git; identity comes from its parent checkout.
    result.update(source)
    result["build_and_inspect_seconds"] = round(time.monotonic() - started, 3)
    result["dependency_cache"] = {
        "backend": "buildkit-local", "configured_import_index_sha256":
        digest_file(imported) if imported.is_file() else None,
        "export_index_sha256": digest_file(exported / "index.json"),
        "export_bytes": sum(p.stat().st_size for p in exported.rglob("*") if p.is_file()),
        "cache_hits_measured": False,
    }
    if previous.exists():
        if previous.parent != cache or previous.name != name:
            raise ValueError("PUBLIC_IMAGE_CACHE_PATH_INVALID")
        shutil.rmtree(previous)
    exported.replace(previous)
    write_json(context.parent / (name + ".json"), result)
    return result


def inspect_image(name, *, runner=run):
    document, = json.loads(runner(["docker", "image", "inspect", COMPONENTS[name]["tag"]]))
    if not DIGEST.fullmatch(document.get("Id", "")):
        raise ValueError("PUBLIC_IMAGE_ID_REQUIRED")
    if document.get("Os") != "linux" or document.get("Architecture") != "amd64":
        raise ValueError("PUBLIC_IMAGE_PLATFORM_CHANGED")
    environment = document.get("Config", {}).get("Env") or []
    for value in environment:
        key, separator, _ = value.partition("=")
        if not separator or key not in (UI_BASE_ENV if name == "ui" else SAFE_ENV):
            raise ValueError("PUBLIC_IMAGE_ENV_NOT_ALLOWLISTED")
    if name == "ui" and sorted(environment) != sorted(
        key + "=" + value for key, value in UI_BASE_ENV.items()
    ):
        raise ValueError("PUBLIC_IMAGE_UI_BASE_ENV_CHANGED")
    history = runner(["docker", "image", "history", "--no-trunc", "--format",
                      "{{json .CreatedBy}}", document["Id"]]).decode()
    if re.search(r"(?:COMPANION_\w+|password|secret|token|private_key)\s*=", history, re.I):
        raise ValueError("PUBLIC_IMAGE_HISTORY_CONTAINS_CREDENTIAL_INPUT")
    return document


def capture(name, *, root=ROOT, runner=run, identity=None):
    image = inspect_image(name, runner=runner)
    spec = COMPONENTS[name]
    return {"component": name, **(source_identity(root) if identity is None else identity),
            "image_id": image["Id"],
            "producer_descriptor": image.get("Descriptor"), "platform": "linux/amd64",
            "image_bytes": image["Size"],
            "rootfs_diff_ids": image.get("RootFS", {}).get("Layers", []),
            "recipe_sha256": digest_file(root / spec["recipe"]),
            "dependency_lock_sha256": digest_file(root / spec["lock"]) if spec["lock"] else None,
            "base_image": spec.get("base"),
            "registry_manifest_digest": None}


def descriptor(value, media_types):
    if (not isinstance(value, dict) or value.get("mediaType") not in media_types
            or not isinstance(value.get("digest"), str) or not DIGEST.fullmatch(value["digest"])
            or type(value.get("size")) is not int or value["size"] <= 0):
        raise ValueError("PUBLIC_IMAGE_ARCHIVE_DESCRIPTOR_INVALID")
    platform = value.get("platform")
    if platform is not None and (not isinstance(platform, dict) or platform.get("os") != "linux"
            or platform.get("architecture") != "amd64"
            or platform.get("variant") not in (None, "")):
        raise ValueError("PUBLIC_IMAGE_ARCHIVE_PLATFORM_CHANGED")
    return {key: value[key] for key in ("mediaType", "digest", "size")}


def archive_identity(path, component):
    """Read one Docker OCI save graph; no extraction, registry lookup, or tag trust."""
    path = Path(path)
    expected_archive = component.get("archive")
    if expected_archive is not None and (path.stat().st_size != expected_archive["bytes"]
            or digest_file(path) != expected_archive["sha256"]):
        raise ValueError("PUBLIC_IMAGE_ARCHIVE_DIGEST_CHANGED")
    blobs, documents, seen = {}, {}, set()
    with tarfile.open(path, mode="r|*") as archive:
        for member in archive:
            identity_file = member.name in {"index.json", "oci-layout", "manifest.json"}
            blob_name = re.fullmatch(r"blobs/sha256/([0-9a-f]{64})", member.name)
            if not identity_file and not blob_name:
                continue
            if not member.isfile() or member.name in seen:
                raise ValueError("PUBLIC_IMAGE_ARCHIVE_ENTRY_INVALID")
            seen.add(member.name)
            if identity_file and member.size > 1024 * 1024:
                raise ValueError("PUBLIC_IMAGE_ARCHIVE_DOCUMENT_TOO_LARGE")
            digest, diff, small, decompressor = hashlib.sha256(), hashlib.sha256(), [], None
            with archive.extractfile(member) as source:
                first = True
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    if member.size <= 1024 * 1024:
                        small.append(chunk)
                    if first and chunk.startswith(b"\x1f\x8b"):
                        decompressor = zlib.decompressobj(31)
                    first = False
                    if decompressor is None:
                        diff.update(chunk)
                    else:
                        pending = chunk
                        while pending:
                            diff.update(decompressor.decompress(pending, 1024 * 1024))
                            pending = decompressor.unconsumed_tail
                if decompressor is not None and (not decompressor.eof or decompressor.unused_data):
                    raise ValueError("PUBLIC_IMAGE_ARCHIVE_LAYER_INVALID")
            if blob_name:
                if digest.hexdigest() != blob_name[1]:
                    raise ValueError("PUBLIC_IMAGE_ARCHIVE_BLOB_DIGEST_CHANGED")
                blobs[member.name] = {"digest": "sha256:" + digest.hexdigest(),
                                     "size": member.size, "diff_id": "sha256:" + diff.hexdigest(),
                                     "gzip": decompressor is not None}
            if small:
                documents[member.name] = b"".join(small)
    if (not documents.get("index.json") or not documents.get("oci-layout")
            or json.loads(documents["oci-layout"]).get("imageLayoutVersion") != "1.0.0"):
        raise ValueError("PUBLIC_IMAGE_ARCHIVE_LAYOUT_INVALID")

    def content(value, media_types):
        reference = descriptor(value, media_types)
        name = "blobs/sha256/" + reference["digest"][7:]
        actual = blobs.get(name, {})
        if any(actual.get(key) != reference[key] for key in ("digest", "size")):
            raise ValueError("PUBLIC_IMAGE_ARCHIVE_DESCRIPTOR_CHANGED")
        return reference, name

    def single(index):
        if not isinstance(index, dict) or index.get("schemaVersion") != 2 or (
                not isinstance(index.get("manifests"), list)) or (
                len(index["manifests"]) != 1):
            raise ValueError("PUBLIC_IMAGE_ARCHIVE_SINGLE_PLATFORM_REQUIRED")
        return index["manifests"][0]

    root, name = content(single(json.loads(documents["index.json"])), INDEX_TYPES | MANIFEST_TYPES)
    manifest = json.loads(documents[name])
    platform_manifest = root
    if root["mediaType"] in INDEX_TYPES:
        if (not isinstance(manifest, dict)
                or manifest.get("mediaType", root["mediaType"]) != root["mediaType"]):
            raise ValueError("PUBLIC_IMAGE_ARCHIVE_MANIFEST_INVALID")
        platform_manifest, name = content(single(manifest), MANIFEST_TYPES)
        manifest = json.loads(documents[name])
    if (not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2
            or manifest.get("mediaType", platform_manifest["mediaType"]) !=
            platform_manifest["mediaType"]):
        raise ValueError("PUBLIC_IMAGE_ARCHIVE_MANIFEST_INVALID")
    config, name = content(manifest.get("config"), CONFIG_TYPES)
    settings = json.loads(documents[name])
    if not isinstance(settings, dict) or not isinstance(settings.get("rootfs"), dict):
        raise ValueError("PUBLIC_IMAGE_ARCHIVE_CONFIG_INVALID")
    rootfs = settings["rootfs"]
    diff_ids = rootfs.get("diff_ids")
    if (settings.get("os") != "linux" or settings.get("architecture") != "amd64"
            or rootfs.get("type") != "layers" or not isinstance(diff_ids, list)
            or any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in diff_ids)
            or not isinstance(manifest.get("layers"), list)
            or len(manifest["layers"]) != len(diff_ids)):
        raise ValueError("PUBLIC_IMAGE_ARCHIVE_PLATFORM_CHANGED")
    for layer, diff_id in zip(manifest["layers"], diff_ids, strict=True):
        _, name = content(layer, LAYER_TYPES)
        compressed = layer["mediaType"].endswith(("tar+gzip", "tar.gzip"))
        if blobs[name]["diff_id"] != diff_id or blobs[name]["gzip"] != compressed:
            raise ValueError("PUBLIC_IMAGE_ARCHIVE_ROOTFS_CHANGED")
    if "manifest.json" in seen:
        legacy = json.loads(documents.get("manifest.json", b"null"))
        if not isinstance(legacy, list) or len(legacy) != 1 or not isinstance(legacy[0], dict):
            raise ValueError("PUBLIC_IMAGE_ARCHIVE_DOCKER_MANIFEST_CHANGED")
        legacy = legacy[0]
        legacy_config = legacy.get("Config")
        legacy_layers = legacy.get("Layers")
        if (not isinstance(legacy_config, str)
                or blobs.get(legacy_config, {}).get("digest") != config["digest"]
                or not isinstance(legacy_layers, list)
                or any(not isinstance(value, str) for value in legacy_layers)
                or [blobs.get(value, {}).get("diff_id") for value in legacy_layers] != diff_ids):
            raise ValueError("PUBLIC_IMAGE_ARCHIVE_DOCKER_MANIFEST_CHANGED")
    if ("config_digest" in component and "archive_descriptor" not in component
            and component["config_digest"] != component["image_id"]):
        raise ValueError("PUBLIC_IMAGE_ARCHIVE_IDENTITY_CHANGED")
    if (component.get("platform") != "linux/amd64"
            or component.get("config_digest", config["digest"]) != config["digest"]
            or component.get("rootfs_diff_ids") != diff_ids
            or component.get("image_id") not in {config["digest"], root["digest"],
                                                 platform_manifest["digest"]}):
        raise ValueError("PUBLIC_IMAGE_ARCHIVE_IDENTITY_CHANGED")
    for key, actual in (("archive_descriptor", root),
                        ("platform_manifest_descriptor", platform_manifest)):
        if key in component and descriptor(component[key], INDEX_TYPES | MANIFEST_TYPES) != actual:
            raise ValueError("PUBLIC_IMAGE_ARCHIVE_IDENTITY_CHANGED")
    producer = component.get("producer_descriptor")
    if producer is not None and descriptor(producer, INDEX_TYPES | MANIFEST_TYPES) not in (
            root, platform_manifest):
        raise ValueError("PUBLIC_IMAGE_ARCHIVE_PRODUCER_CHANGED")
    return {"config_digest": config["digest"], "archive_descriptor": root,
            "platform_manifest_descriptor": platform_manifest, "rootfs_diff_ids": diff_ids}


def export_images(captured, destination, *, root=ROOT, runner=run):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    identity = source_identity(root)
    if set(captured) != set(COMPONENTS):
        raise ValueError("PUBLIC_IMAGE_COMPONENT_SET_CHANGED")
    metadata = {"kind": KIND, **identity, "repository": os.environ.get("GITHUB_REPOSITORY"),
                "run_id": int(os.environ.get("GITHUB_RUN_ID", "0")),
                "attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")), "components": {}}
    for name, built in captured.items():
        if any(built.get(key) != value for key, value in identity.items()):
            raise ValueError("PUBLIC_IMAGE_SOURCE_CHANGED")
        if inspect_image(name, runner=runner)["Id"] != built["image_id"]:
            raise ValueError("PUBLIC_IMAGE_CHANGED_AFTER_VALIDATION")
        raw = destination / (name + ".tar")
        compressed = destination / (name + ".tar.gz")
        started = time.monotonic()
        runner(["docker", "image", "save", "--output", str(raw), built["image_id"]])
        raw_sha, raw_bytes = digest_file(raw), raw.stat().st_size
        saved = time.monotonic()
        portable = archive_identity(raw, built)
        verified = time.monotonic()
        with raw.open("rb") as source, gzip.open(compressed, "wb", compresslevel=1) as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        entry = {**built, **portable, "archive": {"filename": compressed.name,
                 "sha256": digest_file(compressed), "bytes": compressed.stat().st_size,
                 "uncompressed_sha256": raw_sha, "uncompressed_bytes": raw_bytes},
                 "measurement": {"save_and_hash_seconds": round(saved - started, 3),
                                 "archive_identity_seconds": round(verified - saved, 3),
                                 "compress_and_hash_seconds": round(
                                     time.monotonic() - verified, 3)}}
        raw.unlink()
        metadata["components"][name] = entry
    write_json(destination / MANIFEST, metadata)
    return metadata


def artifact_reference(identity, name):
    if (not isinstance(identity, dict) or set(identity) != {"id", "digest", "name"}
            or type(identity.get("id")) is not int or identity["id"] <= 0
            or not isinstance(identity.get("digest"), str)
            or not DIGEST.fullmatch(identity["digest"]) or identity["name"] != name):
        raise ValueError("PUBLIC_IMAGE_ARTIFACT_REFERENCE_REQUIRED")
    return identity


def bind_artifacts(path, env=os.environ):
    metadata = json.loads(Path(path).read_text())
    for name in COMPONENTS:
        prefix = "CI_" + name.upper()
        item = {"id": int(env[prefix + "_ARTIFACT_ID"]),
                "digest": env[prefix + "_ARTIFACT_DIGEST"],
                "name": f"runtime-image-{name}-{metadata['attempt']}"}
        if not item["digest"].startswith("sha256:"):
            item["digest"] = "sha256:" + item["digest"]
        metadata["components"][name]["artifact"] = artifact_reference(item, item["name"])
    write_json(path, metadata)


def metadata_reference(env=os.environ):
    raw_id, raw_digest = env.get("CI_IMAGES_ARTIFACT_ID"), env.get("CI_IMAGES_ARTIFACT_DIGEST")
    if not raw_id or not raw_digest:
        raise ValueError("PUBLIC_IMAGE_METADATA_ARTIFACT_REQUIRED")
    if not raw_digest.startswith("sha256:"):
        raw_digest = "sha256:" + raw_digest
    return artifact_reference({"id": int(raw_id), "digest": raw_digest,
                               "name": "ci-container-images-" + env["GITHUB_RUN_ATTEMPT"]},
                              "ci-container-images-" + env["GITHUB_RUN_ATTEMPT"])


def verified_metadata(reference, artifacts, read, repo, expected):
    """Small authenticated metadata binds the separately retained image archives."""
    artifact_reference(reference, "ci-container-images-" + str(expected["attempt"]))
    if any(type(item.get("id")) is not int or item["id"] <= 0 for item in artifacts):
        raise ValueError("PUBLIC_IMAGE_ARTIFACT_ID_INVALID")
    by_id = {item["id"]: item for item in artifacts}
    if len(by_id) != len(artifacts):
        raise ValueError("PUBLIC_IMAGE_ARTIFACT_AMBIGUOUS")
    item = by_id.get(reference["id"], {})
    if (item.get("expired") is not False or item.get("name") != reference["name"]
            or item.get("digest") != reference["digest"]
            or type(item.get("size_in_bytes")) is not int
            or not 0 < item["size_in_bytes"] <= 65536):
        raise ValueError("PUBLIC_IMAGE_METADATA_UNAVAILABLE")
    payload = read(f"repos/{repo}/actions/artifacts/{item['id']}/zip", binary=True)
    if "sha256:" + hashlib.sha256(payload).hexdigest() != reference["digest"]:
        raise ValueError("PUBLIC_IMAGE_METADATA_DIGEST_CHANGED")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        if archive.namelist() != [MANIFEST] or archive.getinfo(MANIFEST).file_size > 65536:
            raise ValueError("PUBLIC_IMAGE_METADATA_ARCHIVE_INVALID")
        metadata = json.loads(archive.read(MANIFEST))
    if (not isinstance(metadata, dict) or metadata.get("kind") != KIND
            or not isinstance(metadata.get("components"), dict)
            or set(metadata["components"]) != set(COMPONENTS)
            or any(metadata.get(key) != value for key, value in expected.items())):
        raise ValueError("PUBLIC_IMAGE_METADATA_SOURCE_CHANGED")
    for name, component in metadata["components"].items():
        if (not isinstance(component, dict) or component.get("component") != name
                or not isinstance(component.get("image_id"), str)
                or not DIGEST.fullmatch(component["image_id"])
                or not isinstance(component.get("config_digest"), str)
                or not DIGEST.fullmatch(component["config_digest"])
                or component.get("platform") != "linux/amd64"
                or component.get("registry_manifest_digest") is not None
                or any(component.get(k) != expected[k] for k in ("commit", "tree"))):
            raise ValueError("PUBLIC_IMAGE_COMPONENT_INVALID")
        if "archive_descriptor" in component:
            archive_descriptor = descriptor(component["archive_descriptor"],
                                            INDEX_TYPES | MANIFEST_TYPES)
            platform_descriptor = descriptor(component.get("platform_manifest_descriptor"),
                                             MANIFEST_TYPES)
            if component["image_id"] not in {
                    component["config_digest"], archive_descriptor["digest"],
                    platform_descriptor["digest"]}:
                raise ValueError("PUBLIC_IMAGE_COMPONENT_INVALID")
        elif component["config_digest"] != component["image_id"]:
            raise ValueError("PUBLIC_IMAGE_COMPONENT_INVALID")
        if name == "ui" and (component.get("base_image") != COMPONENTS[name]["base"]
                             or "dependency_lock_sha256" not in component
                             or component["dependency_lock_sha256"] is not None):
            raise ValueError("PUBLIC_IMAGE_UI_BASE_METADATA_CHANGED")
        ref = artifact_reference(component.get("artifact"),
                                 f"runtime-image-{name}-{expected['attempt']}")
        actual = by_id.get(ref["id"], {})
        if (actual.get("expired") is not False or actual.get("name") != ref["name"]
                or actual.get("digest") != ref["digest"]):
            raise ValueError("PUBLIC_IMAGE_ARCHIVE_UNAVAILABLE")
        data = component.get("archive", {})
        if (not isinstance(data, dict) or data.get("filename") != name + ".tar.gz"
                or type(data.get("bytes")) is not int or data["bytes"] <= 0
                or not isinstance(data.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"])):
            raise ValueError("PUBLIC_IMAGE_ARCHIVE_METADATA_INVALID")
    return metadata


def load_archive(component, archive, *, runner=run, reuse_existing=False):
    """An authenticated original archive is the only input; there is no build fallback."""
    archive = Path(archive)
    identity = archive_identity(archive, component)
    descriptors = {identity[key]["digest"]: identity[key] for key in (
        "archive_descriptor", "platform_manifest_descriptor")}
    allowed = {identity["config_digest"], *descriptors}

    def inspect_existing():
        inventory = set(runner([
            "docker", "image", "ls", "--all", "--no-trunc", "--quiet"]).decode().split())
        if any(not DIGEST.fullmatch(value) for value in inventory):
            raise ValueError("PUBLIC_IMAGE_LOCAL_INVENTORY_INVALID")
        matches = inventory & allowed
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("PUBLIC_IMAGE_LOADED_IDENTITY_AMBIGUOUS")
        selected, = matches
        image, = json.loads(runner(["docker", "image", "inspect", selected]))
        if (image.get("Id") != selected or image.get("Os") != "linux"
                or image.get("Architecture") != "amd64"
                or image.get("RootFS", {}).get("Layers") != identity["rootfs_diff_ids"]):
            raise ValueError("PUBLIC_IMAGE_LOADED_IDENTITY_CHANGED")
        actual_descriptor = image.get("Descriptor")
        if selected != identity["config_digest"] and actual_descriptor is None:
            raise ValueError("PUBLIC_IMAGE_LOADED_DESCRIPTOR_REQUIRED")
        if actual_descriptor is not None:
            actual_descriptor = descriptor(actual_descriptor, INDEX_TYPES | MANIFEST_TYPES)
            if (descriptors.get(actual_descriptor["digest"]) != actual_descriptor
                    or selected != identity["config_digest"]
                    and actual_descriptor["digest"] != selected):
                raise ValueError("PUBLIC_IMAGE_LOADED_IDENTITY_CHANGED")
        return selected

    selected = inspect_existing() if reuse_existing else None
    action = "already_present"
    if selected is None:
        runner(["docker", "image", "load", "--input", str(archive)])
        action, selected = "loaded", inspect_existing()
        if selected is None:
            raise ValueError("PUBLIC_IMAGE_LOADED_IDENTITY_CHANGED")
    return {"producer_image_id": component["image_id"],
            **{key: value for key, value in identity.items() if key != "rootfs_diff_ids"},
            "consumer_local_image_id": selected, "platform": "linux/amd64", "action": action,
            "archive_sha256": component["archive"]["sha256"],
            "archive_bytes": archive.stat().st_size}


def load_local(path, *, runner=run, reuse_existing=False):
    """Exercise Docker's archive codec before upload; this alone is not full CI proof."""
    path = Path(path)
    metadata = json.loads(path.read_text())
    if metadata.get("kind") != KIND or set(metadata.get("components", {})) != set(COMPONENTS):
        raise ValueError("PUBLIC_IMAGE_LOCAL_ARCHIVE_SET_CHANGED")
    loaded = {}
    for name, component in metadata["components"].items():
        if component["archive"]["filename"] != name + ".tar.gz":
            raise ValueError("PUBLIC_IMAGE_LOCAL_ARCHIVE_NAME_CHANGED")
        started = time.monotonic()
        loaded[name] = {**load_archive(component, path.parent / component["archive"]["filename"],
                                     runner=runner, reuse_existing=reuse_existing),
            "load_and_inspect_seconds": round(time.monotonic() - started, 3)}
    write_json(path.parent / "local-load.json", {"components": loaded, "build_performed": False,
                                                "reuse_existing": reuse_existing})
    return loaded


def resolve_source(repo, run_id, *, read=None):
    """Main reuse points back to the original full run and original retained images."""
    if __package__:
        from . import ci_reuse
    else:
        import ci_reuse
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("PUBLIC_IMAGE_REPOSITORY_INVALID")
    read = ci_reuse.api if read is None else read
    origin = read(f"repos/{repo}/actions/runs/{int(run_id)}")
    if (origin.get("path") != ci_reuse.WORKFLOW or origin.get("status") != "completed"
            or origin.get("conclusion") != "success"
            or origin.get("repository", {}).get("full_name") != repo
            or origin.get("head_repository", {}).get("full_name") != repo):
        raise ValueError("PUBLIC_IMAGE_RUN_NOT_VERIFIED")

    def artifacts(run):
        return read(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]

    def small_artifact(items, name, filename):
        matches = [item for item in items if item["name"] == name and not item["expired"]]
        if len(matches) != 1 or not 0 < matches[0]["size_in_bytes"] <= 65536:
            raise ValueError("PUBLIC_IMAGE_SOURCE_ARTIFACT_UNAVAILABLE")
        data = read(f"repos/{repo}/actions/artifacts/{matches[0]['id']}/zip", binary=True)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if archive.namelist() != [filename] or archive.getinfo(filename).file_size > 65536:
                raise ValueError("PUBLIC_IMAGE_SOURCE_ARTIFACT_INVALID")
            return json.loads(archive.read(filename))

    source = origin
    target_tree = None
    if origin["event"] == "push":
        if origin.get("head_branch") != "main":
            raise ValueError("PUBLIC_IMAGE_DEFAULT_BRANCH_REQUIRED")
        target = read(f"repos/{repo}/git/commits/{origin['head_sha']}")
        target_tree = target["tree"]["sha"]
        validation = small_artifact(artifacts(origin),
                                    f"ci-validation-{origin['run_attempt']}", "validation.json")
        if (validation.get("passed") is not True or validation.get("commit") != origin["head_sha"]
                or validation.get("tree") != target_tree
                or validation.get("run_id") != str(origin["id"])
                or validation.get("attempt") != str(origin["run_attempt"])):
            raise ValueError("PUBLIC_IMAGE_MAIN_TREE_CHANGED")
        if validation.get("profile") == "reuse":
            proof = validation.get("reuse") or {}
            if (proof.get("tree") != target_tree or type(proof.get("run_id")) is not int
                    or type(proof.get("attempt")) is not int or proof["attempt"] <= 0):
                raise ValueError("PUBLIC_IMAGE_ORIGINAL_FULL_RUN_REQUIRED")
            source = read(f"repos/{repo}/actions/runs/{proof['run_id']}")
            if source.get("run_attempt") != proof.get("attempt"):
                raise ValueError("PUBLIC_IMAGE_ORIGINAL_ATTEMPT_CHANGED")
        elif validation.get("profile") != "full":
            raise ValueError("PUBLIC_IMAGE_FULL_COVERAGE_REQUIRED")
    elif origin["event"] != "pull_request":
        raise ValueError("PUBLIC_IMAGE_SOURCE_EVENT_INVALID")
    verified = ci_reuse.verified_run(source, repo, source["head_sha"], read,
                                      expected_event=source["event"])
    if verified is None:
        raise ValueError("PUBLIC_IMAGE_ORIGINAL_FULL_RUN_NOT_VERIFIED")
    metadata, proof = verified
    commit = read(f"repos/{repo}/git/commits/{metadata['commit']}")
    if commit["tree"]["sha"] != proof["tree"] or target_tree not in (None, proof["tree"]):
        raise ValueError("PUBLIC_IMAGE_CHECKOUT_TREE_CHANGED")
    if source["event"] == "pull_request":
        parents = commit.get("parents", [])
        if len(parents) != 2 or parents[1].get("sha") != source["head_sha"]:
            raise ValueError("PUBLIC_IMAGE_PR_MERGE_PREVIEW_REQUIRED")
    elif metadata["commit"] != source["head_sha"]:
        raise ValueError("PUBLIC_IMAGE_PUSH_CHECKOUT_CHANGED")
    return metadata, proof


def download_archive(repo, component, destination, *, runner=run):
    """Stream large archives through gh; API tokens never enter Docker or the image."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    reference = component["artifact"]
    payload = destination / (component["component"] + ".zip")
    started = time.monotonic()
    with payload.open("wb") as output:
        runner(["gh", "api", "--hostname", "github.com",
                f"repos/{repo}/actions/artifacts/{reference['id']}/zip"], output=output)
    if "sha256:" + digest_file(payload) != reference["digest"]:
        raise ValueError("PUBLIC_IMAGE_DOWNLOAD_DIGEST_CHANGED")
    archive_path = destination / component["archive"]["filename"]
    with zipfile.ZipFile(payload) as archive:
        if archive.namelist() != [archive_path.name]:
            raise ValueError("PUBLIC_IMAGE_DOWNLOAD_ENTRIES_INVALID")
        if archive.getinfo(archive_path.name).file_size != component["archive"]["bytes"]:
            raise ValueError("PUBLIC_IMAGE_DOWNLOAD_SIZE_CHANGED")
        with archive.open(archive_path.name) as source, archive_path.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
    payload.unlink()
    return archive_path, round(time.monotonic() - started, 3)


def consume(repo, run_id, destination, *, previous_run=None, read=None, runner=run,
            reuse_existing=False):
    metadata, proof = resolve_source(repo, run_id, read=read)
    previous, previous_proof = resolve_source(repo, previous_run, read=read) if previous_run else (
        None, None)
    results = {}
    for name, component in metadata["components"].items():
        archive, seconds = download_archive(repo, component, destination, runner=runner)
        started = time.monotonic()
        identity = load_archive(component, archive, runner=runner, reuse_existing=reuse_existing)
        results[name] = {**identity, "download_seconds": seconds,
                         "load_and_inspect_seconds": round(time.monotonic() - started, 3),
                         "archive_bytes": archive.stat().st_size,
                         "same_as_previous_verified_image": None if previous is None else (
                             component["config_digest"] ==
                             previous["components"][name]["config_digest"]
                             and component["platform"] == previous["components"][name]["platform"])}
    result = {"kind": KIND, "original_full_proof": proof, "components": results,
              "previous_full_proof": previous_proof,
              "registry_manifest_digest": None, "build_performed": False,
              "reuse_existing": reuse_existing}
    write_json(Path(destination) / "consumed-images.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("destination", type=Path)
    build = commands.add_parser("build")
    build.add_argument("component", choices=COMPONENTS)
    build.add_argument("--context", type=Path, required=True)
    build.add_argument("--cache", type=Path, required=True)
    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("component", choices=COMPONENTS)
    capture_parser.add_argument("output", type=Path)
    export = commands.add_parser("export")
    export.add_argument("captured", type=Path)
    export.add_argument("destination", type=Path)
    bind = commands.add_parser("bind")
    bind.add_argument("metadata", type=Path)
    load = commands.add_parser("load")
    load.add_argument("metadata", type=Path)
    load.add_argument("--reuse-existing", action="store_true")
    consumer = commands.add_parser("consume")
    consumer.add_argument("--repository", required=True)
    consumer.add_argument("--run-id", type=int, required=True)
    consumer.add_argument("--destination", type=Path, required=True)
    consumer.add_argument("--previous-run", type=int)
    consumer.add_argument("--reuse-existing", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare_context(args.destination)))
    elif args.command == "build":
        print(json.dumps(build_component(args.component, args.context, args.cache)))
    elif args.command == "capture":
        write_json(args.output, capture(args.component))
    elif args.command == "export":
        captures = {name: json.loads((args.captured / (name + ".json")).read_text())
                    for name in COMPONENTS}
        print(json.dumps(export_images(captures, args.destination)))
    elif args.command == "bind":
        bind_artifacts(args.metadata)
    elif args.command == "load":
        print(json.dumps(load_local(args.metadata, reuse_existing=args.reuse_existing)))
    else:
        print(json.dumps(consume(args.repository, args.run_id, args.destination,
                                 previous_run=args.previous_run,
                                 reuse_existing=args.reuse_existing)))


if __name__ == "__main__":
    main()
