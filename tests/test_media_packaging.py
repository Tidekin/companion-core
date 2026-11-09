"""Exercise actual image source contents without a GPU stack or external I/O."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("image_kind", ["core", "bridge"])
def test_images_import_media_routes_from_packaged_sources_without_external_io(tmp_path, image_kind):
    repository = Path(__file__).resolve().parents[1]
    image = tmp_path / f"{image_kind}-image"
    image.mkdir()
    # Build the source tree from the actual Docker COPY instructions. Dependencies
    # come from the test environment; an editable checkout cannot fill a missing copy.
    docker_path = "Dockerfile" if image_kind == "core" else "deploy/onebot-bridge.Dockerfile"
    dockerfile = (repository / docker_path).read_text("utf-8").replace("\\\n", " ")
    for instruction in dockerfile.splitlines():
        if not instruction.startswith("COPY "):
            continue
        fields = shlex.split(instruction)[1:]
        if fields[0] == "--from=postgres-client":
            # This stage supplies native clients and libraries, not Python sources.
            continue
        *sources, destination = fields
        assert not any(source.startswith("--") for source in sources)
        target = image / destination
        assert target.resolve().is_relative_to(image)
        for name in sources:
            source = repository / name
            assert source.resolve().is_relative_to(repository)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__"))
    program = r'''
import importlib.abc
import importlib.machinery
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
image_kind = sys.argv[2]
sys.path.insert(0, str(root))
project = {'companion', 'source_reader', 'companion_onebot_bridge',
           'companion_meme_protocol', 'companion_meme_sources',
           'companion_voice_node', 'companion_delivery_protocol'}
gpu = {'torch', 'faster_whisper', 'ctranslate2'}
forbidden_packages = gpu | ({'companion', 'source_reader', 'companion_voice_node'}
                            if image_kind == 'bridge' else set())
assert not gpu.intersection(sys.modules)
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        package = fullname.split('.')[0]
        if package in forbidden_packages:
            raise AssertionError('IMAGE_FORBIDDEN_IMPORT:' + fullname)
        if package in project:
            spec = importlib.machinery.PathFinder.find_spec(
                fullname, [str(root)] if path is None else path)
            if (spec is None or spec.origin is None
                or not Path(spec.origin).resolve().is_relative_to(root)):
                raise ImportError('SOURCE_NOT_IN_IMAGE:' + fullname)
            return spec
sys.meta_path.insert(0, Boundary())
def forbidden(*args, **kwargs):
    raise AssertionError('IMAGE_IMPORT_ATTEMPTED_EXTERNAL_IO')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
socket.getaddrinfo = forbidden
class NoProcesses(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        forbidden()
subprocess.Popen = NoProcesses
os.system = forbidden

from companion_meme_sources import community, fetch, review
assert community.CommunityPacks and fetch.PublicFetcher and review.TrustedReviewer

if image_kind == 'core':
    from companion.api import create_app
    from companion.config import Settings
    from companion.core import Core
    from companion.voice_jobs import VoiceJobs
    settings = Settings(owner_qq_id='10001', bot_qq_id='10002',
                        bridge_token='synthetic-image-import-token-0001',
                        database='dbname=synthetic',
                        state_directory=str(root / 'never-created-state'),
                        erasure_ledger=str(root / 'never-created.jsonl'))
    app = create_app(settings)
    assert Core and VoiceJobs and app.routes
    assert not settings.voice.enabled
    assert not Path(settings.state_directory).exists()
    assert not Path(settings.erasure_ledger).exists()
else:
    from companion_onebot_bridge import meme_runtime, network, owner_resolution, voice_runtime
    from companion_onebot_bridge.window import TestWindow
    assert meme_runtime and network and owner_resolution and voice_runtime and TestWindow
assert not forbidden_packages.intersection(sys.modules)
print(json.dumps({'image': image_kind, 'media_imports': True,
                  'gpu_packages_loaded': False, 'external_io': False}))
'''
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("COMPANION_")}
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(image), image_kind],
        cwd=image, env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"gpu_packages_loaded": false' in result.stdout
