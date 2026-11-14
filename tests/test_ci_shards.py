import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree

import pytest

from scripts.ci_shards import SHARD_COUNT, measured_weights, partition

ROOT = Path(__file__).resolve().parents[1]


def test_partition_is_complete_disjoint_and_deterministic_for_new_files():
    files = [path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").glob("test_*.py")]
    files += ["tests/test_new_runtime.py"]
    shards = partition(files)
    assert len(shards) == SHARD_COUNT == 8
    assert all(shards) and set.union(*shards) == set(files)
    assert sum(map(len, shards)) == len(set(files))
    assert partition(reversed(files)) == shards


def test_updated_weights_balance_slow_files_without_splitting_modules(monkeypatch):
    files = [f"tests/test_rank_{index}.py" for index in range(SHARD_COUNT)]
    monkeypatch.setattr("scripts.ci_shards.WEIGHTS", {
        name: (SHARD_COUNT - index) * 10 for index, name in enumerate(files)
    })
    shards = partition([*files, "tests/test_new.py"])
    assert shards == [{name} for name in files[:-1]] + [{files[-1], "tests/test_new.py"}]


@pytest.mark.parametrize("workers", [0, 1, 2])
def test_cli_shards_execute_every_parametrized_case_exactly_once(tmp_path, workers):
    modules = [f"test_sample_{index}" for index in range(SHARD_COUNT + 1)]
    weights = {name + ".py": (SHARD_COUNT - index) * 10
               for index, name in enumerate(modules[:-1])}
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        "import json,os\nfrom pathlib import Path\nimport pytest\n"
        "from scripts import ci_shards\n"
        f"ci_shards.WEIGHTS = {weights!r}\n"
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport(item,call):\n"
        "    report = (yield).get_result()\n"
        "    path = Path(os.environ['PHASE_REPORTS']) / (str(os.getpid()) + '.jsonl')\n"
        "    with path.open('a') as stream:\n"
        "        stream.write(json.dumps([report.nodeid,report.when,report.duration]) + '\\n')\n",
        encoding="utf-8",
    )
    for name in modules:
        (tmp_path / f"{name}.py").write_text(
            "import os\nfrom pathlib import Path\nimport pytest\n"
            "def record(phase):\n"
            "    with Path(__file__).with_suffix('.events').open('a') as stream:\n"
            "        stream.write(phase + ' ' + str(os.getpid()) + '\\n')\n"
            "@pytest.fixture(scope='module',autouse=True)\n"
            "def module_state():\n"
            "    path = Path(os.environ['PHASE_REPORTS']) / (str(os.getpid()) + '.order')\n"
            "    with path.open('a') as stream:\n"
            "        stream.write(Path(__file__).name + '\\n')\n"
            "    record('setup')\n    yield\n    record('teardown')\n"
            f"@pytest.mark.parametrize('value', {[1, 2, 3] if name == modules[-1] else [1, 2]})\n"
            "def test_case(value):\n    assert value > 0\n", encoding="utf-8",
        )
    cases = []
    for index in range(SHARD_COUNT):
        report = tmp_path / f"shard-{index}.xml"
        phases = tmp_path / f"phases-{index}"
        phases.mkdir()
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "scripts.ci_shards", "--ci-shard", str(index),
             "-q", "-n", str(workers), "--dist=loadfile", "--no-loadscope-reorder",
             "--basetemp=" + str(tmp_path / f"tmp-{index}"),
             "-o", "cache_dir=" + str(tmp_path / "cache"), "--junitxml=" + str(report)],
            cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT), "PHASE_REPORTS": str(phases)},
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # The last shard owns a measured slow file and the unmeasured new file.
        # One real xdist worker detects both count-based scheduling and lost
        # collection ordering, without timing assertions or scheduler mocks.
        if index == SHARD_COUNT - 1 and workers in (0, 1):
            orders = list(phases.glob("*.order"))
            assert len(orders) == 1
            assert orders[0].read_text().splitlines() == [name + ".py" for name in modules[-2:]]
        elapsed = defaultdict(dict)
        for path in phases.glob("*.jsonl"):
            for line in path.read_text().splitlines():
                nodeid, phase, seconds = json.loads(line)
                assert phase not in elapsed[nodeid]
                elapsed[nodeid][phase] = seconds
        for case in ElementTree.parse(report).iter("testcase"):
            module, name = case.attrib["classname"], case.attrib["name"]
            durations = elapsed.pop(module + ".py::" + name)
            assert set(durations) == {"setup", "call", "teardown"}
            assert float(case.attrib["time"]) == pytest.approx(sum(durations.values()), abs=0.001)
            cases.append((module, name))
        assert not elapsed
    assert len(cases) == len(set(cases)) == SHARD_COUNT * 2 + 3
    for path in tmp_path.glob("*.events"):
        setup, teardown = [line.split() for line in path.read_text().splitlines()]
        assert setup[0] == "setup" and teardown == ["teardown", setup[1]]
    assert len(list(tmp_path.glob("*.events"))) == SHARD_COUNT + 1


def test_parallel_shard_returns_real_test_failure(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_failure.py").write_text("def test_failure():\n    assert False\n")
    report = tmp_path / "failed.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "scripts.ci_shards", "--ci-shard=0",
         "-q", "-n", "2", "--dist=loadfile", "--basetemp=" + str(tmp_path / "tmp"),
         "-o", "cache_dir=" + str(tmp_path / "cache"), "--junitxml=" + str(report)],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert len(list(ElementTree.parse(report).iter("failure"))) == 1


def test_junit_refresh_keeps_total_fixture_time_and_max_across_platforms(tmp_path):
    reports = []
    for index, seconds in enumerate((5, 9)):
        report = tmp_path / f"report-{index}.xml"
        report.write_text(
            '<testsuites><testsuite>'
            f'<testcase classname="tests.test_example.TestClass" time="{seconds}"/>'
            '<testcase classname="tests.test_example" time="2"/>'
            '<testcase classname="tests.test_another" time="0"><skipped/></testcase>'
            '</testsuite></testsuites>', encoding="utf-8",
        )
        reports.append(report)
    assert measured_weights(reports) == {
        "tests/test_another.py": 0, "tests/test_example.py": 11,
    }
    assert measured_weights(reports + reports) == measured_weights(reports)


@pytest.mark.parametrize("body", [
    '', '<testcase classname="tests.test_example" time="nan"/>',
    '<testcase classname="tests.test_example" time="-1"/>',
])
def test_junit_refresh_rejects_empty_or_invalid_timings(tmp_path, body):
    report = tmp_path / "bad.xml"
    report.write_text(f"<testsuite>{body}</testsuite>", encoding="utf-8")
    with pytest.raises(ValueError):
        measured_weights([report])
