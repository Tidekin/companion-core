import io

from PIL import Image

from companion.image_input import ImageInputPolicy


def animation(format="GIF", *, count=3, size=(12, 8)):
    frames = [Image.new("RGBA", size, color) for color in ("red", "green", "blue")]
    output = io.BytesIO()
    frames[0].save(output, format=format, save_all=True,
                   append_images=[frames[i % 3] for i in range(1, count)],
                   duration=100, loop=0, lossless=True)
    return output.getvalue()


def policy(**changes):
    return ImageInputPolicy(approval_sha256="a" * 64, estimated_tokens_per_image=100,
                            max_bytes_per_image=50000, max_pixels_per_image=10000, **changes)
