"""Current exact source rules and verified-caption lexical gates, without I/O."""

import json
import re
from urllib.parse import quote, unquote

from pydantic import Field, model_validator

from companion.contracts import StrictModel
from companion_meme_protocol import caption_text


class TrustedSource(StrictModel):
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=512)
    revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    license: str = Field(min_length=1, max_length=128)
    license_path: str = Field(min_length=1, max_length=512)
    license_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    asset_prefix: str = Field(min_length=1, max_length=512)
    usage_basis: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def exact_paths(self):
        if (
            any(p in {".", ".."} for p in self.repo.split("/"))
            or self.license.casefold() in {"unknown", "unlicensed", "none"}
            or not self.license.strip()
            or not self.usage_basis.strip()
        ):
            raise ValueError("MEME_TRUST_RULE_INVALID")
        for path in (self.license_path, self.asset_prefix):
            if (
                len(path) > 512
                or "\\" in path
                or ":" in path
                or any(ord(c) < 32 for c in path)
                or any(p in {"", ".", ".."} or p.endswith((" ", ".")) for p in path.split("/"))
            ):
                raise ValueError("MEME_TRUST_RULE_INVALID")
        if len(self.basis) > 512:
            raise ValueError("MEME_TRUST_RULE_INVALID")
        return self

    @property
    def basis(self):
        return (
            self.usage_basis
            + "; license-file="
            + self.license_path
            + "; license-sha256="
            + self.license_sha256
        )


def source_rules(settings):
    rules = [TrustedSource.model_validate(r) for r in getattr(settings, "meme_trusted_sources", ())]
    if len(rules) > 32:
        raise ValueError("MEME_TRUST_RULE_INVALID")
    identities = {}
    for rule in rules:
        key = rule.repo, rule.revision
        license_key = rule.license_path, rule.license_sha256, rule.license
        if identities.setdefault(key, license_key) != license_key:
            raise ValueError("MEME_TRUST_RULE_INVALID")
    return rules


def trusted_source(metadata, rules):
    # Structural acquisition proof, original fetch hashes and byte identity are
    # independently checked by meme_assets.provenance/_proof before using this gate.
    for rule in rules:
        prefix = f"https://raw.githubusercontent.com/{rule.repo}/{rule.revision}/"
        acquired = json.loads(metadata["acquisition_json"])
        discovered = acquired["discovered_asset_url"]
        path = unquote(discovered[len(prefix) :]) if discovered.startswith(prefix) else ""
        if (
            metadata["source_page"] != f"https://github.com/{rule.repo}/tree/{rule.revision}"
            or metadata["revision"] != rule.revision
            or metadata["license"] != rule.license
            or metadata["usage_basis"] != rule.basis
            or not path.startswith(rule.asset_prefix + "/")
            or discovered != prefix + quote(path)
            or any(p in {"", ".", ".."} for p in path.split("/"))
        ):
            continue
        if "source_files" in acquired:
            if (
                metadata["asset_url"] != discovered
                or acquired["source_files"]["license"]["body_sha256"] != rule.license_sha256
            ):
                continue
        else:
            archive = f"https://codeload.github.com/{rule.repo}/zip/{rule.revision}"
            fetch = acquired["fetch"]
            if (
                metadata["asset_url"] != archive
                or acquired["archive_member"]
                != rule.repo.split("/")[-1] + "-" + rule.revision + "/" + path
                or fetch["requested_url"] != archive
                or fetch["final_url"] != archive
                or len(fetch["hops"]) != 1
                or fetch["hops"][0]["url"] != archive
                or fetch["hops"][0]["status"] != 200
            ):
                continue
        return True
    return False


EXPLICIT_FLOOR = 0.01


def caption_eligible(result, minimum=0.8):
    if (
        result.confidence < minimum
        or result.content_status != "clear"
        or result.target == "uncertain"
        or result.tone == "uncertain"
        or result.temporal_status in {"changes", "uncertain"}
        or not result.suitable
    ):
        return False
    return not (
        re.search(r"你.{0,4}(?:笨|蠢|废物)|活该|滚开|去死", result.visible_text)
        and result.tone not in {"mocking", "sarcastic", "angry"}
    )


def context_allows(caption, query):
    # Asking for a sticker is not asking to be mocked.
    if (query.get("request") and caption.target == "recipient"
            and caption.tone in {"mocking", "sarcastic", "angry"}):
        return False
    # An explicit request without a theme accepts any legal light use; object/tone stay strict.
    if query.get("request") == "any":
        if (not set(caption.suitable) - {"morning", "sleep"}
                or query["intent"] in caption.avoid):
            return False
    elif query["intent"] not in caption.suitable or query["intent"] in caption.avoid:
        return False
    if caption.target not in {"neutral", query["target"]}:
        return False
    if caption.tone == "self_deprecating" and query["target"] != "self":
        return False
    if query["intent"] == "comfort" and caption.tone not in {"warm", "neutral", "sad"}:
        return False
    return not (
        query["intent"] == "comfort"
        and re.search(r"嘲笑|嘲讽|活该|哈哈|笨蛋|废物|mock|sarcas", caption_text(caption), re.I)
    )


def tokens(text):
    terms = set(re.findall(r"[a-z0-9]{2,}", text.casefold()))
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(segment[i : i + 2] for i in range(len(segment) - 1))
    return terms


def lexical_score(caption, query):
    if not caption_eligible(caption) or not context_allows(caption, query):
        return 0.0
    overlap = len(tokens(query["text"]) & tokens(caption_text(caption)))
    if overlap:
        return min(0.49, overlap / 20)
    # A legal candidate for an explicit request stays rankable without word overlap.
    return EXPLICIT_FLOOR if query.get("request") else 0.0
