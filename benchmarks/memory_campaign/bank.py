"""Validate and freeze a round's bank. Counts come from the bank itself.

  python bank.py validate --round DIR   checks only, writes nothing
  python bank.py freeze --round DIR     validate, then write manifests and corpus-freeze.json

Bank layout (see README.md): bank/suite-v1/{inputs/<persona>.jsonl, questions.jsonl,
review-only/criteria.jsonl, assets/}, bank/asr-observations.jsonl for every WAV.
"""

import bisect
import json
import sys
import wave
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from roundcfg import Round, read_rows, save, sha

MEDIA = {"image", "file", "voice", "relative_voice"}
QUOTED = {"quote", "forward"}


def validate(r):
    problems = []

    def check(ok, message):
        if not ok:
            problems.append(message)

    qs, gold = r.questions, r.criteria
    asr = {row["asset"]: row for row in read_rows(r.bank / "asr-observations.jsonl")}
    check(len({q["id"] for q in qs}) == len(qs), "duplicate question ids")
    check(set(gold) == {q["id"] for q in qs}, "criteria and questions differ")
    for c in gold.values():
        check(c.get("required") and isinstance(c["required"], list), f"{c['id']}: no required")
        check(isinstance(c.get("forbidden", []), list), f"{c['id']}: forbidden not a list")
        check(all(isinstance(g, list) and g and all(isinstance(x, str) and x for x in g)
                  for g in c.get("exact", [])), f"{c['id']}: exact must be non-empty string lists")
        check(isinstance(c.get("strata", {}), dict), f"{c['id']}: strata not an object")
    personas, audio, sources, per_persona = r.personas, [], Counter(), {}
    for persona in personas:
        path = r.suite / "inputs" / f"{persona}.jsonl"
        history = read_rows(path)
        check(bool(history), f"{persona}: missing inputs")
        check([row["turn"] for row in history] == list(range(1, len(history) + 1)),
              f"{persona}: turns not 1..n")
        times = [datetime.fromisoformat(row["now"]) for row in history]
        check(times == sorted(set(times)), f"{persona}: times not strictly increasing")
        ids = {row["id"]: row for row in history}
        selected = r.per_persona(persona)
        for q in selected:
            when = datetime.fromisoformat(q["now"])
            check(q["cutoff_turn"] == bisect.bisect_right(times, when), f"{q['id']}: cutoff")
            for source in q["source_ids"]:
                check(source in ids, f"{q['id']}: unknown source {source}")
                if source in ids:
                    check(ids[source]["turn"] <= q["cutoff_turn"], f"{q['id']}: future source")
                    check(datetime.fromisoformat(ids[source]["now"]) <= when,
                          f"{q['id']}: source after question")
        for row in history:
            sources[row["modality"]] += 1
            if row["modality"] in QUOTED and row.get("quoted"):
                quoted = row["quoted"]
                check(bool(quoted.get("text")), f"{row['id']}: empty quoted text")
                check(str(quoted.get("uin", "")) != "10002", f"{row['id']}: quoted as assistant")
            if row.get("asset"):
                p = r.suite / row["asset"]
                check(p.resolve().is_relative_to(r.suite.resolve()) and p.is_file(),
                      f"{row['id']}: asset missing")
                check(not row["text"], f"{row['id']}: media authoring text would reach the model")
                if p.suffix == ".wav" and p.is_file():
                    actual = asr.get(row["asset"])
                    check(bool(actual) and actual["sha256"] == sha(p) and actual["text"].strip(),
                          f"{row['id']}: ASR observation missing or stale")
                    with wave.open(str(p)) as stream:
                        seconds = stream.getnframes() / stream.getframerate()
                        check(1 < seconds < 45, f"{row['id']}: audio length {seconds:.1f}s")
                        audio.append(seconds)
                elif p.suffix == ".png" and p.is_file():
                    from PIL import Image

                    with Image.open(p) as image:
                        check(image.width * image.height <= 1_000_000, f"{row['id']}: image size")
                        image.verify()
                elif p.is_file():
                    check(bool(p.read_text("utf-8").strip()), f"{row['id']}: empty file")
            elif row["modality"] in MEDIA:
                problems.append(f"{row['id']}: media row without asset")
        per_persona[persona] = {"semantic_questions": len(selected), "turns": len(history),
                                "maximum_cutoff_turn": max((q["cutoff_turn"] for q in selected),
                                                           default=0)}
    check(len(audio) == len(asr), f"ASR observations {len(asr)} != WAV assets {len(audio)}")
    summary = {"semantic": len(qs), "personas": len(personas), "categories": len(r.categories),
               "delays": r.delays, "groups": len({q["group_id"] for q in qs}),
               "source_modalities": dict(sources), "audio_assets": len(audio),
               "audio_total_seconds": round(sum(audio), 1), "per_persona": per_persona}
    return problems, summary


def freeze(r):
    if (r.bank / "bundle-manifest.json").exists():
        raise SystemExit("Bank already frozen; never overwrite a frozen bank")
    if r.deepseek_ledger.exists():
        raise SystemExit("Bank must close before any paid call of this round")
    problems, summary = validate(r)
    if problems:
        raise SystemExit("Bank invalid:\n" + "\n".join(problems[:50]))
    manifest = {"version": 3, "classification": "SELF_AUTHORED_FICTIONAL_UNSEEN_AFTER_SOURCE_FREEZE",
                **summary, "source_freeze_sha256": sha(r.root / "source-freeze.json")
                if (r.root / "source-freeze.json").exists() else None,
                "files_sha256": {p.relative_to(r.suite).as_posix(): sha(p)
                                 for p in sorted(r.suite.rglob("*")) if p.is_file()}}
    save(r.suite / "manifest.json", manifest)
    save(r.bank / "bundle-manifest.json", {
        "semantic": summary["semantic"], "independent_lifecycle_cases": 18,
        "files_sha256": {p.relative_to(r.bank).as_posix(): sha(p)
                         for p in sorted(r.bank.rglob("*")) if p.is_file()}})
    save(r.root / "corpus-freeze.json", {
        "status": "FROZEN_SCHEMA_CHRONOLOGY_MEDIA_PASSED", "at": datetime.now(UTC).isoformat(),
        "bundle_manifest_sha256": sha(r.bank / "bundle-manifest.json"),
        "questions_sha256": sha(r.suite / "questions.jsonl"),
        "criteria_sha256": sha(r.suite / "review-only/criteria.jsonl"),
        "asr_sha256": sha(r.bank / "asr-observations.jsonl")
        if (r.bank / "asr-observations.jsonl").exists() else None, **summary})
    return summary


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "validate"
    r = Round()
    if command == "validate":
        problems, summary = validate(r)
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        if problems:
            print("\n".join(["PROBLEMS:", *problems[:100]]))
            raise SystemExit(1)
    elif command == "freeze":
        print(json.dumps(freeze(r), ensure_ascii=False, indent=1))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / "runner")]
    main()
