from companion_voice_node.providers import GPTSoVITSConfig
from companion_voice_node.speech_service import SpeechServiceConfig
from tests.test_local_voice_providers import wav_bytes


def speech_config(root):
    root.mkdir(parents=True, exist_ok=True)
    reference = root / "reference.wav"
    reference.write_bytes(wav_bytes(3))
    for name in ("gpt.ckpt", "sovits.pth"):
        (root / name).write_bytes(b"pinned model resource")
    source = root / "gpt-source"
    for name in (
        "api_v2.py", "GPT_SoVITS/TTS_infer_pack/TTS.py", "GPT_SoVITS/module/models.py",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pinned native GPT resource")
    return SpeechServiceConfig(
        model="test-reference-voice", base=GPTSoVITSConfig(reference_audio_path=reference,
                                                prompt_text="参考音频原文"),
        gpt_weights=root / "gpt.ckpt", sovits_weights=root / "sovits.pth",
        gpt_revision="a" * 40, gpt_source=source,
    )
