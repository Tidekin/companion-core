import base64

from companion.config import ObservationAdapterConfig
from companion.contracts import CoreError
from companion.media import MediaChunk, MediaService, MediaSessionRequest
from companion.media_config import MediaConfig, MediaEndpoint
from companion.media_provider import MediaResult
from tests.conftest import run

AUDIO = b"\x1aE\xdf\xa3test-audio-fixture"


IMAGE = b"\xff\xd8\xfftest-image-fixture"


XML = (
    b'<?xml version="1.0"?><hierarchy><node text="Visible title" />'
    b'<node text="hidden-password" password="true" /></hierarchy>'
)


class FakeMedia:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.fail = False

    async def process(self, operation, data, mime_type, endpoint):
        self.calls.append((operation, data, mime_type, endpoint.model))
        if self.callback:
            await self.callback()
        if self.fail:
            raise CoreError("MEDIA_PROVIDER_UNCERTAIN", 502)
        return (
            MediaResult(audio=b"ID3valid-audio-fixture", mime_type="audio/mpeg")
            if operation == "speech"
            else MediaResult(text="可见的测试内容")
        )


def service(core, **changes):
    core.settings.observation_enabled = True
    core.settings.voice_enabled = core.settings.camera_enabled = core.settings.android_enabled = (
        True
    )
    core.settings.observation_adapters = {
        "media_" + source: ObservationAdapterConfig(
            token=source + "-adapter-token-" * 3,
            source=source,
            media_analysis=source != "android",
        )
        for source in ("voice", "camera", "android")
    }
    endpoint = MediaEndpoint(
        url="https://media.example.test/process",
        model="fixture-media",
        key="private-media-provider-key",
        estimated_request_micro_usd=100,
    )
    options = MediaConfig(
        enabled=True,  stt=endpoint, tts=endpoint, vision=endpoint, **changes
    )
    fake = FakeMedia()
    return MediaService(core.store, core.settings, core.clock, core.observations, options, fake)


def start(media, modalities=None, **fields):
    modalities = modalities or ["voice"]
    return media.start(
        MediaSessionRequest(
            device_id=fields.pop(
                "device_id", "adb:device-1" if "android" in modalities else "browser-owner"
            ),
            modalities=modalities,
            **fields,
        )
    )


def chunk(operation="audio", key="chunk-one", data=None):
    if operation == "speech":
        return MediaChunk(request_key=key, operation=operation, mime_type="text/plain", text="你好")
    content = {"audio": AUDIO, "camera": IMAGE, "android": XML}[operation] if data is None else data
    mime = {"audio": "audio/webm", "camera": "image/jpeg", "android": "application/xml"}[operation]
    return MediaChunk(
        request_key=key,
        operation=operation,
        mime_type=mime,
        data_base64=base64.b64encode(content).decode(),
    )


def process(media, session, item):
    return run(media.process(session["session_id"], session["session_token"], item))
