"""The companion's display profile: stable identity, self-updated name, owner settings.

Identity (`companion_id`) never changes. The display name may be updated by the
companion within owner switches, rate limits and revision checks; every committed
change is one append-only audit row. The owner's alias is separate and never
rewrites the companion's own name. No permission, memory or persona is derived
from the display name.
"""

import json
import unicodedata
import uuid
from datetime import datetime, timedelta

from companion.contracts import CoreError
from companion.store import uid

DEFAULT_NAME = "伙伴"
MAX_NAME = 32
COMPANION_ID = "companion_id"
# Names that would let the companion impersonate the owner or the system.
RESERVED = {"主人", "系统", "管理员", "官方", "客服", "owner", "system", "admin", "root"}
PROFILE_FIELDS = ("companion_id", "revision", "display_name", "avatar_asset_id", "updated_at")
CHANGE_FIELDS = (
    "change_id", "old_revision", "new_revision", "fields", "old_name", "new_name",
    "old_avatar_asset_id", "new_avatar_asset_id", "actor_type", "note",
    "restored_from_change_id", "created_at",
)


def visible_length(text):
    """Characters a reader sees; combining marks attach to their base character."""
    return sum(1 for char in text if not unicodedata.category(char).startswith("M"))


def validate_name(value):
    """Normalize one display name or raise ValueError with a correctable reason."""
    if not isinstance(value, str):
        raise ValueError("NAME_REQUIRED")
    name = unicodedata.normalize("NFC", value).strip()
    if not name:
        raise ValueError("NAME_EMPTY")
    for char in name:
        category = unicodedata.category(char)
        # Cc controls, Cf format (zero-width, bidi overrides), Co private use, Cs/Cn.
        if category[0] == "C" or category in {"Zl", "Zp"}:
            raise ValueError("NAME_INVISIBLE_OR_CONTROL_CHARACTER")
    if "  " in name:
        raise ValueError("NAME_REPEATED_SPACES")
    if visible_length(name) > MAX_NAME:
        raise ValueError("NAME_TOO_LONG")
    if name.casefold() in {item.casefold() for item in RESERVED}:
        raise ValueError("NAME_IMPERSONATES_OWNER_OR_SYSTEM")
    return name


def _character_name(store):
    from companion.history_import import ACTIVE

    pointer = store.one("SELECT value FROM runtime_meta WHERE key=%s", (ACTIVE,))
    if not pointer:
        return None
    row = store.one("SELECT body_json FROM events WHERE event_id=%s AND redacted=0",
                    (pointer["value"],))
    try:
        return validate_name(json.loads(row["body_json"])["character"]["name"]) if row else None
    except (ValueError, KeyError, TypeError):
        return None


def initial_name(store, settings):
    """Explicit character name, then the configured initial name, then a neutral default."""
    return (_character_name(store) or settings.profile.initial_display_name or DEFAULT_NAME)


def current(store, settings):
    """Current profile; before the first write this is the uncommitted initial view."""
    row = store.one("SELECT " + ",".join(PROFILE_FIELDS) + " FROM companion_profile "
                    "WHERE singleton='self'")
    if row:
        return dict(row)
    identity = store.one("SELECT value FROM runtime_meta WHERE key=%s", (COMPANION_ID,))
    return {"companion_id": identity["value"] if identity else None, "revision": 0,
            "display_name": initial_name(store, settings), "avatar_asset_id": None,
            "updated_at": None}


def owner_settings(store):
    row = store.one("SELECT auto_name,auto_avatar,viewer_alias,settings_revision,updated_at "
                    "FROM companion_profile_settings WHERE singleton='self'")
    return dict(row) if row else {"auto_name": True, "auto_avatar": True, "viewer_alias": None,
                                  "settings_revision": 0, "updated_at": None}


def view(store, settings):
    """Everything a native client shows; no permission or audit detail is implied."""
    profile, owner = current(store, settings), owner_settings(store)
    return {**profile, **{key: owner[key] for key in (
        "auto_name", "auto_avatar", "viewer_alias", "settings_revision")},
        "self_update_enabled": bool(settings.profile.enabled)}


def _require_write(store):
    if not store.in_write_transaction:
        raise RuntimeError("COMPANION_PROFILE_WRITE_TRANSACTION_REQUIRED")


def _ensure(store, settings, now):
    """Create identity and the first revision once, then lock the current row."""
    _require_write(store)
    store.execute("INSERT INTO runtime_meta (key,value) VALUES (%s,%s) ON CONFLICT (key) "
                  "DO NOTHING", (COMPANION_ID, "cmp_" + uuid.uuid4().hex))
    identity = store.one("SELECT value FROM runtime_meta WHERE key=%s", (COMPANION_ID,))["value"]
    name = initial_name(store, settings)
    created = store.one(
        "INSERT INTO companion_profile (singleton,companion_id,revision,display_name,updated_at) "
        "VALUES ('self',%s,1,%s,%s) ON CONFLICT (singleton) DO NOTHING RETURNING revision",
        (identity, name, now))
    if created:
        store.execute(
            "INSERT INTO companion_profile_changes (change_id,old_revision,new_revision,fields,"
            "old_name,new_name,actor_type,created_at) VALUES (%s,NULL,1,'initial',NULL,%s,"
            "'system',%s)", (uid("pchg"), name, now))
    store.execute("INSERT INTO companion_profile_settings (singleton,updated_at) "
                  "VALUES ('self',%s) ON CONFLICT (singleton) DO NOTHING", (now,))
    return dict(store.one("SELECT " + ",".join(PROFILE_FIELDS) + " FROM companion_profile "
                          "WHERE singleton='self' FOR UPDATE"))


def _change(store, change_id):
    row = store.one("SELECT " + ",".join(CHANGE_FIELDS) + " FROM companion_profile_changes "
                    "WHERE change_id=%s", (change_id,))
    return dict(row) if row else None


_KEEP = object()


def _commit(store, profile, now, *, actor, name=None, avatar=_KEEP, note=None,
            idempotency_key=None, job_id=None, tool_call_id=None, operation_id=None,
            restored_from=None):
    """One revision for one atomic change of the name, the avatar or both."""
    change_id, revision = uid("pchg"), profile["revision"] + 1
    name = profile["display_name"] if name is None else name
    avatar = profile["avatar_asset_id"] if avatar is _KEEP else avatar
    fields = {(True, False): "name", (False, True): "avatar", (True, True): "both"}[
        (name != profile["display_name"], avatar != profile["avatar_asset_id"])]
    store.execute(
        "INSERT INTO companion_profile_changes (change_id,old_revision,new_revision,fields,"
        "old_name,new_name,old_avatar_asset_id,new_avatar_asset_id,actor_type,origin_job_id,"
        "origin_tool_call_id,origin_operation_id,idempotency_key,note,restored_from_change_id,"
        "created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (change_id, profile["revision"], revision, fields, profile["display_name"], name,
         profile["avatar_asset_id"], avatar, actor, job_id, tool_call_id,
         operation_id, idempotency_key, note, restored_from, now))
    store.execute("UPDATE companion_profile SET revision=%s, display_name=%s, "
                  "avatar_asset_id=%s, updated_at=%s WHERE singleton='self' AND revision=%s",
                  (revision, name, avatar, now, profile["revision"]))
    return change_id, {**profile, "revision": revision, "display_name": name,
                       "avatar_asset_id": avatar, "updated_at": now}


def _public(profile):
    return {key: profile[key] for key in ("revision", "display_name", "avatar_asset_id")}


def _confirmed(change_id, profile):
    return {"status": "confirmed", "change_id": change_id, "new_revision": profile["revision"],
            "profile": _public(profile)}


def _retry_after(store, settings, now, field="name"):
    """Seconds until the companion may change this field again, or 0."""
    config, moment = settings.profile, datetime.fromisoformat(now)
    limit = config.name_daily_limit if field == "name" else config.avatar_daily_limit
    cooldown = config.name_cooldown_seconds if field == "name" else 0
    if limit == 0:
        return 86400
    rows = store.rows(
        "SELECT created_at FROM companion_profile_changes WHERE actor_type='companion' "
        "AND fields IN (%s,'both') AND created_at > %s ORDER BY created_at DESC",
        (field, (moment - timedelta(days=1)).isoformat(timespec="microseconds")))
    waits = []
    if len(rows) >= limit:
        waits.append(datetime.fromisoformat(rows[limit - 1]["created_at"])
                     + timedelta(days=1) - moment)
    latest = store.one(
        "SELECT MAX(created_at) AS at FROM companion_profile_changes WHERE "
        "actor_type='companion' AND fields IN (%s,'both')", (field,))["at"]
    if latest and cooldown:
        waits.append(datetime.fromisoformat(latest) + timedelta(seconds=cooldown) - moment)
    return max([0, *(int(wait.total_seconds()) + 1 for wait in waits if wait.total_seconds() > 0)])


def update_by_companion(store, settings, now, *, expected_revision, note, job_id,
                        tool_call_id, display_name=None, avatar_asset_id=None):
    """The companion's own name/avatar change; the caller already checked the leased job.

    Both requested fields commit together in one revision, or neither does.
    """
    from companion import companion_avatar

    _require_write(store)
    key = f"{job_id}:{tool_call_id}"
    previous = store.one("SELECT change_id FROM companion_profile_changes "
                         "WHERE idempotency_key=%s", (key,))
    if previous:
        # Same tool call, same result: never a second write or a second event.
        change = _change(store, previous["change_id"])
        return {"status": "confirmed", "change_id": change["change_id"],
                "new_revision": change["new_revision"],
                "profile": {"revision": change["new_revision"],
                            "display_name": change["new_name"],
                            "avatar_asset_id": change["new_avatar_asset_id"]}}
    profile = _ensure(store, settings, now)
    public = _public(profile)
    if display_name is None and avatar_asset_id is None:
        return {"status": "validation_failed", "field": "display_name",
                "reason": "NOTHING_REQUESTED", "profile": public}
    owner = owner_settings(store)
    if display_name is not None and not owner["auto_name"]:
        return {"status": "disabled_by_user", "field": "display_name", "profile": public}
    if avatar_asset_id is not None and not owner["auto_avatar"]:
        return {"status": "disabled_by_user", "field": "avatar", "profile": public}
    if expected_revision != profile["revision"]:
        return {"status": "revision_conflict", "profile": public}
    try:
        name = None if display_name is None else validate_name(display_name)
        if note is not None and len(note) > 200:
            raise ValueError("NOTE_TOO_LONG")
    except ValueError as error:
        return {"status": "validation_failed", "field": "display_name", "reason": str(error),
                "profile": public}
    if avatar_asset_id is not None and (
            not isinstance(avatar_asset_id, str)
            or not companion_avatar.ASSET_ID.fullmatch(avatar_asset_id)
            or companion_avatar.ready(store, avatar_asset_id) is None):
        return {"status": "asset_not_ready", "field": "avatar", "profile": public}
    name_changes = name is not None and name != profile["display_name"]
    avatar_changes = (avatar_asset_id is not None
                      and avatar_asset_id != profile["avatar_asset_id"])
    if not name_changes and not avatar_changes:
        return {"status": "no_change", "profile": public}
    for field, changes in (("name", name_changes), ("avatar", avatar_changes)):
        if changes and (wait := _retry_after(store, settings, now, field)):
            return {"status": "rate_limited", "field": field, "retry_after_seconds": wait,
                    "profile": public}
    change_id, updated = _commit(
        store, profile, now, actor="companion", name=name if name_changes else None,
        avatar=avatar_asset_id if avatar_changes else _KEEP, note=note,
        idempotency_key=key, job_id=job_id, tool_call_id=tool_call_id)
    return _confirmed(change_id, updated)


def set_avatar(store, settings, now, *, asset_id, expected_revision, operation_id):
    """The owner sets an uploaded avatar directly; recorded as an owner revision."""
    from companion import companion_avatar

    _require_write(store)
    # Compare with what the owner saw: revision 0 is the not-yet-created initial view.
    seen = current(store, settings)["revision"]
    profile = _ensure(store, settings, now)
    previous = store.one("SELECT change_id FROM companion_profile_changes "
                         "WHERE idempotency_key=%s", ("owner:" + operation_id,))
    if previous:
        return {"change_id": previous["change_id"], **view(store, settings)}
    if expected_revision != seen:
        raise CoreError("PROFILE_REVISION_CONFLICT", 409)
    if (not isinstance(asset_id, str) or not companion_avatar.ASSET_ID.fullmatch(asset_id)
            or companion_avatar.ready(store, asset_id) is None):
        raise CoreError("PROFILE_ASSET_UNAVAILABLE", 410)
    if asset_id == profile["avatar_asset_id"]:
        raise CoreError("PROFILE_NO_CHANGE", 409)
    change_id, _ = _commit(store, profile, now, actor="owner", avatar=asset_id,
                           idempotency_key="owner:" + operation_id, operation_id=operation_id)
    return {"change_id": change_id, **view(store, settings)}


def update_settings(store, settings, now, *, expected_settings_revision, auto_name=None,
                    auto_avatar=None):
    """Owner switches; they affect only future companion commits."""
    _require_write(store)
    # Compare with what the owner saw: revision 0 is the not-yet-created default row.
    seen = owner_settings(store)["settings_revision"]
    _ensure(store, settings, now)
    if expected_settings_revision != seen:
        raise CoreError("PROFILE_SETTINGS_REVISION_CONFLICT", 409)
    owner = owner_settings(store)
    store.execute(
        "UPDATE companion_profile_settings SET auto_name=%s, auto_avatar=%s, "
        "settings_revision=settings_revision+1, updated_at=%s WHERE singleton='self'",
        (owner["auto_name"] if auto_name is None else auto_name,
         owner["auto_avatar"] if auto_avatar is None else auto_avatar, now))
    return view(store, settings)


def set_alias(store, settings, now, *, expected_settings_revision, alias):
    """The owner's own name for the companion; it never changes the display name."""
    _require_write(store)
    # Compare with what the owner saw: revision 0 is the not-yet-created default row.
    seen = owner_settings(store)["settings_revision"]
    _ensure(store, settings, now)
    if expected_settings_revision != seen:
        raise CoreError("PROFILE_SETTINGS_REVISION_CONFLICT", 409)
    if alias is not None:
        try:
            alias = validate_name(alias)
        except ValueError as error:
            raise CoreError("PROFILE_ALIAS_INVALID", 400) from error
    store.execute("UPDATE companion_profile_settings SET viewer_alias=%s, "
                  "settings_revision=settings_revision+1, updated_at=%s WHERE singleton='self'",
                  (alias, now))
    return view(store, settings)


def restore(store, settings, now, *, change_id, expected_revision, pause_name, operation_id):
    """Restore an earlier name as a new owner revision; history is never rewritten."""
    _require_write(store)
    profile = _ensure(store, settings, now)
    previous = store.one("SELECT change_id FROM companion_profile_changes "
                         "WHERE idempotency_key=%s", ("owner:" + operation_id,))
    if previous:
        return {"change_id": previous["change_id"], **view(store, settings)}
    if expected_revision != profile["revision"]:
        raise CoreError("PROFILE_REVISION_CONFLICT", 409)
    target = _change(store, change_id)
    if target is None:
        raise CoreError("PROFILE_CHANGE_NOT_FOUND", 404)
    from companion import companion_avatar

    avatar = target["new_avatar_asset_id"]
    if avatar is not None and companion_avatar.ready(store, avatar) is None:
        # The image expired or was erased; its record stays, restoring it cannot.
        raise CoreError("PROFILE_ASSET_UNAVAILABLE", 410)
    if (target["new_name"], avatar) == (profile["display_name"], profile["avatar_asset_id"]):
        raise CoreError("PROFILE_NO_CHANGE", 409)
    restored_id, _ = _commit(store, profile, now, name=target["new_name"], avatar=avatar,
                             actor="owner", idempotency_key="owner:" + operation_id,
                             operation_id=operation_id, restored_from=change_id)
    if pause_name:
        # Only an explicit owner choice pauses future self-updates.
        store.execute("UPDATE companion_profile_settings SET auto_name=false, "
                      "settings_revision=settings_revision+1, updated_at=%s "
                      "WHERE singleton='self'", (now,))
    return {"change_id": restored_id, **view(store, settings)}


def changes(store, *, before=None, limit=50):
    """Newest first; `before` is a revision cursor."""
    limit = max(1, min(int(limit), 100))
    rows = store.rows(
        "SELECT " + ",".join(CHANGE_FIELDS) + " FROM companion_profile_changes "
        + ("WHERE new_revision < %s " if before is not None else "")
        + "ORDER BY new_revision DESC LIMIT %s",
        ((before, limit) if before is not None else (limit,)))
    return [dict(row) for row in rows]


def prompt_fact(store, settings):
    """One per-turn fact; the character's name stays in its own reference."""
    name = current(store, settings)["display_name"]
    character = _character_name(store)
    text = f"你现在对外显示的名字是「{name}」。"
    if character and character != name:
        text += f"你的角色名是「{character}」；显示名是你自己选的，可以与角色名不同。"
    if settings.profile.enabled:
        text += ("想换名字或换成主人上传的头像时可以用 update_display_profile 自己改，"
                 "改成功之前不要说已经改了。")
    return text
