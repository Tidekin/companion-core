import io

from PIL import Image


def canvas_gif(sizes, *, offsets=None):
    """GIF image descriptors may extend the logical screen of an earlier frame."""
    data = b""
    for index, size in enumerate(sizes):
        stream = io.BytesIO()
        frame = Image.new("P", size, index % 2)
        frame.putpalette([0, 0, 0, 255, 0, 0] + [0, 0, 0] * 254)
        frame.save(stream, format="GIF", optimize=False)
        encoded = bytearray(stream.getvalue())
        descriptor = 13 + 3 * (2 ** ((encoded[10] & 7) + 1))
        assert encoded[descriptor] == 44
        if offsets:
            x, y = offsets[index]
            encoded[descriptor + 1:descriptor + 3] = x.to_bytes(2, "little")
            encoded[descriptor + 3:descriptor + 5] = y.to_bytes(2, "little")
        data = data[:-1] + encoded[descriptor:] if data else bytes(encoded)
    return bytes(data)
