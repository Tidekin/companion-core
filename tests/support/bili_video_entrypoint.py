import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

from tests.support.bili_media import NAV, PLAY, VIEW
from tests.support.source_reader import PLAYER, response
from tests.support.video_extract import local_media

BVID = "BV1xx411c7mD"


URL = "https://www.bilibili.com/video/" + BVID


TOKEN = "synthetic-bili-reader-token-123456"


READER = r'''
import base64, importlib.abc, json, runpy, ssl, sys
from datetime import datetime
sys.path.insert(0, sys.argv[1])
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sqlite3', 'fastapi', 'pydantic', 'alembic', 'fastembed'}:
            raise ImportError('FORBIDDEN_READER_DEPENDENCY:' + fullname)
        if fullname.startswith('companion.') and fullname not in {
            'companion.source_policy', 'companion.video_media_contract',
            'companion.bili_video_protocol',
        }:
            raise ImportError('FORBIDDEN_CORE_MODULE:' + fullname)
sys.meta_path.insert(0, Boundary())
import httpcore
from source_reader import worker_v1
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP, PublicNetwork
value = json.loads(sys.stdin.read())
native_reader = BilibiliReader.__init__
def initialize_reader(self, *args, **kwargs):
    native_reader(self, *args, clock=lambda: value['now'], **kwargs)
BilibiliReader.__init__ = initialize_reader
rows = [base64.b64decode(row) for row in value['responses']]
seen = []
class Stream(httpcore.AsyncMockStream):
    async def write(self, buffer, timeout=None):
        if buffer:
            assert b'authorization:' not in buffer.lower()
            assert b'cookie:' not in buffer.lower()
            seen.append(buffer.decode('latin1'))
    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        assert ssl_context.check_hostname
        return self
class Backend(httpcore.AsyncNetworkBackend):
    async def connect_tcp(self, host, port, **kwargs):
        assert host == '93.184.216.34' and port == 443
        return Stream([rows.pop(0)])
async def addresses(host, port):
    assert host in {'api.bilibili.com', 'aisubtitle.hdslb.com', 'cn-test.bilivideo.com'}
    return ['93.184.216.34']
original = PublicHTTP.__init__
def init(self, pool=None):
    original(self, httpcore.AsyncConnectionPool(
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        network_backend=PublicNetwork(resolver=addresses, backend=Backend()),
        max_keepalive_connections=0, retries=0,
    ))
PublicHTTP.__init__ = init
native_tick = worker_v1.tick
async def tick(*args, **kwargs):
    return await native_tick(*args, clock=lambda: datetime.fromisoformat(value['now']), **kwargs)
worker_v1.tick = tick
sys.argv = ['source_reader', '--source', 'bilibili', '--protocol', 'reading-v1',
            '--core-url', sys.argv[2], '--adapter-id', 'bili', '--once',
            '--video-media' if value['media'] else '--discussion']
try:
    runpy.run_module('source_reader', run_name='__main__')
finally:
    print(json.dumps({'requests': seen, 'remaining': len(rows)}))
'''


def reader_files(tmp_path):
    source = Path(__file__).resolve().parents[2]
    isolated = tmp_path / "reader"
    shutil.copytree(source / "source_reader", isolated / "source_reader",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (isolated / "companion").mkdir()
    for filename in ("__init__.py", "source_policy.py", "video_media_contract.py",
                     "bili_video_protocol.py"):
        shutil.copy2(source / "companion" / filename, isolated / "companion" / filename)
    return isolated


async def reader_once(root, origin, clock, rows, *, media=False):
    work = root / "owned-temp"
    work.mkdir(exist_ok=True)
    completed = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-I", "-B", "-c", READER, str(root), origin],
        cwd=root, input=json.dumps({"responses": [base64.b64encode(row).decode() for row in rows],
                                   "now": clock(), "media": media}),
        env={**os.environ, "COMPANION_READER_TOKEN": TOKEN, "TMPDIR": str(work)},
        text=True, capture_output=True, timeout=45, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert list(work.iterdir()) == [], completed.stdout + completed.stderr
    return json.loads(completed.stdout.splitlines()[-1])


def upstream(tmp_path):
    local_media(tmp_path)
    view = {"code": 0, "data": {**VIEW["data"], "bvid": BVID, "aid": 1701,
                                "title": "Synthetic star colors"}}
    subtitle = {"body": [{"from": 0, "to": 2.5, "content": "字幕说恒星颜色与温度有关。"}]}
    comments = {"code": 0, "data": {"page": {"count": 20}, "replies": [{
        "rpid": 101, "floor": 1, "member": {"uname": "synthetic reader"},
        "content": {"message": "评论资料：我喜欢蓝色。"},
    }]}}
    danmaku = '<i><chatid>123</chatid><d p="1.25,1,25,0,0,0,x,1">弹幕资料</d></i>'
    return (
        [response(value) for value in (view, PLAYER, subtitle, comments)]
        + [response(zlib.compress(danmaku.encode()))],
        [response(view), response(NAV), response(PLAY),
         response((tmp_path / "source.mp4").read_bytes())],
    )
