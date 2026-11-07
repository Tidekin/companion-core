import base64
import io

from PIL import Image

from companion import budget
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live


def image_messages(size=(2, 2)):
    stream = io.BytesIO()
    Image.new("RGB", size, "white").save(stream, format="PNG")
    data = stream.getvalue()
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "只描述这张当前图片。"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
                    },
                },
            ],
        }
    ]


def image_approval(core, tmp_path, **changes):
    policy = dict(
        approval_sha256="b" * 64,
        estimated_tokens_per_image=1000,
        max_bytes_per_image=100000,
        max_pixels_per_image=10000,
        max_images_per_request=1,
    )
    policy.update(changes.pop("policy", {}))
    approval = approved_live(
        core,
        tmp_path,
        **{
            "image_input": policy,
            "per_request_calls": 2,
            **changes,
        },
    )
    core.settings.content.use_chat_model_for_vision = True
    core.settings.content.image_daily_micro_usd = 100000
    return approval


def reserve_image(core, messages=None, *, number=1, **kwargs):
    purposes = ("chat", "image") if core.settings.paid_model.image_input else ("chat",)
    job = authorized_job(core, purposes=purposes, number=number)
    request = budget.reserve(
        core.store,
        core.settings,
        job,
        "content_vision",
        messages or image_messages(),
        core.clock(),
        logical_request_id=job,
        **kwargs,
    )
    return job, request
