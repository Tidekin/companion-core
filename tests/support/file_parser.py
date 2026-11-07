import io
import threading

import pytest

IMAGE = "companion-file-parser@sha256:" + "a" * 64


def docx_bytes(build=None):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    if build:
        build(document)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def pdf_bytes(texts, *, encrypt=False, compress=False):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    writer = pypdf.PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for text in texts:
        page = writer.add_blank_page(width=300, height=400)
        if text is None:
            continue
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 10 300 Td (" + text.encode("ascii") + b") Tj ET")
        page[NameObject("/Contents")] = content.flate_encode() if compress else content
    if encrypt:
        writer.encrypt("secret")
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class FakeProcess:
    def __init__(self, output, *, block=False, returncode=0):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(output)
        self.returncode = returncode
        self.killed = False
        self.release = threading.Event()
        if block:
            self.stdout = self

    def read(self, count):
        self.release.wait(10)
        return b""

    def close(self):
        pass

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.release.set()
