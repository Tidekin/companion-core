"""One-off Windows Compose diagnosis; config rendering only, or exact mocked tests.

Run from the repository with its Python environment. No version command precedes
the first native config render. Raw subprocess output and command lines are never
written to the public report. All generated fixtures stay under .local/test-temp.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path.cwd().resolve()
POWERSHELL = shutil.which('powershell.exe') or shutil.which('pwsh.exe')
CREATE_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
SNAPSHOT = r'''
$ErrorActionPreference='Stop'
$rootId=ROOT_PID
$rows=@(Get-CimInstance -ClassName Win32_Process)
$ids=[System.Collections.Generic.HashSet[uint32]]::new()
[void]$ids.Add([uint32]$rootId)
do {
 $changed=$false
 foreach($row in $rows) {
  if($ids.Contains([uint32]$row.ParentProcessId) -and -not $ids.Contains([uint32]$row.ProcessId)) {
   [void]$ids.Add([uint32]$row.ProcessId);$changed=$true
  }
 }
} while($changed)
$selected=@(foreach($row in $rows) {
 if(-not $ids.Contains([uint32]$row.ProcessId)){continue}
 $mode='other_descendant'
 if($row.Name -ieq 'docker.exe'){$mode='docker_cli'}
 if($row.Name -imatch '^docker-.+\.exe$') {
  $mode='docker_plugin_other'
  if($row.CommandLine -match '(?:^|\s)docker-cli-plugin-metadata(?:\s|$)'){$mode='plugin_metadata'}
  elseif($row.CommandLine -match '(?:^|\s)config(?:\s|$)'){$mode='compose_config'}
  elseif($row.CommandLine -match '(?:^|\s)version(?:\s|$)'){$mode='compose_version'}
 }
 [ordered]@{pid=[int]$row.ProcessId;parent_pid=[int]$row.ParentProcessId;name=$row.Name;
  executable=$row.ExecutablePath;created_utc=$row.CreationDate.ToUniversalTime().ToString('o');
  cpu_seconds=([double]$row.KernelModeTime+[double]$row.UserModeTime)/10000000;
  threads=[int]$row.ThreadCount;handles=[int]$row.HandleCount;mode=$mode}
})
ConvertTo-Json -InputObject $selected -Depth 4 -Compress
'''
STOP_SURVIVORS = r'''
$ErrorActionPreference='Stop'
$expected=@(ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd()))
$stopped=@(foreach($item in $expected) {
 $row=Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId='+[int]$item.pid)
 if($null -eq $row){continue}
 if($row.Name -eq $item.name -and [int]$row.ParentProcessId -eq [int]$item.parent_pid -and
    $row.CreationDate.ToUniversalTime().ToString('o') -eq $item.created_utc -and
    $row.ExecutablePath -eq $item.executable) {
  Stop-Process -Id ([int]$item.pid) -Force -ErrorAction SilentlyContinue
  [int]$item.pid
 }
})
ConvertTo-Json -InputObject $stopped -Compress
'''


def digest(value):
    return hashlib.sha256(value).hexdigest()


def output_evidence(value):
    return {'available': value is not None, **({'bytes': len(value), 'sha256': digest(value)}
                                             if value is not None else {})}


def executable(path):
    path = Path(path).resolve()
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': digest(path.read_bytes())}


def powershell(code, data=None, timeout=8):
    encoded = base64.b64encode(code.encode('utf-16-le')).decode()
    result = subprocess.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive',
        '-EncodedCommand', encoded], input=data, capture_output=True, timeout=timeout,
        creationflags=CREATE_FLAGS)
    if result.returncode:
        raise RuntimeError('CIM_DIAGNOSTIC_FAILED')
    return json.loads(result.stdout.decode('utf-8-sig'))


def snapshot(pid):
    started = time.monotonic()
    try:
        rows = powershell(SNAPSHOT.replace('ROOT_PID', str(int(pid))))
        return {'processes': rows, 'query_seconds': round(time.monotonic() - started, 3)}
    except Exception as error:
        return {'processes': [], 'query_seconds': round(time.monotonic() - started, 3),
                'error_type': type(error).__name__}


def run(label, command, *, timeout, stdin_null=False, monitor=False):
    """Retain pipe capture, but bound the original call and its exact-tree cleanup."""
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL if stdin_null else None)
    value = {'label': label, 'pid': process.pid, 'executable': executable(command[0]),
             'stdin': 'DEVNULL' if stdin_null else 'inherited', 'timeout_seconds': timeout,
             'timed_out': False, 'snapshots': []}
    deadline = started + timeout
    output = error = None
    while True:
        try:
            output, error = process.communicate(timeout=max(0.01, min(
                5 if monitor else timeout, deadline - time.monotonic())))
            break
        except subprocess.TimeoutExpired as failure:
            if monitor and time.monotonic() < deadline:
                observed = snapshot(process.pid)
                docker_rows = [row for row in observed['processes']
                               if row['name'].lower().startswith('docker')]
                if docker_rows or observed.get('error_type'):
                    value['snapshots'].append({**observed, 'processes': docker_rows,
                        'elapsed_seconds': round(time.monotonic() - started, 3)})
                continue
            value.update(timed_out=True, returncode_at_timeout=process.poll(),
                output_at_timeout={'stdout': output_evidence(failure.stdout),
                                   'stderr': output_evidence(failure.stderr)})
            observed = snapshot(process.pid)
            value['snapshots'].append({**observed,
                'elapsed_seconds': round(time.monotonic() - started, 3), 'at_timeout': True})
            cleanup = value['cleanup'] = {}
            # taskkill /T is the repository's existing Windows cleanup mechanism.
            # The retained Popen handle prevents targeting a recycled parent PID.
            if process.poll() is None:
                try:
                    killed = subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                        capture_output=True, timeout=5, creationflags=CREATE_FLAGS)
                    cleanup['taskkill_returncode'] = killed.returncode
                except Exception as failure:
                    cleanup['taskkill_error_type'] = type(failure).__name__
            # Revalidate exact observed identities if the parent exited during capture.
            try:
                cleanup['revalidated_survivors_stopped'] = powershell(STOP_SURVIVORS,
                    json.dumps(observed['processes']).encode(), timeout=5)
            except Exception as failure:
                cleanup['survivor_error_type'] = type(failure).__name__
            if process.poll() is None:
                process.kill()
            try:
                output, error = process.communicate(timeout=5)
                cleanup['pipes_drained'] = True
            except subprocess.TimeoutExpired as failure:
                output, error = failure.stdout, failure.stderr
                cleanup['pipes_drained'] = False
            break
    value.update(returncode=process.poll(), elapsed_seconds=round(time.monotonic() - started, 3),
                 stdout=output_evidence(output), stderr=output_evidence(error))
    if value.get('cleanup', {}).get('pipes_drained', True):
        process.stdout.close()
        process.stderr.close()
    return value, output, error


def candidates(docker, observations):
    """Read plugin locations only after the cold render; never execute discovery."""
    paths = []
    for value in observations:
        for capture in value.get('snapshots', []):
            paths += [Path(row['executable']) for row in capture['processes']
                      if row.get('executable') and row['name'].lower() == 'docker-compose.exe']
    config_dir = Path(os.environ.get('DOCKER_CONFIG', str(Path.home() / '.docker')))
    config = config_dir / 'config.json'
    if config.is_file():
        try:
            configured = json.loads(config.read_text(encoding='utf-8-sig'))
            paths += [Path(item) / 'docker-compose.exe'
                      for item in configured.get('cliPluginsExtraDirs', []) if isinstance(item, str)]
        except (OSError, ValueError, TypeError):
            pass
    paths += [config_dir / 'cli-plugins/docker-compose.exe',
              docker.parent / 'cli-plugins/docker-compose.exe',
              docker.parent.parent / 'cli-plugins/docker-compose.exe']
    for variable, suffix in (('ProgramData', 'Docker/cli-plugins/docker-compose.exe'),
                             ('ProgramFiles', 'Docker/cli-plugins/docker-compose.exe'),
                             ('ProgramFiles', 'Docker/Docker/resources/cli-plugins/docker-compose.exe')):
        if os.environ.get(variable):
            paths.append(Path(os.environ[variable]) / suffix)
    found = shutil.which('docker-compose.exe')
    if found:
        paths.append(Path(found))
    unique = {}
    for path in paths:
        if path.is_file() and path.suffix.lower() == '.exe':
            unique.setdefault(str(path.resolve()).lower(), path.resolve())
    return list(unique.values())


def safe_version(value, raw):
    text = (raw or b'').decode('utf8', errors='replace').strip()
    if re.fullmatch(r'(?:Docker (?:Compose )?version )?v?[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+_-]*(?:, build [A-Za-z0-9._+-]+)?', text):
        value['version'] = text


def config_result(value, raw, token):
    if value['returncode'] != 0 or value['timed_out']:
        return
    try:
        rendered = json.loads(raw)
        service = rendered['services']['heybox-reader']
        value['literal_credentials_preserved'] = (
            service['environment']['COMPANION_READER_TOKEN'] == token.replace('$', '$$'))
        value['public_discovery_command_preserved'] = '--public-discovery' in service['command']
        value['service_count'] = len(rendered['services'])
    except Exception as error:
        value['config_parse_error_type'] = type(error).__name__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pytest', action='store_true', help='Observe the exact two-worker test pairing')
    parser.add_argument('--concurrent', action='store_true', help='End native mode with two concurrent config renders')
    parser.add_argument('--output', type=Path, default=Path('.local/compose-diagnostic/report.json'))
    args = parser.parse_args()
    assert os.name == 'nt' and POWERSHELL, 'WINDOWS_POWERSHELL_REQUIRED'
    assert (ROOT / 'source_reader/migrate_astrbot.py').is_file(), 'RUN_FROM_REPOSITORY_REQUIRED'
    output = (ROOT / args.output).resolve()
    assert output.is_relative_to(ROOT / '.local') and not output.exists(), 'FRESH_WORKSPACE_OUTPUT_REQUIRED'
    scratch = ROOT / '.local/test-temp/compose-diagnostic'
    assert scratch.resolve().is_relative_to(ROOT / '.local/test-temp') and not scratch.exists()
    scratch.mkdir(parents=True, exist_ok=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    events = output.with_suffix('.jsonl')
    assert not events.exists(), 'FRESH_EVENT_LOG_REQUIRED'
    report = {'status': 'DIAGNOSTIC_RUNNING', 'mode': 'pytest' if args.pytest else 'native',
        'started_utc': datetime.now(UTC).isoformat(), 'source_root': str(ROOT),
        'python': executable(sys.executable), 'calls': [], 'no_version_warmup': True,
        'ambient_environment_present': {name: name in os.environ for name in (
            'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'DOCKER_CLI_PLUGIN_ORIGINAL_CLI_COMMAND',
            'COMPOSE_FILE', 'COMPOSE_PROFILES', 'COMPOSE_ENV_FILES', 'COMPOSE_PROJECT_NAME')},
        'source_sha256': {name: digest((ROOT / name).read_bytes()) for name in (
            'source_reader/migrate_astrbot.py', 'tests/test_reader_extraction.py',
            'tests/test_native_image_bursts.py', 'scripts/verify_dev_compose.py')}}
    started = time.monotonic()
    lock = threading.Lock()

    def record(value):
        with lock:
            report['calls'].append(value)
            line = json.dumps(value, ensure_ascii=True)
            with events.open('a', encoding='utf8') as stream:
                stream.write(line + '\n')
            print(line, flush=True)
        if value.get('cleanup', {}).get('pipes_drained') is False:
            raise RuntimeError('EXACT_TREE_CLEANUP_DRAIN_INCOMPLETE')

    def invoke(label, command, *, bound=30, **kwargs):
        remaining = 235 - (time.monotonic() - started)
        if remaining < 15:
            raise TimeoutError('DIAGNOSTIC_TOTAL_BUDGET_REACHED')
        return run(label, command, timeout=min(bound, max(1, remaining - 15)), **kwargs)

    exit_code = 0
    try:
        if args.pytest:
            command = [sys.executable, '-B', '-m', 'pytest', '-q', '-n', '2', '--dist=loadfile',
                '--no-loadscope-reorder', '--basetemp=' + str(scratch / 'pytest'),
                'tests/test_reader_extraction.py::test_migration_cli_and_compose_preserve_literal_credentials',
                'tests/test_native_image_bursts.py']
            value, raw, _ = invoke('pytest_original_two_worker_pair', command, bound=220, monitor=True)
            # Only standard pytest result counts may enter the public report.
            value['pytest_summary_counts'] = re.findall(
                r'\b\d+ (?:passed|failed|skipped|errors?)\b', (raw or b'').decode('utf8', errors='replace'))[-8:]
            record(value)
        else:
            docker_path = shutil.which('docker.exe')
            assert docker_path, 'DOCKER_CLI_REQUIRED'
            docker = Path(docker_path).resolve()
            token = 'literal-$value\'"=' * 3
            config = {'core_url': 'http://companion-api:8765', 'bridge_token': 'bridge-' * 8,
                'content_enabled': True, 'heybox_reader_enabled': True, 'heybox_reader_id': 'heybox',
                'heybox_reader_token': token}
            config_file, target = scratch / 'config.json', scratch / 'private'
            config_file.write_text(json.dumps(config), encoding='utf8')
            value, _, _ = invoke('actual_migration_cli', [sys.executable, '-B', '-m',
                'source_reader.migrate_astrbot', '--config', str(config_file), '--output', str(target),
                '--network', 'private-test'])
            record(value)
            assert value['returncode'] == 0 and not value['timed_out'], 'MIGRATION_CLI_FAILED'
            arguments = ['-f', str(target / 'compose.json'), 'config', '--format', 'json']
            native = [str(docker), 'compose', *arguments]
            for label, null in (('cold_docker_config_inherited_stdin', False),
                                ('docker_config_devnull_stdin', True)):
                value, raw, _ = invoke(label, native, stdin_null=null)
                config_result(value, raw, token)
                record(value)
            # The exact cold command above always precedes these metadata/version calls.
            plugins = candidates(docker, report['calls'])
            report['installed_compose_candidates'] = [executable(path) for path in plugins]
            value, raw, _ = invoke('docker_cli_version', [str(docker), '--version'], bound=10)
            safe_version(value, raw)
            record(value)
            if plugins:
                plugin = plugins[0]
                report['direct_compose_selected'] = executable(plugin)
                value, raw, _ = invoke('direct_compose_metadata',
                    [str(plugin), 'docker-cli-plugin-metadata'], bound=10)
                if value['returncode'] == 0 and not value['timed_out']:
                    try:
                        metadata = json.loads(raw)
                        value['metadata'] = {key: metadata[key] for key in ('SchemaVersion', 'Vendor', 'Version')
                            if isinstance(metadata.get(key), str)
                            and re.fullmatch(r'[A-Za-z0-9 ._+-]{1,128}', metadata[key])}
                    except (ValueError, TypeError):
                        value['metadata_parse_failed'] = True
                record(value)
                value, raw, _ = invoke('direct_compose_version', [str(plugin), 'version', '--short'], bound=10)
                safe_version(value, raw)
                record(value)
                value, raw, _ = invoke('direct_compose_config', [str(plugin), *arguments])
                config_result(value, raw, token)
                record(value)
            else:
                report['direct_compose_unavailable'] = True
            if args.concurrent:
                def concurrent_call(number):
                    value, raw, _ = invoke('concurrent_docker_config_' + str(number), native)
                    config_result(value, raw, token)
                    record(value)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(concurrent_call, (1, 2)))
        report['status'] = 'DIAGNOSTIC_COMPLETE'
    except Exception as error:
        report.update(status='DIAGNOSTIC_INCOMPLETE', error_type=type(error).__name__)
        exit_code = 2
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['finished_utc'] = datetime.now(UTC).isoformat()
        # This exact directory was created by this invocation; no broad cleanup.
        assert scratch.resolve().is_relative_to((ROOT / '.local/test-temp').resolve())
        assert scratch.name == 'compose-diagnostic' and not scratch.is_symlink()
        try:
            shutil.rmtree(scratch)
            report['synthetic_fixture_cleanup'] = 'complete'
        except OSError as error:
            report['synthetic_fixture_cleanup'] = type(error).__name__
        with output.open('x', encoding='utf8') as stream:
            json.dump(report, stream, indent=2, ensure_ascii=True)
            stream.write('\n')
        print(json.dumps({'status': report['status'], 'report': str(output),
                          'elapsed_seconds': report['elapsed_seconds']}), flush=True)
    # A stuck inherited pipe must not turn interpreter cleanup into an unbounded wait.
    if any(call.get('cleanup', {}).get('pipes_drained') is False for call in report['calls']):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(2)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
