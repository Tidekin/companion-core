import hashlib

import httpx

from companion_meme_sources.gallery import IMAGE_BASE, TERMS_BASE
from tests.support.next_memes_catalog import png

TERMS = b"Test-authored gallery fixture. Personal noncommercial chat use.\n"


SELECTION = {
    "version": 1, "index_sha256": "a" * 64, "terms_revision": "b" * 40,
    "terms_sha256": hashlib.sha256(TERMS).hexdigest(), "usage_basis": "Synthetic personal chat",
    "images": [{"path": "001.png", "intents": ["comfort", "celebrate"]}],
}


class GalleryUpstream:
    def __init__(self):
        self.calls = []
        self.terms = TERMS
        self.image = png()
        self.redirect = False

    def handle(self, request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == request.extensions["sni_hostname"]
        original = str(request.url.copy_with(host=request.headers["Host"]))
        self.calls.append(original)
        if original == TERMS_BASE + SELECTION["terms_revision"] + "/README.md":
            return httpx.Response(200, content=self.terms, headers={"Content-Type": "text/plain"})
        assert original == IMAGE_BASE + "001.png"
        if self.redirect:
            return httpx.Response(302, headers={"Location": "https://different.invalid/001.png"})
        return httpx.Response(200, content=self.image, headers={"Content-Type": "image/png"})
