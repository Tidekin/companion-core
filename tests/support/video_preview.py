import io

from PIL import Image

FORMATS = [("GIF", "image/gif"), ("WEBP", "image/webp"), ("PNG", "image/apng")]


COLORS = ["white", "red", "lime", "blue", "yellow", "magenta", "cyan"]


def animation(format, *, count=4, size=(48, 24), poster=False, fractional=False):
    frames = [Image.new("RGBA", size, color) for color in COLORS[:count]]
    if poster:
        frames.insert(0, Image.new("RGBA", size, "black"))
    output = io.BytesIO()
    durations = [1.4] * count if fractional else [80, 10, 10, 80, 120, 400, 80][:count]
    frames[0].save(output, format=format, save_all=True, append_images=frames[1:],
                   default_image=poster, duration=durations, loop=0,
                   optimize=False, lossless=True)
    return output.getvalue()
