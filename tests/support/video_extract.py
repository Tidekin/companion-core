import hashlib
from fractions import Fraction

import av
from PIL import Image

from companion.video_media_contract import ClipRange, DownloadedMedia
from tests.support.video_media_contract import target


def local_media(tmp_path, *, origin=0, offset=0, container_format="mp4"):
    path = tmp_path / ("source." + container_format)
    with av.open(str(path), "w", format=container_format) as container:
        stream = container.add_stream("libx264" if container_format == "mp4" else "libvpx-vp9",
                                      rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        stream.options = ({"threads": "1", "preset": "veryfast"} if container_format == "mp4"
                          else {"threads": "1", "deadline": "realtime", "cpu-used": "8"})
        for index in range(30):
            frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), (index * 8, 30, 90)))
            frame.pts, frame.time_base = index + origin, Fraction(1, 10)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    value = target(max(3, offset + 3))
    data = path.read_bytes()
    artifact = DownloadedMedia(value.sha256, str(path), hashlib.sha256(data).hexdigest(), len(data),
                               "video/" + container_format, ClipRange(offset, offset + 3))
    return value, artifact
