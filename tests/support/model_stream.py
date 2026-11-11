"""Synthetic provider SSE frames shared by model, worker and native acceptance."""

import json


def chunk(delta=None, *, finish=None, usage=None):
    data = {"choices": [] if usage is not None else [{
        "index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        data["usage"] = usage
    return ("data: " + json.dumps(data, ensure_ascii=False) + "\r\n\r\n").encode()
