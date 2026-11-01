"""Closed platform policies shared by content, receipts, grants and sharing.

Platform hosts are fixed; generic web reads require an explicit configured host scope.
Observation protocol
types also retain their existing non-content sources; those confer no read/share
capability. Transport-specific scraping and credential handling stay in adapters.
"""

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from companion.contracts import CoreError
from companion.source_policy import (
    DEFINITIONS as _DEFINITIONS,
)
from companion.source_policy import (
    ContentSource as ContentSource,
)
from companion.source_policy import (
    ObservationSource as ObservationSource,
)
from companion.source_policy import (
    SourceCapability as SourceCapability,
)
from companion.source_policy import (
    SourceDefinition as SourceDefinition,
)
from companion.source_policy import source_definition as policy_definition
from companion.source_policy import web_host_allowed


def source_definition(name):
    try:
        return policy_definition(name)
    except KeyError:
        raise CoreError("CONTENT_SOURCE_NOT_ALLOWED", 400) from None


def supports(name, capability):
    return any(row.name == name and capability in row.capabilities for row in _DEFINITIONS)


def source_names(capability):
    return tuple(row.name for row in _DEFINITIONS if capability in row.capabilities)


def observation_sources(capability):
    return tuple("observation." + name for name in source_names(capability))


def _url(value):
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Invalid source URL")
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Invalid source URL")
    return parsed


def web_hosts(settings):
    if not settings.web_reading_enabled:
        return frozenset()
    return frozenset(settings.web_reading_hosts)


def content_target(value, *, web_hosts=()):
    """Extract only an allowed platform content identifier; never fetch the input URL.

    Preserve existing query handling: ordinary content URLs lose their query; the
    explicit share route requires exactly one valid link_id. Receipt URLs below
    instead reject queries entirely.
    """
    try:
        parsed = _url(value)
        if parsed.scheme != "https" or parsed.port not in {None, 443}:
            raise ValueError()
        if parsed.hostname == "b23.tv" and re.fullmatch(r"/[A-Za-z0-9]{1,64}", parsed.path):
            return "bilibili", "https://b23.tv" + parsed.path
        for definition in _DEFINITIONS:
            if parsed.hostname not in definition.content_hosts:
                continue
            match = re.fullmatch(definition.content_path, parsed.path)
            identifier = match[1] if match else ""
            if definition.query_path and parsed.path == definition.query_path:
                identifiers = parse_qs(parsed.query, keep_blank_values=True).get(
                    definition.query_identifier, []
                )
                identifier = identifiers[0] if len(identifiers) == 1 else ""
            if re.fullmatch(definition.identifier, identifier):
                if definition.lowercase_identifier:
                    identifier = identifier.lower()
                return definition.name, definition.canonical_prefix + identifier
        if (web_host_allowed(parsed.hostname, web_hosts) and "\\" not in value
            and not any(char.isspace() for char in value)):
            return "web", urlunsplit(("https", parsed.hostname, parsed.path or "/",
                                      parsed.query, ""))
    except (TypeError, ValueError):
        pass
    raise CoreError("CONTENT_URL_NOT_ALLOWED", 400)


def public_image_url(value, source):
    """Canonical download input, with the same platform-bound CDN transforms removed."""
    try:
        definition = source_definition(source)
        parsed = _url(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in definition.image_hosts
            or parsed.port not in {None, 80, 443}
            or not parsed.path.startswith("/")
        ):
            raise ValueError()
        return urlunsplit(("https", parsed.hostname, parsed.path, "", ""))
    except (CoreError, TypeError, ValueError):
        raise CoreError("CONTENT_IMAGE_URL_NOT_ALLOWED", 400) from None


def _evidence_url(value, capability, hosts):
    parsed = _url(value)
    if (
        parsed.scheme != "https"
        or parsed.port not in {None, 443}
        or parsed.query
        or not any(
            parsed.hostname in hosts(row) for row in _DEFINITIONS if capability in row.capabilities
        )
    ):
        raise ValueError("Invalid source evidence URL")
    return parsed


def validate_reading_evidence_url(value):
    _evidence_url(value, "reading", lambda row: row.reading_evidence_hosts)


def validate_article_image_url(value):
    parsed = _evidence_url(value, "article", lambda row: row.image_hosts)
    if not parsed.path.startswith("/"):
        raise ValueError("Invalid article image reference")
