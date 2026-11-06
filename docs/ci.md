# CI validation profiles

Every selected run reports a `preflight` result and a final `ci` result. Configure
branch protection to require `ci`; repository protection settings are managed
separately. Integration evidence must identify the exact PR workflow or main
commit and all of its required jobs.

| Change / event | Validation |
|---|---|
| Default branch | Full validation, or exact-tree reuse of successful merged-PR CI |
| Release tag, merge queue, manual run | Full validation |
| PR containing code or configuration | Full or conservatively path-selected validation against GitHub's merge preview |
| Development branch push | No workflow; open a PR or manually dispatch full validation |
| Only README.md, Markdown under docs/, or explicitly registered evidence JSON | Privacy, CI policy and registered evidence checks |
| Dependency locks, protocols, schema, privacy/budget/transport policy, build/deployment or CI configuration in a PR | Full validation |
| Unknown files, unavailable comparison, malformed event, empty diff | Full validation |

Full validation retains the complete Ubuntu and Windows pytest suites, replay,
smoke, package checks, all container isolation tests, the Windows browser reader,
and the pinned upstream native-plugin checks. Preflight only selects the profile
and checks existing verification receipts; it does not install dependencies or
repeat product tests before the parallel jobs. Documentation validation owns a
fresh `ci-docs-*` directory under `.local/test-temp`, removed on success or failure.
Unrelated retained diagnostics are preserved.

Windows product shards run `docker compose version --short` once, with a two-minute
step limit, immediately before parallel pytest. The [Windows diagnostic run](https://github.com/Tidekin/companion-core/actions/runs/42256242406)
observed a 30-second cold configuration timeout and a prolonged plugin-metadata child
in a separate two-worker run. Docker's [native metadata discovery](https://github.com/docker/cli/blob/v29.7.2/cli-plugins/manager/candidate.go)
starts that child when loading the Compose plugin. This initialization gives the
installed dependency its own bounded startup phase; the configuration tests retain
their existing deadlines and assertions.

## Console and Windows path selection

Only pull requests can use the `selected` profile. The complete PR diff selects the checks;
a final UI-only commit cannot hide backend changes from earlier commits in that PR.

| Complete non-document diff | Product tests and packaging | Browser | Containers / native plugins |
|---|---|---|---|
| Only the three existing Console static assets in `STATIC_UI` and/or the two browser tests in `CONSOLE_BROWSER_TESTS` | One Ubuntu job: Console CSP/assets, owner API contracts, wheel/source package checks | Console interactions on Windows | Omitted |
| Only existing backend files/tests explicitly listed in `NON_BROWSER` | Complete Ubuntu and Windows suites, replay, smoke and packages | Omitted | Required |
| Mixed surfaces, shared dependencies, packaging/deployment, PowerShell, process/path/lock, voice/bridge, new tests/files or any unknown path | Complete Ubuntu and Windows suites, replay, smoke and packages | Readers and Console on Windows | Required |

Console-only validation always type-checks the owner Console and runs
`test_console_static.py`, `test_owner_console.py`, `test_console_cognition.py` and the real
TLS/browser interactions in `test_console_browser.py` and `test_console_supplement_browser.py`.
It builds and verifies the release
archives, including the three Console resources. It does not schedule full backend shards,
backend replay/smoke, reader browser tests or unchanged container/native-plugin checks.
The existing backend-only selection and all full validation keep their previous coverage.
Documentation privacy and registered evidence checks also run, so Markdown or evidence
JSON accompanying a Console-only change retains its validators.

The two existing Console browser test files can accompany static assets or select this
same profile on their own. This exception covers exactly those filenames. Adding backend
Python, locks, protocols, migrations, deployment, CI changes or a new/unknown test file to
this Console diff requires full validation. The separate `NON_BROWSER` allowlist and all
checks within each profile are unchanged.

The exact small allowlists live in `scripts/ci_policy.py`. Generic Python changes are **not**
assumed portable or browser-independent; they keep Windows unless explicitly listed. Even
the listed backend Python changes retain the complete Windows product suite. New filenames,
unknown evidence JSON, missing Git history and malformed events run both surfaces. Existing
backend test files listed alongside their modules can omit only the browser job; new test
files default to both. An allowlisted file gaining browser/platform responsibilities requires
review of this policy in the same change. No source grep, test deletion or timing heuristic
decides coverage.

Main pushes, tags, merge queues and manual runs keep full platform coverage (or the existing
documentation-only profile). Main can reuse only independently verified **full** PR evidence;
a `selected` PR must run full validation after merge. Missing Windows work never counts as
full coverage. The full reuse artifact is version 3 and records Windows product and browser
coverage plus the retained runtime-image metadata artifact. Reuse checks all actual job
names/results and the original image artifacts. Older receipts and selected receipts are rejected.

Preflight emits the native GitHub Actions JSON matrix. Full product validation gets all
four shards on each platform, plus its replay/smoke/package checks once. Console-only PRs
get one Ubuntu Console/package job. The gate validates every selected skip, requires the
Console test and browser jobs, and rejects failed, cancelled or unexpected job results. Every final
gate writes `.local/ci/validation.json` with selection reasons, the actual scheduled matrix,
expected and observed job results, checkout identity and reuse reference. The always-uploaded
`ci-validation-*` artifact preserves failures as well as successes. It is a scoped report,
not reusable full-tree proof; only a successful `full` run emits `ci-full-*`.
The gate also reads this run attempt's native Actions job timestamps with read-only permission
and adds per-job elapsed seconds to the report/summary. Unavailable or incomplete timing data
is explicitly marked unavailable; the running gate has no invented completion time. These
measurements do not replace JUnit timings or change which jobs must pass.

The final `ci` job rejects failed, cancelled, missing or unexpectedly skipped
required jobs. A docs run requires every full-only job to be skipped; it cannot
hide a failed attempted job. PR checks retain GitHub's default merge-preview
checkout, including for forks, with read-only contents permission. No
`pull_request_target` event or additional token permissions are used.

The push filter selects `main` and tags. A development commit is validated once
through its PR, rather than starting duplicate push and PR full suites. A branch
without a PR has no remote check. New PR updates cancel older runs for that PR.
Main, tag and merge-queue runs do not cancel in-progress integration runs. Event
types use separate concurrency groups so a main push cannot cancel a PR merge
preview. If the default branch is renamed, update the explicit push branch filter.

The selector uses the complete local Git diff with renames disabled, including
deletions and both names of a rename. New branches compare with the default-branch
merge base. Missing history and unsupported paths select full validation. It does
not depend on a truncated API file list or use filenames as shell commands.

For local policy changes, use this PowerShell command:

```powershell
./scripts/check.ps1 -TestFiles @('tests/test_ci_policy.py', 'tests/test_source_reader_image.py')
```

Use `./scripts/check.ps1 -Docs` for local documentation validation. For code,
pass related tests to `check.ps1` during implementation, then freeze the reviewed
candidate before opening/updating its PR for full CI. Run the workflow manually
only when a full integration run is needed without a PR. The final job's
Validation receipt is the one handoff record; link it instead of replaying the
same candidate's checks or duplicating its commit/tree identifiers.

## Retained public runtime images

The full container job builds Core, OneBot Bridge, Source Reader and Console UI once,
runs the existing
isolation and entry-point checks, then saves those exact Docker image IDs as standard Docker
archives. It also loads and inspects the exported archives before upload. Executor, file-parser
and all existing smoke checks remain required; their images are outside this delivery slice.
The Bridge still builds through the actual native Compose build file. The UI candidate image
is the one used by the real Linux Nginx/HTTP checks and UI replacement/rollback smoke;
the synthetic rollback baseline is not a release component. Main exact-tree reuse
inherits the original full run and its retained images instead of building them again.

These artifacts are labelled `PUBLIC_SOURCE_RUNTIME_ARTIFACT`. This repository is public:
its Actions artifacts are not private storage. The build context is a Git archive of the
checked-out commit. The producer checks the reviewed Dockerfile COPY inputs, the existing
package privacy exclusions and packaging canary, image environment keys and credential-like
history assignments. Local configuration, credentials, databases, user data, logs and private
staging directories are excluded. An unexpected COPY shape or private input fails before upload.
Existing wheel/source privacy and canary checks continue in the product shards.
Before each build, the producer compares the actual directory/file set and raw blob IDs with
the committed Git tree, then repeats the COPY/privacy checks. Added files, changed source,
recipes or build configuration fail before Docker runs; the metadata JSON alone cannot authorize
a build. Directory permissions may differ for the owner-only migration-directory test.

| Identity | Meaning |
|---|---|
| Commit, tree, run ID and attempt | Original full CI source and all fourteen successful jobs |
| `image_id` | Actual producer-local Docker ID; classic stores use config IDs, containerd uses manifest/index IDs |
| `config_digest` | Portable SHA256 of the original saved image configuration bytes |
| `archive_descriptor` / `platform_manifest_descriptor` | Validated OCI root and single `linux/amd64` manifest descriptors |
| `consumer_local_image_id` | Actual verified Docker ID on the receiving daemon, recorded in the load receipt |
| Archive SHA256 and bytes | Exact compressed Docker archive to download and load |
| Actions artifact ID and digest | Immutable uploaded ZIP identity and transport checksum |
| `registry_manifest_digest` | Unset; this slice does not push to a registry |

The configuration digest, saved OCI manifest identity and local daemon ID have separate roles.
The producer derives portable identity from the original save archive rather than treating
`docker inspect`'s ID as a configuration hash. Descriptor hashes and byte sizes bind the
single-image OCI index, manifest, raw config and ordered layers. If the Docker-compatible
`manifest.json` is present, its config hash and ordered layer identities must describe that
same image. Unknown platforms and ambiguous images fail before any daemon operation.
Classic and containerd stores can report different local IDs for identical saved image content,
as explained in [Moby's storage-backend discussion](https://github.com/moby/moby/issues/51934)
and [Docker's containerd image-store documentation](https://docs.docker.com/engine/storage/containerd/).
The archive parser follows the standard [OCI layout](https://github.com/opencontainers/image-spec/blob/v1.1.1/image-layout.md),
[descriptors](https://github.com/opencontainers/image-spec/blob/v1.1.1/descriptor.md)
and [manifest](https://github.com/opencontainers/image-spec/blob/v1.1.1/manifest.md).
The small `ci-container-images-*`
artifact binds the four separate `runtime-image-{core,bridge,reader,ui}-*` archives; `ci-full-*`
and `ci-validation-*` reference that metadata. A selected run cannot emit reusable image proof.
Missing, expired, changed, ambiguous or incomplete image evidence makes main run full validation.
All retained artifacts expire after seven days. Reuse and consumption depend on the exact
tested tree, the latest successful run/attempt and unexpired authenticated original artifacts.
Run timestamps do not authorize or expire proof: upstream record timestamps can disagree
with actual step/log times, as exposed by the [native job API](https://docs.github.com/en/rest/actions/workflow-jobs).

The default `load` command always performs Docker load, including in producer CI where the
images already exist, so native archive codec validation remains exercised. A deployment resume
may explicitly use `load container-images.json --reuse-existing`. It first authenticates the
original archive, lists exact local IDs and inspects the matching archive-bound ID. Only a
verified existing image is skipped; absent images are loaded and verified through the same path.
Daemon errors or inconsistent identity never authorize a fallback load. The receipt reports
`action` as `already_present` or `loaded`, the producer ID, portable config digest, archive
descriptors and `consumer_local_image_id`. No tags, registry references or daemon configuration
are changed. Original retained metadata and archives remain unchanged; the deployment pins the
reviewed consumer-tool revision separately from the original full-run image source.

BuildKit's local cache backend and the existing pinned Actions cache preserve dependency
layers. The dependency installation steps already precede application COPY steps in the
Dockerfiles. Recorded cache imports/exports describe configured indexes and bytes; they do
not claim a measured cache hit. No inferred dependency graph chooses release components.
UI dependencies are its pinned official Nginx base; its dependency-lock field is unset.
The UI privacy check accepts only the exact public environment of that pinned base, scoped
to UI, and its four public assets plus the public Nginx configuration. Python-image environment
rules are unchanged. A base-image or environment change requires updating this reviewed boundary.

`container-images.json` records actual per-image raw/compressed sizes, save/hash,
compression/hash, archive-identity validation and build/inspect durations. `runtime-image-load-*` preserves local load
timings. Native Actions upload-step timestamps and logs supply upload duration and transport
size; the consumer records download and load/inspect time. Measurements are produced by the
actual Linux run, not estimated locally. `--previous-run` compares portable configuration digests
and platforms, so an unchanged image is identified by content rather than a source label.

The read-only consumer and its deployment boundary are documented in
[deployment preparation](deployment.md#加载已验证的-ci-镜像).
The implementation uses Docker's documented
[image archive sharing](https://docs.docker.com/build/ci/github-actions/share-image-jobs/),
[BuildKit cache backends](https://docs.docker.com/build/ci/github-actions/cache/),
[Compose build cache](https://docs.docker.com/reference/compose-file/build/), and
[immutable Actions artifact outputs](https://github.com/actions/upload-artifact#using-outputs).
The official Nginx base environment was read from its actual `linux/amd64` configuration using
[`docker buildx imagetools inspect`](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/).
Build-context verification uses Git's documented
[`ls-tree`](https://git-scm.com/docs/git-ls-tree) and
[`hash-object --no-filters`](https://git-scm.com/docs/git-hash-object) commands.

## File shards and timing maintenance

The ordinary suite runs as four disjoint file shards on each selected platform.
Full runs include both Ubuntu and Windows. Every collected file is assigned, including new files, and a file's
parametrized cases and module fixtures stay together. Replay, smoke, build and
package checks run once per platform, on the existing designated shard. A failed
shard fails the final gate; full-tree reuse requires all eight test jobs as well as the
remaining full-validation jobs. An old two-shard receipt is insufficient.

Each product shard uses the existing `scripts.test_feedback` entry point with two
pytest-xdist workers and `--dist=loadfile`. The shard plugin still selects the
same complete files before xdist assigns those files to workers; no impact
selection is enabled in CI. Module fixtures and parametrized cases stay together.
Within each shard, files run in descending recorded duration, with path ties
and stable case order inside each module. CI passes `--no-loadscope-reorder` so
the native scheduler preserves this priority instead of reordering by case count.
This uses the pinned xdist 3.8.0 [loadfile inheritance](https://github.com/pytest-dev/pytest-xdist/blob/v3.8.0/src/xdist/scheduler/loadfile.py)
and its [loadscope ordering option](https://github.com/pytest-dev/pytest-xdist/blob/v3.8.0/src/xdist/scheduler/loadscope.py).
Serial and one/two-worker subprocess checks verify exact-once case coverage,
module fixture ownership and slow-file priority through the real scheduler.
Product jobs precompile dependency bytecode once with the existing
[uv option](https://docs.astral.sh/uv/reference/cli/#uv-run--compile-bytecode), then
precompile checkout modules with Python's [compileall](https://docs.python.org/3.12/library/compileall.html)
and checked source hashes. Independent API/worker/CLI processes can read that
bytecode even with the existing [`-B`](https://docs.python.org/3.12/using/cmdline.html#cmdoption-B)
write suppression. This avoids recompiling identical imports in each child.
The preparation cost remains part of job timing; Console-only jobs skip it.
The job's [PYTHONPYCACHEPREFIX](https://docs.python.org/3.12/using/cmdline.html#envvar-PYTHONPYCACHEPREFIX)
keeps both dependency and source bytecode in `.local/python-bytecode`, outside
public image COPY inputs. The pinned [uv compiler](https://github.com/astral-sh/uv/blob/0.12.10/crates/uv-installer/src/compile.rs)
passes this environment to its Python workers. A real compile/import regression
checks public input guards and stale-source invalidation with the workflow's
actual prefix and compiler options. Bytecode remains excluded from release packages.
The shared empty-schema fixture performs one real migration per job, then creates
independent test databases. Each worker and its children keep their own scratch
directory and real port tests continue binding kernel-assigned ports.

Before starting workers, the coordinator prepares the pinned BGE file snapshot
once per job. A cold model cache still downloads the required files; a warm cache
uses the local snapshot. Each worker retains its own real ONNX inference session
and process-local encoder cache. Sharding preserves dependency/model cache rules,
four-shard weights, the platform matrix, required jobs and full-tree evidence requirements. JUnit
continues recording setup, call and teardown durations. If resource pressure or
instability outweighs the measured gain, set `--workers 0` in the product command
to restore serial execution without changing its scope; test failures never
trigger an automatic passing fallback.

`scripts/ci_weights.json` records file durations from a successful full run.
Weights use the slower platform's per-file JUnit total, including setup and
teardown. The existing longest-first/lightest-shard algorithm balances those
weights; unmeasured files retain a two-second estimate. No live cache, network
lookup or mutable timing service changes collection. To refresh after substantial
suite growth or measured load drift, download the successful run's complete `pytest-*` artifacts into
an owned `.local` directory, then run:

```powershell
$reports = Get-ChildItem .local/ci-timings -Recurse -Filter pytest.xml | Select-Object -ExpandProperty FullName
python -B scripts/ci_shards.py --source-run <successful-run-url> @reports
./scripts/check.ps1 -TestFiles tests/test_ci_shards.py
```

Review the resulting weights with their source run, then include them in the next
frozen candidate. Do not refresh weights after every CI run or trigger a second
full run just to update timings. Compare first-job-to-gate wall time separately
from summed job/case time and queue delays. Four shards add runner startup and
session-fixture overhead; if runner concurrency or costs dominate, reverting the
matrix, shard count and reuse job set together restores the previous two shards.

The October 31 refresh uses all eight JUnit reports from successful full run
[41165362841](https://github.com/yanyuhanyue/companion-core/actions/runs/41165362841).
Its slowest job took 954 seconds; the suite step accounted for 895 seconds.
`test_model_carry_native.py` contained only eight cases but took 515.525 seconds
on Ubuntu, making case count a poor dispatch priority. Repartitioning the same
measured cases reduces Ubuntu per-shard totals from 935–1569 to 1237–1344 seconds
and Windows totals from 1215–1550 to 1302–1412 seconds. These are summed case
loads, not candidate wall-time predictions; the frozen candidate's full CI
receipt supplies the actual wall time and queue delays.

Full pytest runs report their 15 slowest cases to guide later work. The generic
`core` fixture migrates an empty SQLite database once per pytest session, before
any Core, Runtime or ErasureLedger initialization. SQLite backup then creates
each test's separate database. Runtime pragmas, identity initialization, ledger
creation, real embedding inference and all business assertions still execute.
Migration, historical upgrade and recovery tests continue calling real Alembic.

Measured on the same Windows worktree, interpreter and offline model-cache
configuration with cProfile enabled for both runs:

| `tests/test_m0.py`, 20 passing tests | Baseline `12a3f5b` | Empty-schema fixture |
|---|---:|---:|
| Pytest elapsed | 32.38 s | 5.12 s |
| Real `migrate()` calls | 23 | 4 |
| Cumulative migration time | 27.969 s | 1.330 s |

This is an 84.2% reduction for this measured scope. It is not a full-suite speedup
claim. The remaining four migrations are the session template and tests that
explicitly exercise repeat migration or recovery. The encoder already had a
process-local cache; no embedding stub or global model replacement was added.

To repeat the comparison, run the same command at the baseline and candidate
using separate owned directories under `.local/test-temp` and the same cached
model. Keep `PYTHONPATH` bound to that checkout and use `HF_HUB_OFFLINE=1` when
reusing an existing model cache:

```powershell
.venv/Scripts/python.exe -m cProfile -o .local/performance/m0.pstats -m pytest -q tests/test_m0.py --durations=15 --basetemp=.local/test-temp/m0 -o cache_dir=.local/test-temp/m0-cache
```

The existing embedding cache remains enabled. No runtime feature, spending or
delivery permission is changed by test setup or CI selection.


## October 24 measurement and reused mechanisms

The source [full CI run](https://github.com/yanyuhanyue/companion-core/actions/runs/40062163855)
took 672 seconds from its first job starting to the final gate ending. Windows
shard 0 took 658 seconds (621.899 seconds reported by pytest); Windows shard 1
finished in 513 seconds. The previous weights described only seven files.
Repartitioning the same JUnit case totals with the updated 270-file weights gives
these estimates, excluding collection, session setup, packaging and runner queues:

| Assignment | Ubuntu maximum case-time sum | Windows maximum case-time sum |
|---|---:|---:|
| Previous two shards and weights | 592.12 s | 609.90 s |
| Updated weights, two shards | 578.57 s | 537.36 s |
| Updated weights, four shards | 291.95 s | 271.41 s |

These are historical-load simulations, not measured candidate CI wall times.
Candidate measurements belong in its PR and final CI receipt. Baseline and
candidate use the same Python, dependency lock, hosted OS labels, full test
coverage, cache configuration, native/browser/container and packaging checks;
hosted runner variability and doubled session initialization remain limitations.

The implementation reuses the existing file partitioner and native
[GitHub Actions matrix](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations),
with standard [pytest JUnit total durations](https://docs.pytest.org/en/stable/how-to/output.html#creating-junitxml-format-files).
The DSH plugin's [selfcheck](https://github.com/c1overzz/dsh-bili-scroll/blob/main/selfcheck.mjs)
was inspected: it exercises real host/client entry functions with synthetic host
services. That supports retaining our existing native-plugin and entrypoint
checks; it supplies no Python sharding facility to replace. There is no new test
executor, validation service or business protocol.

The Console-only refinement reuses the same selector, matrix, job conditions and final gate.
The pinned [DSH selfcheck](https://github.com/c1overzz/dsh-bili-scroll/blob/9a3e0e67d82feedf755c763807c806c4d125a245/selfcheck.mjs)
was rechecked: its synthetic host/client entrypoint validation supports retaining real Console
HTTP/browser checks. GitHub's [matrix outputs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations#using-an-output-to-define-two-matrices)
provide the existing mechanism for scheduling the one Console job. No dependency analysis
or new evidence/reuse format is introduced; selected results still cannot mint full-tree proof.

## October 25 local worker comparison

The same 40 M0, Console HTTP/proxy, native TLS voice cancellation and independent
worker STOP/lock cases passed in every run below, with identical JUnit case IDs.
The order was serial, parallel, parallel, serial, using the same checkout,
Python 3.12.10, dependency lock and warm model cache on Windows 11 with an
i9-14900HX, 32 logical CPUs and 31.6 GiB RAM. The entry time includes `check.ps1`,
Ruff, parent preparation, pytest and cleanup:

| Warm-cache run | Workers | Entry wall time | Pytest wall time |
|---|---:|---:|---:|
| A | 0 | 105.494 s | 95.60 s |
| A | 2 | 95.275 s | 88.20 s |
| B | 2 | 114.424 s | 106.47 s |
| B | 0 | 144.660 s | 133.71 s |

The two-sample medians are 125.077 and 104.850 seconds, a 16.2% reduction for
this scope. Background system CPU rose from 23.3% to 34.0% across the sequence;
the spread prevents a stable CI speedup claim. Process-tree samples every 0.2 s
recorded 728–730 MiB peak working sets in serial and 1058–1078 MiB in parallel,
with sampled CPU time of 108–150 and 138–164 seconds respectively. Samples can
miss short children, and summed working sets count shared pages more than once.
A separate cold-cache serial run took 123.397 seconds including the initial
public model download; it is excluded from the warm comparison. Hosted runner
images, queues, cache state and complete-suite wall time require the candidate's
CI receipt before accepting the optimization.

An offline check through the same worker entry point also ran real BGE inference
in two distinct worker processes. Each returned a finite 512-dimensional vector,
initialized its own encoder once and hit that process's encoder cache on reuse;
the pinned file cache was shared and worker scratch paths were distinct.
