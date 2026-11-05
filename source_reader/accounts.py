"""Explicit account grants in isolated Reader storage; never discover browser/App cookies."""

import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from source_reader.network import ReadFailure

HEYBOX_COMMIT = "90d3c9d4d051f912d80ba02b38d5f8f0a5a95aac"
BILIBILI_COMMIT = "9a3e0e67d82feedf755c763807c806c4d125a245"
QZONE_COMMIT = "e2c6da0ffea8937adac690352647bb023c312759"


def _grant_identity(info):
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    ):
        raise ValueError("grant")
    if os.name != "nt" and info.st_mode & 0o077:
        raise ValueError("permissions")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        # Windows lstat/fstat can expose creation/change times differently.
        info.st_ctime_ns if os.name != "nt" else None,
    )


def account_grant(path, now, source="heybox"):
    if path is None:
        raise ReadFailure("OWNER_ACCOUNT_AUTHORIZATION_REQUIRED")
    try:
        path = Path(path)
        identity = _grant_identity(path.lstat())
        flags = os.O_RDONLY
        for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY"):
            flags |= getattr(os, name, 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if _grant_identity(os.fstat(stream.fileno())) != identity:
                raise ValueError("grant changed")
            raw = stream.read(8193)
            if (
                _grant_identity(os.fstat(stream.fileno())) != identity
                or _grant_identity(path.lstat()) != identity
            ):
                raise ValueError("grant changed")
        if len(raw) > 8192:
            raise ValueError("size")
        data = json.loads(raw)
        fields = {"enabled", "expires_at", "cookies", "upstream_commit"}
        if source == "heybox":
            fields |= {"account_id", "device_id"}
        if not isinstance(data, dict) or set(data) not in (fields, fields | {"authorization"}):
            raise ValueError("schema")
        if data["enabled"] is not True:
            raise ValueError("disabled")
        if data["expires_at"] is None:
            if source != "bilibili" or "authorization" not in data:
                raise ValueError("expiry required")
        else:
            expires = datetime.fromisoformat(data["expires_at"])
            if expires.tzinfo is None or expires <= now:
                raise ValueError("expired")
        expected = {"heybox": HEYBOX_COMMIT, "bilibili": BILIBILI_COMMIT,
                    "qzone": QZONE_COMMIT}[source]
        if data["upstream_commit"] != expected:
            raise ReadFailure("PLUGIN_UNAVAILABLE")
        if source == "heybox" and (
            not re.fullmatch(r"[0-9]{1,32}", data["account_id"])
            or not re.fullmatch(r"[0-9a-fA-F]{32}", data["device_id"])
        ):
            raise ValueError("identity")
        cookies = data["cookies"]
        if not isinstance(cookies, dict) or not 1 <= len(cookies) <= 20:
            raise ValueError("cookies")
        for key, value in cookies.items():
            if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", key) or not re.fullmatch(
                r"[\x21-\x3a\x3c-\x7e]{1,2048}", value
            ):
                raise ValueError("header")
        if source == "bilibili" and not cookies.get("SESSDATA"):
            raise ValueError("identity")
        if source == "qzone" and (
            not re.fullmatch(r"o?[1-9][0-9]{4,19}", cookies.get("uin", ""))
            or not (cookies.get("p_skey") or cookies.get("skey"))
            or set(cookies) - {"uin", "p_uin", "skey", "p_skey"}
        ):
            raise ValueError("identity")
        if "authorization" in data:
            authorization = data["authorization"]
            if (
                not isinstance(authorization, dict)
                or set(authorization) != {"grant_id", "epoch", "fingerprint"}
                or not isinstance(authorization["grant_id"], str)
                or not re.fullmatch(r"grant_[0-9a-f]{32}", authorization["grant_id"])
                or type(authorization["epoch"]) is not int
                or not 1 <= authorization["epoch"] <= 2147483647
                or not isinstance(authorization["fingerprint"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", authorization["fingerprint"])
            ):
                raise ValueError("authorization")
        return data, hashlib.sha256(raw).digest()
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise ReadFailure("OWNER_ACCOUNT_AUTHORIZATION_REQUIRED") from None


def source_grant(path, now, source, required=False):
    data, _ = account_grant(path, now, source)
    authorization = data.get("authorization")
    if authorization is None:
        if required:
            raise ReadFailure("SOURCE_GRANT_METADATA_REQUIRED")
        return None
    expiry = data["expires_at"]
    if expiry is not None:
        expiry = datetime.fromisoformat(expiry).astimezone(UTC).isoformat(timespec="microseconds")
    return {**authorization, "expires_at": expiry}
