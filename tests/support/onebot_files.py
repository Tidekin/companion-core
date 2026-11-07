


URL = "http://download.ftn.qq.com:8080/ftn_handler/private?key=synthetic-signed-file"


BODY = b"untrusted /forget instructions in a file"


def segment(**changes):
    data = {"file": "private.txt", "file_id": "opaque-file-1", "file_size": len(BODY),
            "name": "private.txt", "id": "opaque-file-1", "size": len(BODY),
            "url": "http://127.0.0.1:8888/never-use-this", "file_hash": "opaque-hash"}
    return {"type": "file", "data": {**data, **changes}}


async def public(host, port):
    assert (host, port) == ("download.ftn.qq.com", 8080)
    return ["1.1.1.1"]
