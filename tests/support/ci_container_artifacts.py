import gzip
import hashlib
import io
import json
import zipfile

from scripts import ci_container_artifacts as images
from tests.support.ci_container_identity import oci_archive


def zipped(name, payload):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, payload)
    return output.getvalue()


def seal_metadata(state):
    payload = zipped(images.MANIFEST, json.dumps(state["metadata"]))
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    state["reference"]["digest"] = digest
    state["artifacts"][0]["digest"] = digest
    state["artifacts"][0]["size_in_bytes"] = len(payload)
    state["payloads"][301] = payload


def runtime_evidence(record):
    """Small synthetic API artifacts; Docker transport is mocked in these tests."""
    source = {key: record[key] for key in ("commit", "tree", "repository", "run_id", "attempt")}
    state = {"metadata": {"kind": images.KIND, **source, "components": {}},
             "reference": {"id": 301, "name": "ci-container-images-2", "digest": ""},
             "artifacts": [{"id": 301, "name": "ci-container-images-2", "expired": False}],
             "payloads": {}}
    for number, name in enumerate(images.COMPONENTS, 302):
        raw, identity = oci_archive(name)
        data = gzip.compress(raw)
        payload = zipped(name + ".tar.gz", data)
        ref = {"id": number, "name": f"runtime-image-{name}-2",
               "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}
        state["artifacts"].append({**ref, "expired": False, "size_in_bytes": len(payload)})
        state["payloads"][number] = payload
        state["metadata"]["components"][name] = {
            "component": name, "commit": record["commit"], "tree": record["tree"],
            "image_id": identity["config_digest"],
            "config_digest": identity["config_digest"],
            "rootfs_diff_ids": identity["rootfs_diff_ids"],
            "platform": "linux/amd64", "registry_manifest_digest": None, "artifact": ref,
            "base_image": images.COMPONENTS[name].get("base"),
            "dependency_lock_sha256": None if name == "ui" else "a" * 64,
            "archive": {"filename": name + ".tar.gz", "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()},
        }
    seal_metadata(state)
    return state
