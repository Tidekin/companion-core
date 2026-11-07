import hashlib
import io
import json
import zipfile

from tests.support.next_memes_catalog import png

REVISION = "a" * 40


PACK = {
    "id": "fixture-pack", "description": "celebrate 庆祝 成功", "tags": ["happy"],
    "license": "CC0-1.0", "verified": False,
    "source": {"type": "github", "repo": "example/fixture", "ref": "main", "subpath": "."},
}


def archive(*, semantic=False, extra=None, mutate=None, subpath=""):
    image = png()
    digest = hashlib.sha256(image).hexdigest()
    path = "memes/happy/a.png"
    entry_id = hashlib.sha256((digest + "\0happy\0" + path).encode()).hexdigest()
    files = {
        "manifest.json": json.dumps({
            "schema_version": 1, "id": "fixture-pack", "name": "Fixture", "version": "1.0",
            "categories": {"happy": {"description": "庆祝 celebrate 成功"}},
        }).encode(),
        path: image,
        "run.py": b"raise AssertionError('never execute downloaded scripts')",
    }
    if semantic:
        value = {"schema_version": "2.0", "pack_id": "fixture-pack", "images": {
            entry_id: {"entry_id": entry_id, "content_sha256": digest, "relative_path": path,
                       "category": "happy", "caption": "庆祝 celebrate 开心的绿色图片",
                       "caption_status": "done", "tags": ["celebrate"]},
        }}
        if mutate:
            mutate(value["images"][entry_id])
        files["semantic_metadata.json"] = json.dumps(value, ensure_ascii=False).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for name, data in files.items():
            zip_file.writestr("fixture-" + REVISION + "/" + subpath + name, data)
        if extra:
            zip_file.writestr(*extra)
    return stream.getvalue()
