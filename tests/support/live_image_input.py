import io

from PIL import Image

from companion.config import ObservationAdapterConfig
from companion.image_input import ImageInputPolicy
from companion.media_config import ContentConfig


def png():
    out = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(out, format="PNG")
    return out.getvalue()


def options():
    image = ImageInputPolicy(
        approval_sha256="e" * 64,
        estimated_tokens_per_image=100,
        max_bytes_per_image=50000,
        max_pixels_per_image=100,
    )
    return {
        "image_scope": "owner_image_v1",
        "image_approval_sha256": image.approval_sha256,
        "core_options": {
            "content_enabled": True,
            "observation_enabled": True,
            "content": ContentConfig(use_chat_model_for_vision=True),
            "observation_adapters": {
                "content_image": ObservationAdapterConfig(
                    token="synthetic-image-adapter-token-12345",
                    source="qq_image",
                    media_analysis=True,
                )
            },
        },
        "paid_options": {"per_request_calls": 2, "image_input": image},
    }


def image_parts(request):
    return [part for message in request["messages"] if isinstance(message["content"], list)
            for part in message["content"] if part["type"] == "image_url"]


async def resolve(host, port):
    return ["1.1.1.1"]
