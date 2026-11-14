"""Complete, disjoint file shards using measured pytest setup/call/teardown time."""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree

# Refresh from complete CI JUnit artifacts with this module's CLI. We keep each
# file together so module fixtures/order retain their normal pytest semantics.
WEIGHTS_PATH = Path(__file__).with_name("ci_weights.json")
WEIGHTS = json.loads(WEIGHTS_PATH.read_text("utf-8"))["file_seconds"]
SHARD_COUNT = 8


def file_priority(name):
    return -WEIGHTS.get(name, 2), name


def partition(files):
    shards, loads = [set() for _ in range(SHARD_COUNT)], [0] * SHARD_COUNT
    for name in sorted(set(files), key=file_priority):
        index = min(range(SHARD_COUNT), key=loads.__getitem__)
        shards[index].add(name)
        loads[index] += WEIGHTS.get(name, 2)
    return shards


def pytest_addoption(parser):
    parser.addoption("--ci-shard", type=int, choices=range(SHARD_COUNT), default=None)


def pytest_collection_modifyitems(config, items):
    index = config.getoption("--ci-shard")
    if index is None:
        return
    names = {item: Path(item.path).relative_to(config.rootpath).as_posix() for item in items}
    selected_files = partition(names.values())[index]
    selected = [item for item in items if names[item] in selected_files]
    if not selected:
        import pytest

        raise pytest.UsageError("CI shard collected no tests")
    deselected = [item for item in items if names[item] not in selected_files]
    config.hook.pytest_deselected(items=deselected)
    # Stable sorting keeps each module's original case/fixture order. The native
    # loadfile scheduler must retain this order via --no-loadscope-reorder.
    items[:] = sorted(selected, key=lambda item: file_priority(names[item]))


def measured_weights(reports):
    """Max of per-file totals across reports/platforms; never sum duplicate runs."""
    weights = {}
    for report in reports:
        durations = defaultdict(float)
        for case in ElementTree.parse(report).iter("testcase"):
            parts = case.attrib["classname"].split(".")
            # pytest class tests append the class name after the test module.
            end = next(i + 1 for i, part in enumerate(parts) if part.startswith("test_"))
            name = "/".join(parts[:end]) + ".py"
            seconds = float(case.attrib["time"])
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("JUnit duration must be finite and nonnegative")
            durations[name] += seconds
        if not durations:
            raise ValueError("JUnit report contains no test cases")
        for name, seconds in durations.items():
            weights[name] = round(max(weights.get(name, 0), seconds), 3)
    return dict(sorted(weights.items()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh file weights from CI JUnit reports")
    parser.add_argument("--source-run", required=True, help="Successful full CI run URL")
    parser.add_argument("reports", type=Path, nargs="+")
    args = parser.parse_args()
    result = {"source_run": args.source_run, "file_seconds": measured_weights(args.reports)}
    WEIGHTS_PATH.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
