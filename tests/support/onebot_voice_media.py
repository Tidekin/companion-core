import wave
from io import BytesIO

URL = "https://multimedia.nt.qq.com.cn/download/voice?key=private-signed-token"


def wav(seconds=1, *, rate=16000, channels=1):
    result = BytesIO()
    with wave.open(result, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x01\x00" * (int(seconds * rate) * channels))
    return result.getvalue()


def segment(**changes):
    return {"type": "record", "data": {"file": "cache-123.amr",
                                        "url": URL, **changes}}


async def public(host, port):
    assert (host, port) == ("multimedia.nt.qq.com.cn", 443)
    return ["93.184.216.34"]
