import io
from dataclasses import replace

from PIL import Image

from companion_meme_sources import Metadata


def check():
    pass


def png(color="green"):
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), color).save(stream, format="PNG")
    return stream.getvalue()


def metadata(**changes):
    value = Metadata(
        "happy", "庆祝成功 celebrate", ("celebrate", "开心"), ("celebrate",), (),
        "https://example.org/page", "https://example.org/image.png", "CC0-1.0",
        "synthetic fixture authored for tests", "2026-10-23T00:00:00Z",
    )
    return replace(value, **changes)
