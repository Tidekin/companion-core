"""Run the shipped Nginx configurations on Windows and Linux; no proxy substitutes."""

import asyncio
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
from contextlib import ExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx

from tests.support.voice_node_api import tls_core

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "companion/console_static"


def quote_paths(config):
    # Windows workspaces may contain spaces; preserve the shipped directives.
    return re.sub(r"(?m)^(\s*(?:pid|root|[a-z_]+_temp_path|ssl_certificate(?:_key)?|include)\s+)"
                  r"([^;]+);", lambda m: m[1] + '"' + m[2] + '";', config)


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def nginx(config, directory):
    binary = os.environ.get("COMPANION_TEST_NGINX") or shutil.which("nginx")
    if not binary:
        raise RuntimeError("Console proxy checks require Nginx: set COMPANION_TEST_NGINX")
    directory.mkdir(parents=True)
    (directory / "logs").mkdir()
    (directory / "temp").mkdir()
    path = directory / "nginx.conf"
    path.write_text(config, encoding="utf-8")
    with (directory / "process.log").open("wb") as output:
        process = subprocess.Popen(
            [binary, "-p", directory.as_posix() + "/", "-c", path.as_posix(),
             "-g", "daemon off; master_process off;"],
            stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


async def ready(http, url, process, directory):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and process.poll() is None:
        try:
            if (await http.get(url)).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.02)
    logs = "\n".join(p.read_text("utf-8", errors="replace")
                     for p in directory.rglob("*.log"))
    raise RuntimeError("Nginx did not become ready:\n" + logs)


@asynccontextmanager
async def console_proxy(core, tmp_path, *, native_backend=False):
    """Optionally use a separately verified OCI UI via COMPANION_TEST_CONSOLE_UI_URL."""
    with ExitStack() as stack:
        assets = tmp_path / "assets"
        shutil.copytree(STATIC, assets)
        backend = None
        backend_url = None if native_backend else os.environ.get("COMPANION_TEST_CONSOLE_UI_URL")
        backend_log = tmp_path / "backend-access.log"
        if not backend_url:
            backend_url = "http://127.0.0.1:" + str(port())
            config = (ROOT / "deploy/console-ui.nginx.conf").read_text("utf-8")
            config = config.replace("listen 8080;", "listen " + backend_url[7:] + ";")
            config = config.replace("/usr/share/nginx/html", assets.as_posix())
            config = config.replace("/tmp/", tmp_path.as_posix() + "/")
            config = config.replace("error_log /dev/stderr warn;", "error_log stderr warn;")
            config = config.replace("access_log /dev/stdout;",
                "log_format isolation '$http_cookie|$http_authorization|$request_body';\n"
                f'    access_log "{backend_log.as_posix()}" isolation;')
            backend = stack.enter_context(nginx(quote_paths(config), tmp_path / "backend"))
            async with httpx.AsyncClient(trust_env=False, timeout=1) as http:
                await ready(http, backend_url + "/index.html", backend, tmp_path / "backend")

        async with tls_core(core, tmp_path) as (core_url, certificate):
            url = "https://127.0.0.1:" + str(port())
            core.settings.console.public_origin = url
            server = (ROOT / "deploy/console.nginx.conf").read_text("utf-8")
            server = server.replace("listen 443 ssl;", "listen " + url[8:] + " ssl;")
            server = server.replace("/etc/letsencrypt/live/console.example.test/fullchain.pem",
                                    certificate.as_posix())
            server = server.replace("/etc/letsencrypt/live/console.example.test/privkey.pem",
                                    (tmp_path / "core-key.pem").as_posix())
            server = server.replace("/etc/nginx/console-static-headers.conf",
                                    (ROOT / "deploy/console-static-headers.nginx.conf").as_posix())
            server = server.replace("http://127.0.0.1:8765", core_url)
            server = server.replace("http://127.0.0.1:8766", backend_url.rstrip("/"))
            prefix = (f'pid "{tmp_path.as_posix()}/edge.pid";\nerror_log stderr warn;\n'
                      'events { worker_connections 256; }\nhttp { access_log off;\n'
                      f'client_body_temp_path "{tmp_path.as_posix()}/body";\n'
                      f'proxy_temp_path "{tmp_path.as_posix()}/proxy";\n'
                      f'fastcgi_temp_path "{tmp_path.as_posix()}/fastcgi";\n'
                      f'uwsgi_temp_path "{tmp_path.as_posix()}/uwsgi";\n'
                      f'scgi_temp_path "{tmp_path.as_posix()}/scgi";\n')
            # Close the proxy's upstream sockets before asking Core to drain.
            with nginx(prefix + quote_paths(server) + "\n}", tmp_path / "edge") as edge:
                verify = ssl.create_default_context(cafile=str(certificate))
                async with httpx.AsyncClient(verify=verify, trust_env=False, timeout=2) as http:
                    await ready(http, url + "/console/", edge, tmp_path / "edge")
                yield SimpleNamespace(url=url, certificate=certificate, verify=verify,
                    backend=backend, edge=edge, backend_log=backend_log, assets=assets,
                    core_url=core_url, backend_url=backend_url)
