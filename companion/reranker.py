"""Optional local ONNX cross-encoder (e.g. bge-reranker-v2-m3 int8); never downloads."""

import os
from functools import lru_cache
from pathlib import Path
from threading import Lock

from companion.contracts import CoreError

MAX_LENGTH = 320
AUTO_NAME = "local-bge-reranker-v2-m3-int8"
_lock = Lock()


def directory(settings):
    """Configured local model directory, or None: retrieval then keeps fused order."""
    value = getattr(settings, "reranker_model_dir", "")
    if not value:
        return None
    path = Path(settings.embedding_cache_dir) / AUTO_NAME if value == "auto" else Path(value)
    return str(path) if (path / "tokenizer.json").is_file() else None


@lru_cache(maxsize=1)
def _model(directory):
    import onnxruntime
    from tokenizers import Tokenizer

    root = Path(directory).resolve()
    files = sorted((root / "onnx").glob("*.onnx")) if (root / "onnx").is_dir() else []
    if len(files) != 1 or not (root / "tokenizer.json").is_file():
        raise CoreError("MEMORY_RERANKER_UNAVAILABLE", 503)
    tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
    tokenizer.enable_truncation(MAX_LENGTH)
    tokenizer.enable_padding(pad_id=tokenizer.token_to_id("<pad>") or 1, pad_token="<pad>")
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 2))
    session = onnxruntime.InferenceSession(str(files[0]), options,
                                           providers=["CPUExecutionProvider"])
    return tokenizer, session


def scores(directory, query, docs, *, batch_size=8):
    import numpy as np

    try:
        tokenizer, session = _model(str(directory))
    except CoreError:
        raise
    except Exception as error:
        raise CoreError("MEMORY_RERANKER_UNAVAILABLE", 503) from error
    names = {item.name for item in session.get_inputs()}
    result = []
    with _lock:
        for start in range(0, len(docs), batch_size):
            batch = tokenizer.encode_batch([(query, doc) for doc in docs[start:start + batch_size]])
            feed = {"input_ids": np.array([item.ids for item in batch], dtype=np.int64),
                    "attention_mask": np.array([item.attention_mask for item in batch],
                                               dtype=np.int64)}
            if "token_type_ids" in names:
                feed["token_type_ids"] = np.array([item.type_ids for item in batch],
                                                  dtype=np.int64)
            logits = session.run(None, {key: value for key, value in feed.items()
                                        if key in names})[0]
            values = np.asarray(logits).reshape(len(batch), -1)[:, 0]
            result.extend(float(value) for value in values)
    return result
