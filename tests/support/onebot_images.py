


URL = "https://multimedia.nt.qq.com.cn/download?appid=1407&rkey=synthetic-private-reference"


def image_segment(url=URL, *, subtype=0):
    return {
        "type": "image",
        "data": {"url": url, "file": "synthetic-file", "summary": "", "sub_type": subtype},
    }
