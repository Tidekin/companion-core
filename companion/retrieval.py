"""Local Chinese embeddings; SQLite remains the only memory/index store."""

import asyncio
import hashlib
from collections import OrderedDict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from threading import Lock

from companion.contracts import CoreError
from companion.store import encode

MODEL = "BAAI/bge-small-zh-v1.5"
REPOSITORY = "Qdrant/bge-small-zh-v1.5"
REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
INDEX_VERSION = f"{MODEL}@{REVISION}"


@lru_cache(maxsize=2)
def _inference_lock(cache_dir):
    # Keep one inference per cached model, including cancelled callers still finishing CPU work.
    return Lock()


_queries = ContextVar("retrieval_queries", default=None)


@asynccontextmanager
async def query_scope():
    """Bound query reuse to one reply, including its to_thread inference calls."""
    cache = {"values": OrderedDict(), "lock": Lock(), "closed": False}
    token = _queries.set(cache)
    try:
        yield
    finally:
        _queries.reset(token)
        with cache["lock"]:
            cache["closed"] = True
            cache["values"].clear()


def query_vector(model, query):
    """Reuse only within the current reply and encoder. Caller holds its inference lock."""
    import numpy as np

    cache = _queries.get()
    key = (model, query)
    if cache is not None:
        with cache["lock"]:
            if key in cache["values"]:
                cache["values"].move_to_end(key)
                return cache["values"][key]
    vector = np.asarray(next(model.query_embed(query)), dtype="<f4")
    if cache is not None:
        with cache["lock"]:
            if not cache["closed"]:
                cache["values"][key] = vector
                while len(cache["values"]) > 64:
                    cache["values"].popitem(last=False)
    return vector


@lru_cache(maxsize=2)
def encoder(cache_dir, *, local_only=False):
    from fastembed import TextEmbedding
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    options = dict(
        repo_id=REPOSITORY,
        revision=REVISION,
        cache_dir=cache_dir,
        allow_patterns=["*.json", "*.txt", "model_optimized.onnx"],
        max_workers=2,
    )
    try:
        path = snapshot_download(**options, local_files_only=True)
        if not (Path(path) / "model_optimized.onnx").is_file():
            raise LocalEntryNotFoundError("Incomplete model snapshot")
    except LocalEntryNotFoundError:
        if local_only:
            raise
        path = snapshot_download(**options)
    return TextEmbedding(
        MODEL,
        specific_model_path=path,
        cache_dir=cache_dir,
        threads=2,
        providers=["CPUExecutionProvider"],
        local_files_only=True,
    )


class SemanticIndex:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    def warmup(self):
        try:
            return encoder(str(Path(self.settings.embedding_cache_dir).resolve()))
        except Exception as error:
            raise CoreError("MEMORY_INDEX_UNAVAILABLE", 503) from error

    def _prepare(self, rows):
        import numpy as np

        cached = {
            row["memory_id"]: row
            for row in self.store.rows(
                "SELECT * FROM memory_vectors WHERE model_version=? "
                "AND memory_id IN (SELECT value FROM json_each(?))",
                (INDEX_VERSION, encode([row["memory_id"] for row in rows])),
            )
        }
        vectors, missing = {}, []
        for row in rows:
            key = hashlib.sha256(row["content"].encode()).hexdigest()
            old = cached.get(row["memory_id"])
            if old and old["content_hash"] == key:
                vectors[row["memory_id"]] = np.frombuffer(old["vector"], dtype="<f4")
            else:
                missing.append((row, key))
        return vectors, missing

    def _infer(self, query, contents):
        """Pure inference only; fully consume lazy generators before leaving the thread."""
        import numpy as np

        with _inference_lock(str(Path(self.settings.embedding_cache_dir).resolve())):
            model = self.warmup()
            encoded = tuple(np.asarray(v, dtype="<f4") for v in
                            model.passage_embed(contents, batch_size=32)) if contents else ()
            return encoded, query_vector(model, query)

    def _finish(self, vectors, missing, inferred):
        import numpy as np

        encoded, question = inferred
        if missing:
            for (row, key), vector in zip(missing, encoded, strict=True):
                vector = np.asarray(vector, dtype="<f4")
                vectors[row["memory_id"]] = vector
                # A concurrent forget/correction must not recreate a stale derived index.
                self.store.execute(
                    "INSERT OR REPLACE INTO memory_vectors "
                    "SELECT memory_id,?,?,? FROM memories WHERE memory_id=? AND content=? "
                    "AND status IN ('HYPOTHESIS','CONFIRMED') AND confidence>0",
                    (INDEX_VERSION, key, vector.tobytes(), row["memory_id"], row["content"]),
                )
        # ponytail: exact scan suits a single owner's memories; use an ANN index at larger scale.
        return {key: float(np.dot(value, question)) for key, value in vectors.items()}

    def scores(self, query, rows):
        if not rows:
            return {}
        vectors, missing = self._prepare(rows)
        return self._finish(vectors, missing,
                            self._infer(query, tuple(row["content"] for row, _ in missing)))

    async def scores_async(self, query, rows, *, revalidate):
        if not rows:
            return {}
        vectors, missing = self._prepare(rows)
        inferred = await asyncio.to_thread(
            self._infer, query, tuple(row["content"] for row, _ in missing),
        )
        # No Store access occurs in the worker. Cancellation never reaches cache writes.
        revalidate()
        return self._finish(vectors, missing, inferred)


def fuse(lexical, semantic):
    """Reciprocal rank fusion, independent of the two scorers' numeric scales."""
    scores = {}
    for ranking in (lexical, semantic):
        for rank, key in enumerate(ranking, 1):
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    return scores
