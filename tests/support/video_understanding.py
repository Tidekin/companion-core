from pydantic import SecretStr

from companion.video_input import VideoInputPolicy
from tests.support.live_image_input import options as image_options
from tests.support.private_companion import companion_options


def video_policy(**changes):
    return VideoInputPolicy(**{
        "approval_sha256": "d" * 64, "provider": "synthetic-video",
        "endpoint": "https://video.example.test/v1", "model": "configurable-video-one",
        "input_rate": 1000000, "cached_input_rate": 100000, "output_rate": 2000000,
        "estimated_tokens_per_second": 200, "max_output_tokens": 128,
        **changes,
    })


def scope_options(core, *, model, different_rates=False):
    options, image = companion_options(core), image_options()
    options.update(image_scope=image["image_scope"],
                   image_approval_sha256=image["image_approval_sha256"])
    options["core_options"].update(image["core_options"], video_model_key=SecretStr(
        "synthetic-video-provider-key-12345",
    ))
    prices = ({"input_rate": 8990, "cached_input_rate": 899, "output_rate": 44949,
               "max_output_tokens": 1000} if different_rates else {})
    options["paid_options"].update(
        image_input=image["paid_options"]["image_input"],
        video_input=video_policy(model=model, **prices), per_request_calls=4,
    )
    return options
