import threading

from companion import meme_index as index


def vector(first=1.0, second=0.0):
    return [first, second, *([0.0] * 510)]


class Encoder:
    def passage_embed(self, texts, *, batch_size):
        assert type(texts) is tuple and len(texts) <= 32 and batch_size == 32
        assert len(texts) == len(set(texts))
        return (vector() for _ in texts)

    def query_embed(self, query):
        assert type(query) is str and len(query) <= 2000
        return iter([vector()])


def local_encoder(monkeypatch, factory=Encoder):
    calls = []
    main_thread = threading.get_ident()

    def load(path, *, local_only):
        assert local_only is True and threading.get_ident() != main_thread
        assert index._inference_lock(path).locked()
        calls.append(path)
        return factory()

    monkeypatch.setattr(index, "encoder", load)
    return calls
