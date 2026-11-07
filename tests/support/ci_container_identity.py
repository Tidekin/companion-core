import gzip
import hashlib
import io
import json
import tarfile
from copy import deepcopy


def oci_archive(name="core", *, nested=False, architecture="amd64", fault=None, compressed=False):
    blobs = {}

    def blob(value, media_type):
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        blobs["blobs/sha256/" + digest[7:]] = data
        return {"mediaType": media_type, "digest": digest, "size": len(data)}

    layer_bytes = b"SYNTHETIC_LAYER_BYTES"
    diff_id = "sha256:" + hashlib.sha256(layer_bytes).hexdigest()
    layer = blob(gzip.compress(layer_bytes) if compressed else layer_bytes,
                 "application/vnd.oci.image.layer.v1.tar" + ("+gzip" if compressed else ""))
    if fault == "layer-diffid":
        diff_id = "sha256:" + "f" * 64
    config = blob({"os": "linux", "architecture": architecture,
        "config": {"Labels": {"synthetic.component": name}},
        "rootfs": {"type": "layers", "diff_ids": [diff_id]}},
        "application/vnd.oci.image.config.v1+json")
    manifest = blob({"schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": config, "layers": [layer]}, "application/vnd.oci.image.manifest.v1+json")
    root = manifest
    if nested:
        root = blob({"schemaVersion": 2,
            "mediaType": "wrong" if fault == "index-media-type" else
                "application/vnd.oci.image.index.v1+json", "manifests": [manifest]},
            "application/vnd.oci.image.index.v1+json")
    index = {"schemaVersion": 2, "manifests": [deepcopy(root)]}
    if fault == "descriptor-size":
        index["manifests"][0]["size"] += 1
    elif fault == "descriptor-digest":
        index["manifests"][0]["digest"] = "sha256:" + "f" * 64
    elif fault == "ambiguous":
        index["manifests"].append(deepcopy(root))
    elif fault == "extra-platform":
        index["manifests"][0]["platform"] = {"os": "linux", "architecture": "arm64"}
    elif fault == "blob-bytes":
        blobs["blobs/sha256/" + config["digest"][7:]] += b" "
    files = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
             "index.json": json.dumps(index).encode(),
             "manifest.json": json.dumps([{
                 "Config": "blobs/sha256/" + config["digest"][7:], "Layers": [
                     "blobs/sha256/" + layer["digest"][7:]]}]).encode(), **blobs}
    if fault == "docker-config":
        alternate = json.loads(blobs["blobs/sha256/" + config["digest"][7:]])
        alternate["config"]["Labels"]["synthetic.component"] = "other-config-same-rootfs"
        other = blob(alternate, "application/vnd.oci.image.config.v1+json")
        files.update(blobs)
        files["manifest.json"] = json.dumps([{"Config": "blobs/sha256/" + other["digest"][7:],
            "Layers": ["blobs/sha256/" + layer["digest"][7:]]}]).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for path, data in files.items():
            if fault == "empty-duplicate" and path == "index.json":
                archive.addfile(tarfile.TarInfo(path), io.BytesIO())
            entry = tarfile.TarInfo(path)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return output.getvalue(), {"config_digest": config["digest"],
        "archive_descriptor": root, "platform_manifest_descriptor": manifest,
        "rootfs_diff_ids": [diff_id]}
