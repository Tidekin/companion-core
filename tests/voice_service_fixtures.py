from companion_voice_node.providers import GPTSoVITSConfig
from companion_voice_node.speech_service import SpeechServiceConfig
from tests.test_local_voice_providers import wav_bytes


def speech_config(root):
    root.mkdir(parents=True, exist_ok=True)
    reference = root / "reference.wav"
    reference.write_bytes(wav_bytes(3))
    for name in ("gpt.ckpt", "sovits.pth", "voice.pth", "voice.index"):
        (root / name).write_bytes(b"pinned model resource")
    source = root / "applio"
    for name in (
        "rvc/models/embedders/contentvec/pytorch_model.bin",
        "rvc/models/embedders/contentvec/config.json", "rvc/models/predictors/rmvpe.pt",
        "rvc/infer/infer.py", "rvc/infer/pipeline.py", "rvc/lib/predictors/f0.py",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pinned Applio resource")
    return SpeechServiceConfig(
        model="test-applio", base=GPTSoVITSConfig(reference_audio_path=reference,
                                                prompt_text="参考音频原文"),
        gpt_weights=root / "gpt.ckpt", sovits_weights=root / "sovits.pth",
        gpt_revision="a" * 40, applio_source=source, applio_revision="b" * 40,
        model_path=root / "voice.pth", index_path=root / "voice.index",
    )
