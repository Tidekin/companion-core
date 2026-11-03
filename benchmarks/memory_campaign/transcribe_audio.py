"""Real local ASR over a round's bank WAVs; expected spoken text is never read.

  <voice venv python> transcribe_audio.py --round DIR [--voice-source <checkout with companion_voice_node>]
     [--model-dir <faster-whisper model dir>]
Appends bank/asr-observations.jsonl (resumable, one row per WAV).
"""

import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


ROOT = Path(arg("--round", os.environ.get("MEMORY_ROUND", ""))).resolve()
CFG = json.loads((ROOT / "round.json").read_text("utf-8"))
BANK = ROOT / CFG.get("bank", "bank")
sys.path.insert(0, arg("--voice-source", CFG.get("source_checkout",
                       "E:/Companion Core/.local/worktrees/memory-recall-probes-20261102")))
from companion_voice_node.providers import FasterWhisperConfig, FasterWhisperProvider  # noqa: E402

provider = FasterWhisperProvider(FasterWhisperConfig(
    model_dir=Path(arg("--model-dir", "E:/Companion Core/.local/next-features-handoff/"
                                      "voice-runtime/models/asr-small")),
    device="cpu", compute_type="int8", beam_size=3, cpu_threads=4))
output = BANK / "asr-observations.jsonl"
done = {json.loads(s)["asset"] for s in output.read_text("utf-8").splitlines()} \
    if output.exists() else set()
for asset in sorted((BANK / "suite-v1/assets").glob("*.wav")):
    name = asset.relative_to(BANK / "suite-v1").as_posix()
    if name in done:
        continue
    data = asset.read_bytes()
    result = provider.transcribe(data)
    with output.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"asset": name, "sha256": hashlib.sha256(data).hexdigest(),
                                 "provider": "FasterWhisperProvider small CPU int8 beam3",
                                 **asdict(result)}, ensure_ascii=False) + "\n")
    print(name, len(result.text), flush=True)
