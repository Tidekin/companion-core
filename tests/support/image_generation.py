import base64
import io

import httpx
from PIL import Image
from pydantic import SecretStr

from companion.media_config import ImageGenerationConfig, MediaEndpoint
from tests.sdk_reader_fixture import answer, call
from tests.support.native_image_pipeline import admit_image

URL = "https://images.example.test/v1/images/generations"


def png(color="orange", size=(16, 12)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return stream.getvalue()


def config(**changes):
    values = {
        "enabled": True,
        "endpoint": MediaEndpoint(url=URL, key=SecretStr("synthetic-image-provider-key"),
                                  model="gpt-image-1", estimated_request_micro_usd=40000),
        **changes,
    }
    return ImageGenerationConfig(**values)


def image_reply(data):
    return httpx.Response(200, json={"created": 1, "data": [
        {"b64_json": base64.b64encode(data).decode()}]})


def draw(prompt="一只在窗边晒太阳的橘猫", size="1024x1024", call_id="draw-1"):
    return answer(calls=[call(call_id, "generate_image", {"prompt": prompt, "size": size})])


async def owner_text(core, client, config_, plan, tmp_path, text, number=42):
    event, _ = await admit_image(core, client, config_, plan, tmp_path, number=number,
                                 message=[{"type": "text", "data": {"text": text}}])
    return event
