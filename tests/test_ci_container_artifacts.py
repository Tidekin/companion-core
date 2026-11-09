"""Bind public runtime archives to the original full run without a consumer build."""

import io
import json
import os
import shlex
import subprocess
import sys
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts import ci_container_artifacts as images
from scripts import ci_reuse
from tests.support.ci_container_artifacts import runtime_evidence, seal_metadata
from tests.support.ci_container_identity import oci_archive
from tests.support.ci_policy import commit, git


@pytest.fixture
def retained():
    return runtime_evidence({"commit": "a" * 40, "tree": "b" * 40,
                             "repository": "synthetic/repository", "run_id": 101, "attempt": 2})


def verify(state):
    return images.verified_metadata(
        state["reference"], state["artifacts"],
        lambda path, **_: state["payloads"][int(path.split("/")[-2])],
        "synthetic/repository", {"repository": "synthetic/repository", "commit": "a" * 40,
                                 "tree": "b" * 40, "run_id": 101, "attempt": 2},
    )


@pytest.mark.parametrize("fault", [
    "metadata-bytes", "archive-expired", "archive-retargeted", "missing-archive", "duplicate-id",
    "wrong-tree", "wrong-platform", "fake-registry-digest", "wrong-config-id", "traversal",
    "extra-component", "selected-source", "null-image-id", "ui-base",
])
def test_retained_metadata_rejects_tamper_incomplete_and_unbound_images(retained, fault):
    component = retained["metadata"]["components"]["core"]
    if fault == "metadata-bytes":
        retained["payloads"][301] += b"changed"
    elif fault == "archive-expired":
        retained["artifacts"][1]["expired"] = True
    elif fault == "archive-retargeted":
        retained["artifacts"][1]["digest"] = "sha256:" + "f" * 64
    elif fault == "missing-archive":
        retained["artifacts"].pop()
    elif fault == "duplicate-id":
        retained["artifacts"].append(deepcopy(retained["artifacts"][1]))
    else:
        if fault == "wrong-tree":
            component["tree"] = "c" * 40
        elif fault == "wrong-platform":
            component["platform"] = "linux/arm64"
        elif fault == "fake-registry-digest":
            component["registry_manifest_digest"] = component["image_id"]
        elif fault == "wrong-config-id":
            component["config_digest"] = "sha256:" + "f" * 64
        elif fault == "traversal":
            component["archive"]["filename"] = "../core.tar.gz"
        elif fault == "extra-component":
            retained["metadata"]["components"]["executor"] = deepcopy(component)
        elif fault == "selected-source":
            retained["metadata"]["run_id"] = 102
        elif fault == "null-image-id":
            component["image_id"] = None
        else:
            retained["metadata"]["components"]["ui"]["base_image"] = "unbound-base"
        seal_metadata(retained)
    with pytest.raises(ValueError, match="PUBLIC_IMAGE_"):
        verify(retained)


@pytest.mark.parametrize("unbound", [False, True])
def test_metadata_binds_containerd_producer_separately_from_portable_config(retained, unbound):
    _, identity = oci_archive("core")
    component = retained["metadata"]["components"]["core"]
    component.update(identity)
    component["image_id"] = ("sha256:" + "f" * 64 if unbound else
                             identity["archive_descriptor"]["digest"])
    seal_metadata(retained)
    if unbound:
        with pytest.raises(ValueError, match="COMPONENT_INVALID"):
            verify(retained)
    else:
        verified = verify(retained)["components"]["core"]
        assert verified["image_id"] != verified["config_digest"]
        assert verified["config_digest"] == identity["config_digest"]


def test_consumer_checks_hashes_then_loads_exact_id_without_build(retained, tmp_path):
    component = verify(retained)["components"]["core"]
    calls = []

    def runner(arguments, *, output=None):
        calls.append(arguments)
        if arguments[0] == "gh":
            output.write(retained["payloads"][302])
            return None
        if arguments[:3] == ["docker", "image", "load"]:
            return b"loaded"
        if arguments[:3] == ["docker", "image", "ls"]:
            return (component["image_id"] + "\n").encode()
        assert arguments == ["docker", "image", "inspect", component["image_id"]]
        return json.dumps([{"Id": component["image_id"], "Os": "linux",
                            "Architecture": "amd64", "RootFS": {
                                "Layers": component["rootfs_diff_ids"]}}]).encode()

    archive, seconds = images.download_archive("synthetic/repository", component, tmp_path,
                                                runner=runner)
    loaded = images.load_archive(component, archive, runner=runner)
    assert seconds >= 0 and loaded["consumer_local_image_id"] == component["image_id"]
    assert len(calls) == 4 and not any("build" in call for call in calls)
    archive.write_bytes(archive.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="ARCHIVE_DIGEST_CHANGED"):
        images.load_archive(component, archive, runner=runner)
    assert len(calls) == 4  # A bad archive never reaches the daemon.


@pytest.mark.parametrize("fault", ["zip-digest", "loaded-id", "loaded-platform"])
def test_consumer_fails_closed_without_a_rebuild_fallback(retained, tmp_path, fault):
    component = verify(retained)["components"]["core"]
    calls = []

    def runner(arguments, *, output=None):
        calls.append(arguments)
        if arguments[0] == "gh":
            output.write(retained["payloads"][302] + (b"changed" if fault == "zip-digest" else b""))
        elif "load" in arguments:
            return b"loaded"
        elif "ls" in arguments:
            return (component["image_id"] + "\n").encode()
        else:
            return json.dumps([{"Id": "sha256:" + "f" * 64 if fault == "loaded-id"
                               else component["image_id"], "Os": "linux",
                               "Architecture": "arm64" if fault == "loaded-platform"
                               else "amd64", "RootFS": {
                                   "Layers": component["rootfs_diff_ids"]}}]).encode()

    with pytest.raises(ValueError, match="DIGEST_CHANGED|IDENTITY_CHANGED"):
        archive, _ = images.download_archive("synthetic/repository", component, tmp_path,
                                             runner=runner)
        images.load_archive(component, archive, runner=runner)
    assert not any("build" in call for call in calls)


@pytest.fixture
def public_repository(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    for spec in images.COMPONENTS.values():
        recipe = root / spec["recipe"]
        recipe.parent.mkdir(parents=True, exist_ok=True)
        if spec is images.COMPONENTS["core"]:
            recipe.write_bytes((images.ROOT / "Dockerfile").read_bytes())
            continue
        recipe.write_text("FROM " + spec.get("base", "synthetic") + "\nCOPY "
                          + " ".join(sorted(spec["copies"])) + " /app\n")
    # Source-shaped files suffice; the test exercises Git archive and COPY boundaries.
    for spec in images.COMPONENTS.values():
        for name in spec["copies"]:
            path = root / name
            if not path.suffix:
                path.mkdir(parents=True, exist_ok=True)
                (path / "public.txt").write_text("public input")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("public input")
    (root / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    (root / ".dockerignore").write_text(".env\n", encoding="utf-8")
    (root / "deploy/compose.onebot-build.yaml").write_text("services: {}\n", encoding="utf-8")
    (root / "migrations/versions").mkdir(parents=True)
    (root / "migrations/versions/public.py").write_text("# synthetic migration\n", encoding="utf-8")
    commit(root)
    return root


@pytest.mark.parametrize("source_root", ["source_reader", "companion_meme_sources"])
def test_committed_context_excludes_dirty_private_inputs_and_rejects_copy_expansion(
    tmp_path, public_repository, source_root,
):
    root = public_repository
    (root / "config.toml").write_bytes(images.CANARY)
    (root / ".env").write_bytes(images.CANARY)
    context = tmp_path / "public" / "source"
    images.prepare_context(context, root=root)
    assert not (context / "config.toml").exists() and not (context / ".env").exists()
    canary = context / source_root / "public.txt"
    canary.write_bytes(images.CANARY)
    with pytest.raises(ValueError, match="PACKAGING_CANARY"):
        images.check_public_inputs(context)
    canary.unlink()
    (context / "Dockerfile").write_text("FROM synthetic\nCOPY . /app\n")
    with pytest.raises(ValueError, match="COPY_INPUTS_CHANGED"):
        images.check_public_inputs(context)


@pytest.mark.parametrize("value", [
    "private.key", "config.toml", "gateway.toml", ".env.production", "dump.sqlite3",
    "client.pem", "runtime.log",
    "keys/nested.txt", "CONFIG.TOML", "__pycache__/module.pyc",
])
@pytest.mark.parametrize("source_root", ["source_reader", "companion_meme_sources"])
def test_public_copy_inputs_reject_nested_private_files(tmp_path, value, source_root):
    # Use the actual reviewed recipes and all inputs without invoking Docker.
    for spec in images.COMPONENTS.values():
        recipe = tmp_path / spec["recipe"]
        recipe.parent.mkdir(parents=True, exist_ok=True)
        recipe.write_bytes((images.ROOT / spec["recipe"]).read_bytes())
        for name in spec["copies"]:
            path = tmp_path / name
            if Path(name).suffix:
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text("public")
            else:
                path.mkdir(parents=True, exist_ok=True)
    private = tmp_path / source_root / value
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text("private")
    with pytest.raises(ValueError, match="PRIVATE_INPUT"):
        images.check_public_inputs(tmp_path)


def test_real_recipes_allow_shared_sources_only_in_core_and_bridge():
    images.check_public_inputs()
    assert {name for name, spec in images.COMPONENTS.items()
            if "companion_meme_sources" in spec["copies"]} == {"core", "bridge"}


def test_workflow_precompilation_keeps_bytecode_outside_public_inputs(public_repository):
    root = public_repository
    workflow = yaml.safe_load((images.ROOT / ".github/workflows/ci.yml").read_text("utf-8"))
    job = workflow["jobs"]["test"]
    cache = Path(job["env"]["PYTHONPYCACHEPREFIX"].replace("${{ github.workspace }}", str(root)))
    assert cache.is_relative_to(root / ".local")
    step = next(step for step in job["steps"]
                if step.get("name") == "Precompile repeated worker imports")
    command = shlex.split(step["run"])
    options = command[command.index("python") + 1:command.index("companion")]
    source = root / "companion_meme_sources" / "bytecode_probe.py"
    source.write_text("value = 41\n")
    env = {**os.environ, "PYTHONPYCACHEPREFIX": str(cache), "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, "-B", *options, str(source)],
                   cwd=root, env=env, check=True, capture_output=True, timeout=30)
    assert list(cache.rglob("*.pyc"))
    assert not list(source.parent.rglob("*.pyc"))
    images.check_public_inputs(root)
    # Same size and timestamp must not let a child read stale precompiled code.
    stat = source.stat()
    source.write_text("value = 42\n")
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    probe = "import bytecode_probe; assert bytecode_probe.value == 42"
    subprocess.run([sys.executable, "-B", "-c", probe],
                   cwd=source.parent, env=env, check=True, capture_output=True, timeout=30)
    images.check_public_inputs(root)


@pytest.mark.parametrize("fault", ["env", "history", "platform"])
def test_image_privacy_validation_rejects_credential_canaries_without_echo(fault, capsys):
    canary = "SYNTHETIC_PRIVATE_VALUE_DO_NOT_ECHO"
    document = {"Id": "sha256:" + "a" * 64, "Os": "linux", "Architecture": "amd64",
                "Config": {"Env": ["PYTHON_VERSION=3.12"]}}
    if fault == "env":
        document["Config"]["Env"].append("COMPANION_TOKEN=" + canary)
    if fault == "platform":
        document["Architecture"] = "arm64"

    def runner(arguments):
        return (json.dumps([document]).encode() if "inspect" in arguments else
                ("RUN password=" + canary if fault == "history" else "RUN public").encode())

    with pytest.raises(ValueError, match="PUBLIC_IMAGE_") as error:
        images.inspect_image("core", runner=runner)
    assert canary not in str(error.value) + capsys.readouterr().out


@pytest.mark.parametrize("fault", [None, "unknown-key", "changed-value", "python-env"])
def test_ui_capture_uses_only_the_reviewed_official_base_environment(fault):
    environment = [key + "=" + value for key, value in images.UI_BASE_ENV.items()]
    if fault == "unknown-key":
        environment.append("PRIVATE_TOKEN=SYNTHETIC_PRIVATE_VALUE")
    elif fault == "changed-value":
        environment = [v.replace("NGINX_VERSION=1.31.8", "NGINX_VERSION=0") for v in environment]
    elif fault == "python-env":
        environment.append("PYTHON_VERSION=3.12")
    image_id = "sha256:" + "a" * 64

    def runner(arguments):
        return (json.dumps([{"Id": image_id, "Os": "linux", "Architecture": "amd64",
                             "Size": 1000, "Config": {"Env": environment}}]).encode()
                if "inspect" in arguments else b"RUN public base history")

    if fault:
        with pytest.raises(ValueError, match="ENV_NOT_ALLOWLISTED|BASE_ENV_CHANGED"):
            images.capture("ui", runner=runner, identity={"commit": "a", "tree": "b"})
    else:
        result = images.capture("ui", runner=runner, identity={"commit": "a", "tree": "b"})
        assert result["image_id"] == image_id
        assert result["base_image"] == images.COMPONENTS["ui"]["base"]
        assert result["dependency_lock_sha256"] is None
        assert result["registry_manifest_digest"] is None


def test_ui_environment_does_not_widen_python_image_privacy_allowlist():
    def runner(_):
        return json.dumps([{"Id": "sha256:" + "a" * 64, "Os": "linux", "Architecture": "amd64",
                            "Config": {"Env": ["NGINX_VERSION=1.31.8"]}}]).encode()

    with pytest.raises(ValueError, match="ENV_NOT_ALLOWLISTED"):
        images.inspect_image("core", runner=runner)


@pytest.mark.parametrize("original,replacement", [
    (images.POSTGRES_IMAGE, "postgres:latest"),
    ("AS postgres-client", "AS postgres-client\nRUN touch /usr/local/new-client"),
    ("--from=postgres-client", "--from=unknown"),
    ("/usr/lib/postgresql/16/bin/pg_dump", "/usr/bin/extra"),
    ("/usr/local/lib/postgresql/", "/app/companion/"),
])
def test_postgres_client_stage_rejects_changed_provenance_and_copy_bounds(
    public_repository, original, replacement,
):
    path = public_repository / "Dockerfile"
    recipe = path.read_text()
    assert original in recipe
    path.write_text(recipe.replace(original, replacement, 1))
    with pytest.raises(ValueError, match="PG_CLIENT_|COPY_SHAPE_CHANGED"):
        images.check_public_inputs(public_repository)


@pytest.mark.parametrize("fault", ["base", "copy"])
def test_ui_public_context_rejects_retargeted_base_and_added_private_copy(tmp_path, fault):
    context = tmp_path / "public"
    images.prepare_context(context)
    recipe = context / images.COMPONENTS["ui"]["recipe"]
    text = recipe.read_text("utf-8")
    changed = (text.replace(images.COMPONENTS["ui"]["base"], "nginx:latest")
               if fault == "base" else text + "\nCOPY config.toml /secret\n")
    recipe.write_text(changed, encoding="utf-8")
    with pytest.raises(ValueError, match="UI_BASE_CHANGED|COPY_INPUTS_CHANGED"):
        images.check_public_inputs(context)


def test_export_captures_validated_image_ids_and_actual_archive_sizes(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "source_identity", lambda _: {"commit": "a", "tree": "b"})
    archives = {name: oci_archive(name) for name in images.COMPONENTS}
    captured = {name: {"component": name, "commit": "a", "tree": "b",
                       "image_id": identity["archive_descriptor"]["digest"],
                       "rootfs_diff_ids": identity["rootfs_diff_ids"], "platform": "linux/amd64"}
                for name, (_, identity) in archives.items()}
    monkeypatch.setattr(images, "inspect_image", lambda name, **_: {
        "Id": captured[name]["image_id"]})
    calls = []

    def save(arguments):
        calls.append(arguments)
        assert arguments[:3] == ["docker", "image", "save"]
        name, = [name for name, built in captured.items() if built["image_id"] == arguments[-1]]
        Path(arguments[4]).write_bytes(archives[name][0])

    metadata = images.export_images(captured, tmp_path, runner=save)
    assert len(calls) == len(images.COMPONENTS) and all(call[-1] == captured[name]["image_id"]
                                   for call, name in zip(calls, captured, strict=True))
    for name, component in metadata["components"].items():
        compressed = tmp_path / (name + ".tar.gz")
        assert component["archive"]["bytes"] == compressed.stat().st_size
        assert component["archive"]["uncompressed_bytes"] == len(archives[name][0])
        assert component["config_digest"] == archives[name][1]["config_digest"]
        assert component["config_digest"] != component["image_id"]
        assert not (tmp_path / (name + ".tar")).exists()
    monkeypatch.setattr(images, "inspect_image", lambda name, **_: {"Id": "changed-after-smoke"})
    with pytest.raises(ValueError, match="CHANGED_AFTER_VALIDATION"):
        images.export_images(captured, tmp_path, runner=save)
    assert len(calls) == len(images.COMPONENTS)


def test_local_load_checks_all_exported_archives_before_upload(retained, tmp_path):
    images.write_json(tmp_path / images.MANIFEST, retained["metadata"])
    for name, component in retained["metadata"]["components"].items():
        payload = retained["payloads"][component["artifact"]["id"]]
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            (tmp_path / (name + ".tar.gz")).write_bytes(archive.read(name + ".tar.gz"))
    calls = []

    def runner(arguments):
        calls.append(arguments)
        if "load" in arguments:
            return b"loaded"
        if "ls" in arguments:
            return ("\n".join(c["image_id"] for c in retained["metadata"]["components"].values())
                    + "\n").encode()
        assert arguments[:3] == ["docker", "image", "inspect"]
        component, = [c for c in retained["metadata"]["components"].values()
                      if c["image_id"] == arguments[-1]]
        return json.dumps([{"Id": arguments[-1], "Os": "linux", "Architecture": "amd64",
                            "RootFS": {"Layers": component["rootfs_diff_ids"]}}]).encode()

    result = images.load_local(tmp_path / images.MANIFEST, runner=runner)
    assert set(result) == set(images.COMPONENTS) and len(calls) == 3 * len(images.COMPONENTS)
    assert not any("build" in command for command in calls)
    assert json.loads((tmp_path / "local-load.json").read_text())["build_performed"] is False
    assert all(row["action"] == "loaded" for row in result.values())


def test_resume_keeps_existing_core_and_loads_only_three_missing_images(retained, tmp_path):
    images.write_json(tmp_path / images.MANIFEST, retained["metadata"])
    components = retained["metadata"]["components"]
    identities = {name: oci_archive(name)[1] for name in components}
    native_ids = {name: identity["archive_descriptor"]["digest"]
                  for name, identity in identities.items()}
    present, loaded = {native_ids["core"]}, []
    for name, component in components.items():
        data = retained["payloads"][component["artifact"]["id"]]
        with zipfile.ZipFile(io.BytesIO(data)) as payload:
            (tmp_path / (name + ".tar.gz")).write_bytes(payload.read(name + ".tar.gz"))

    def runner(command):
        if "ls" in command:
            return ("\n".join(sorted(present)) + "\n").encode()
        if "load" in command:
            name = Path(command[-1]).name.split(".")[0]
            assert name != "core"
            loaded.append(name)
            present.add(native_ids[name])
            return b"loaded"
        name, = [name for name, value in native_ids.items() if value == command[-1]]
        return json.dumps([{"Id": native_ids[name], "Os": "linux", "Architecture": "amd64",
            "RootFS": {"Layers": components[name]["rootfs_diff_ids"]},
            "Descriptor": identities[name]["archive_descriptor"]}]).encode()

    before = (tmp_path / images.MANIFEST).read_bytes()
    result = images.load_local(tmp_path / images.MANIFEST, runner=runner, reuse_existing=True)
    assert result["core"]["action"] == "already_present"
    assert loaded == [name for name in components if name != "core"]
    assert all(row["consumer_local_image_id"] == native_ids[name] for name, row in result.items())
    assert all("image_id" not in row for row in result.values())
    assert (tmp_path / images.MANIFEST).read_bytes() == before


def test_build_uses_public_identity_and_native_compose_cache_without_second_build(
    tmp_path, monkeypatch, public_repository,
):
    context, cache = tmp_path / "public", tmp_path / "cache"
    source = images.prepare_context(context, root=public_repository)
    calls = []

    def runner(arguments):
        calls.append(arguments)
        images.write_json(cache / "bridge-next/index.json", {"cache": "synthetic"})

    def capture(name, *, root, runner, identity):
        assert name == "bridge" and root == context and identity == source
        return {**identity, "image_id": "sha256:" + "a" * 64}

    monkeypatch.setattr(images, "capture", capture)
    result = images.build_component("bridge", context, cache, root=public_repository, runner=runner)
    assert len(calls) == 1 and calls[0][:2] == ["docker", "compose"]
    assert str(context / "deploy/compose.onebot-build.yaml") in calls[0]
    assert calls[0][-4:] == ["build", "--builder", "companion-ci", "onebot-bridge"]
    assert result["dependency_cache"]["configured_import_index_sha256"] is None
    images.build_component("bridge", context, cache, root=public_repository, runner=runner)
    override = json.loads((tmp_path / "bridge-cache.json").read_text())
    assert override["services"]["onebot-bridge"]["build"]["cache_from"] == [
        "type=local,src=" + str(cache / "bridge")]
    assert len(calls) == 2


@pytest.mark.parametrize("fault", [
    "content", "canary", "added-public", "added-private", "deleted", "recipe", "dockerignore",
    "compose", "extra-directory", "attributes",
])
def test_build_entry_rejects_changed_prepared_context_before_docker(
    tmp_path, public_repository, fault,
):
    context = tmp_path / "prepared source"
    images.prepare_context(context, root=public_repository)
    metadata = (context.parent / "public-context.json").read_bytes()
    if fault in {"content", "canary"}:
        (context / "companion/__init__.py").write_bytes(
            images.CANARY if fault == "canary" else b"arbitrary changed public content")
    elif fault in {"added-public", "added-private"}:
        (context / "source_reader" / ("extra.py" if fault == "added-public" else "secret.key"))\
            .write_text("synthetic additional file", encoding="utf-8")
    elif fault == "deleted":
        (context / "companion/__init__.py").unlink()
    elif fault == "recipe":
        recipe = context / "Dockerfile"
        recipe.write_text(recipe.read_text() + "\nRUN echo arbitrary_recipe_change\n")
    elif fault == "dockerignore":
        (context / ".dockerignore").write_text("different context ignore rules\n")
    elif fault == "compose":
        (context / "deploy/compose.onebot-build.yaml").write_text("different compose input\n")
    elif fault == "attributes":
        (context / ".gitattributes").write_text("* text eol=crlf\n")
    else:
        (context / "companion/added-directory").mkdir()
    calls = []

    def runner(arguments):
        calls.append(arguments)
        raise AssertionError("Docker must not be reached")

    assert (context.parent / "public-context.json").read_bytes() == metadata
    with pytest.raises(ValueError, match="PUBLIC_IMAGE_CONTEXT_"):
        images.build_component("core", context, tmp_path / "cache",
                               root=public_repository, runner=runner)
    assert calls == []


@pytest.mark.parametrize("name", images.COMPONENTS)
@pytest.mark.parametrize("owner_only_directory", [False, True])
def test_build_entry_accepts_committed_context_and_owner_only_directory(
    tmp_path, public_repository, name, owner_only_directory,
):
    context, cache = tmp_path / "prepared source", tmp_path / "cache"
    images.prepare_context(context, root=public_repository)
    if owner_only_directory:
        (context / "migrations/versions").chmod(0o700)
    calls = []

    class BuildReached(Exception):
        pass

    def runner(arguments):
        calls.append(arguments)
        raise BuildReached

    with pytest.raises(BuildReached):
        images.build_component(name, context, cache, root=public_repository, runner=runner)
    assert len(calls) == 1 and calls[0][0] == "docker"


@pytest.mark.parametrize("endpoint", ["ssh://host", "tcp://127.0.0.1:2375", "unix://relative"])
def test_daemon_boundary_never_uses_a_remote_context(monkeypatch, endpoint):
    images.local_context.cache_clear()
    monkeypatch.setattr(images.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout=json.dumps(endpoint).encode()))
    try:
        with pytest.raises(ValueError, match="LOCAL_CONTEXT_REQUIRED"):
            images.local_context()
    finally:
        images.local_context.cache_clear()


def test_workflow_exports_only_after_all_existing_smokes_and_never_publishes_from_pr():
    workflow = yaml.load((images.ROOT / ci_reuse.WORKFLOW).read_text(), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    assert workflow["permissions"] == {"contents": "read"}
    assert all("packages" not in job.get("permissions", {}) for job in jobs.values())
    steps = jobs["container"]["steps"]
    commands = [step.get("run", "") for step in steps]
    exported = next(i for i, command in enumerate(commands) if "artifacts.py export " in command)
    loaded = next(i for i, command in enumerate(commands) if "artifacts.py load " in command)
    assert exported < loaded
    assert all(i < exported for i, command in enumerate(commands)
               if "docker run" in command or "smoke_" in command)
    assert any("smoke_dev_compose --context default --image" in c for c in commands)
    assert any("deploy/executor.Dockerfile" in c for c in commands)
    assert any("deploy/file-parser.Dockerfile" in c for c in commands)
    for name in images.COMPONENTS:
        assert sum(f"artifacts.py build {name} " in command for command in commands) == 1
        upload = next(step for step in steps if step.get("id") == name + "-image")
        assert loaded < steps.index(upload)
        assert upload["if"] == "${{ needs.preflight.outputs.profile == 'full' }}"
        assert upload["with"]["path"] == f".local/container-images/{name}.tar.gz"
        assert upload["with"]["compression-level"] == "0"
    assert "images-artifact-id" in jobs["container"]["outputs"]
    assert jobs["ci"]["env"]["CI_IMAGES_ARTIFACT_ID"] == (
        "${{ needs.container.outputs.images-artifact-id }}")
