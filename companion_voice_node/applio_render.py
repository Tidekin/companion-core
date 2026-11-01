"""Offline native Applio singing, intended for a supervised single-job process.

The caller owns admission, process limits, cancellation, custody and delivery.
This module receives one bounded local PCM WAV, never a URL or platform credential.
"""

import argparse
import contextlib
import json
import math
import os
import random
import sys
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from companion_voice_node.providers import LocalVoiceError
from companion_voice_node.resources import file_digest
from companion_voice_node.singing_audio import RATE, bubble_manifest, split_song_pcm


class ApplioRenderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    applio_source: Path
    separator_source: Path
    voice_model: Path
    voice_index: Path
    separator_model: Path
    dependency_overlay: Path | None = None
    resource_sha256: dict[str, str] = Field(min_length=10, max_length=10)

    @field_validator("applio_source", "separator_source", "voice_model", "voice_index",
                     "separator_model", "dependency_overlay")
    @classmethod
    def real_local_path(cls, value):
        if value is None:
            return None
        if (not value.is_absolute() or str(value).startswith(("\\\\", "//"))
            or value.is_symlink() or value.resolve() != value.absolute()):
            raise ValueError("explicit real local paths required")
        return value

    def resources(self):
        return {
            "voice_model": self.voice_model,
            "voice_index": self.voice_index,
            "separator_model": self.separator_model,
            "applio_core": self.applio_source / "core.py",
            "applio_infer": self.applio_source / "rvc/infer/infer.py",
            "applio_pipeline": self.applio_source / "rvc/infer/pipeline.py",
            "contentvec": self.applio_source / "rvc/models/embedders/contentvec/pytorch_model.bin",
            "contentvec_config": self.applio_source / "rvc/models/embedders/contentvec/config.json",
            "rmvpe": self.applio_source / "rvc/models/predictors/rmvpe.pt",
            "separator_code": self.separator_source / "vr.py",
        }

    def verify(self):
        resources = self.resources()
        if set(resources) != set(self.resource_sha256):
            raise LocalVoiceError("SINGING_RESOURCE_SET_INVALID")
        actual = {name: file_digest(path) for name, path in resources.items()}
        if any(row["sha256"] != self.resource_sha256[name] for name, row in actual.items()):
            raise LocalVoiceError("SINGING_RESOURCE_CHANGED")
        return actual


def _read(path, sf, np, *, maximum=128 * 1024 * 1024):
    if (not path.is_file() or path.is_symlink() or path.resolve() != path.absolute()
        or not 44 <= path.stat().st_size <= maximum):
        raise LocalVoiceError("SINGING_INPUT_INVALID")
    info = sf.info(path)
    if (info.format != "WAV" or info.subtype != "PCM_16" or info.channels not in {1, 2}
        or info.samplerate not in {32000, 44100, 48000} or not 0 < info.duration <= 600):
        raise LocalVoiceError("SINGING_INPUT_INVALID")
    data, rate = sf.read(path, dtype="float32", always_2d=True)
    if len(data) != info.frames or not np.isfinite(data).all():
        raise LocalVoiceError("SINGING_INPUT_INVALID")
    return data, rate


def render(config: ApplioRenderConfig, source: Path, output: Path):
    """Separate, convert, mix and partition one song in a fresh caller-owned directory."""
    resources = config.verify()
    if config.dependency_overlay is not None:
        # Operator-owned installed packages, after the primary environment's packages.
        sys.path.append(str(config.dependency_overlay))
    source, output = source.absolute(), output.absolute()
    if (source.resolve() != source or output.resolve() != output or output.exists()
        or str(output).startswith(("\\\\", "//"))):
        raise LocalVoiceError("SINGING_OUTPUT_INVALID")
    # These flags prevent Hugging Face helpers from downloading missing assets.
    # OS network/process isolation remains the supervising caller's responsibility.
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", OMP_NUM_THREADS="2", MKL_NUM_THREADS="2")
    import numpy as np
    import soundfile as sf
    import torch
    from scipy.signal import resample_poly

    if not torch.cuda.is_available():
        raise LocalVoiceError("SINGING_CUDA_UNAVAILABLE")
    torch.set_num_threads(2)
    original, source_rate = _read(source, sf, np)
    source_sha = file_digest(source)["sha256"]
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    sys.path.insert(0, str(config.separator_source))
    from vr import AudioPre

    torch.manual_seed(42)
    random.seed(42)
    np.random.seed(42)
    native = output / "native"
    native.mkdir()
    separation_started = time.monotonic()
    processor = AudioPre(10, str(config.separator_model), "cuda", False)
    processor._path_audio_(str(source), str(native), str(native), "wav")
    del processor
    torch.cuda.empty_cache()
    separation_seconds = time.monotonic() - separation_started
    vocal_path = native / f"vocal_{source.name}_10.wav"
    backing_path = native / f"instrument_{source.name}_10.wav"
    vocal_source, rate = _read(vocal_path, sf, np)
    backing, backing_rate = _read(backing_path, sf, np)
    if rate != backing_rate or len(vocal_source) != len(backing):
        raise LocalVoiceError("SINGING_SEPARATION_ALIGNMENT_INVALID")
    if abs(len(backing) / rate - len(original) / source_rate) > 0.2:
        raise LocalVoiceError("SINGING_SOURCE_COVERAGE_INVALID")
    sys.path.insert(0, str(config.applio_source))
    with contextlib.chdir(config.applio_source):
        import core as applio_core

        converted_path = native / "converted.wav"
        conversion_started = time.monotonic()
        applio_core.run_infer_script(pitch=0, index_rate=0.35, volume_envelope=1.0,
            protect=0.33, f0_method="rmvpe", input_path=str(vocal_path),
            output_path=str(converted_path), pth_path=str(config.voice_model),
            index_path=str(config.voice_index), f0_autotune=False, f0_autotune_strength=0,
            proposed_pitch=False, proposed_pitch_threshold=155, export_format="WAV",
            embedder_model="contentvec", cleanup_mode="off")
    conversion_seconds = time.monotonic() - conversion_started
    converted, converted_rate = _read(converted_path, sf, np)
    if abs(len(converted) / converted_rate - len(backing) / rate) > 0.2:
        raise LocalVoiceError("SINGING_CONVERSION_ALIGNMENT_INVALID")
    divisor = math.gcd(rate, converted_rate)
    converted = resample_poly(converted, rate // divisor, converted_rate // divisor, axis=0)
    if len(converted) < len(backing):
        converted = np.pad(converted, ((0, len(backing) - len(converted)), (0, 0)))
    converted = converted[:len(backing)]
    seconds = len(backing) // rate
    if seconds:
        before = vocal_source.mean(axis=1)[:seconds * rate].reshape(seconds, rate)
        after = converted.mean(axis=1)[:seconds * rate].reshape(seconds, rate)
        missing = np.flatnonzero((np.sqrt(np.mean(before ** 2, axis=1)) > 0.01)
            & (np.sqrt(np.mean(after ** 2, axis=1)) < 0.0001)).tolist()
        if missing:
            raise LocalVoiceError("SINGING_VOCAL_COVERAGE_INVALID")
    mixed = converted + backing
    if not np.isfinite(mixed).all() or np.max(np.abs(mixed)) < 0.0001:
        raise LocalVoiceError("SINGING_RESULT_INVALID")
    fade = min(int(rate * 0.03), len(mixed) // 2)
    if fade:
        mixed[:fade] *= np.linspace(0, 1, fade)[:, None]
        mixed[-fade:] *= np.linspace(1, 0, fade)[:, None]
    mixed *= 0.891 / max(float(np.max(np.abs(mixed))), 0.891)
    full_path = output / "full.wav"
    sf.write(full_path, mixed, rate, subtype="PCM_16")
    divisor = math.gcd(RATE, rate)
    mono = resample_poly(mixed.mean(axis=1), RATE // divisor, rate // divisor)
    pause = resample_poly(converted.mean(axis=1), RATE // divisor, rate // divisor)
    mono *= 0.891 / max(float(np.max(np.abs(mono))), 0.891)
    pcm = np.rint(mono * 32767).astype("<i2").tobytes()
    pause_pcm = np.rint(np.clip(pause, -1, 1) * 32767).astype("<i2").tobytes()
    bubbles = split_song_pcm(pcm, pause_pcm=pause_pcm)
    manifest = bubble_manifest(pcm, bubbles)
    for part in bubbles:
        (output / f"bubble-{part.index + 1:02d}.wav").write_bytes(part.audio.audio)
    if config.verify() != resources or file_digest(source)["sha256"] != source_sha:
        raise LocalVoiceError("SINGING_RESOURCE_CHANGED")
    receipt = {"source_sha256": source_sha, "resources": resources,
        "full": {**file_digest(full_path), "frames": len(mixed), "sample_rate": rate},
        "seed": 42, "separation_seconds": round(separation_seconds, 3),
        "conversion_seconds": round(conversion_seconds, 3),
        "elapsed_seconds": round(time.monotonic() - started, 3), **manifest,
        "core_connected": False, "qq_sent": False}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    config = ApplioRenderConfig.model_validate_json(args.config.read_bytes())
    # Upstream console output is not a result or a source of private transcripts.
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), \
            contextlib.redirect_stderr(quiet):
        result = render(config, args.input, args.output)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
