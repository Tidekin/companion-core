"""Owner control authentication is an API boundary; scoped grants are short lived records."""

import json

from pydantic import Field

from companion.contracts import CoreError, StrictModel
from companion.privacy import advance_epoch
from companion.store import after, encode, uid

CAPABILITY_FLAGS = {
    "audio.transcribe": "voice_enabled",
    "camera.observe": "camera_enabled",
    "android.observe": "android_enabled",
    "model.evaluate": "evaluation_enabled",
}


class ConsentRequest(StrictModel):
    capabilities: list[str] = Field(min_length=1, max_length=3)
    device_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    duration_seconds: int = Field(default=300, ge=1, le=3600)


class ConsentService:
    def __init__(self, store, settings, clock):
        self.store, self.settings, self.clock = store, settings, clock

    def grant(self, request):
        for capability in request.capabilities:
            flag = CAPABILITY_FLAGS.get(capability)
            if not flag or not getattr(self.settings, flag):
                raise CoreError("CAPABILITY_DISABLED", 403)
        consent_id, now = uid("consent"), self.clock()
        with self.store.transaction():
            self.store.insert(
                "consents",
                consent_id=consent_id,
                capabilities_json=encode(sorted(set(request.capabilities))),
                device_id=request.device_id,
                session_id=request.session_id,
                created_at=now,
                expires_at=after(now, request.duration_seconds),
            )
            self.store.audit("consent.granted", consent_id, consent_id, now)
        return self.store.one('SELECT consents.consent_id AS consent_id, '
            'consents.capabilities_json AS capabilities_json, consents.device_id AS device_id, '
            'consents.session_id AS session_id, consents.created_at AS created_at, '
            'consents.expires_at AS expires_at, consents.revoked_at AS revoked_at FROM consents AS '
            'consents WHERE consents.consent_id = %s', (consent_id,))

    def require(self, consent_id, capability, device_id, session_id, observed_at=None):
        flag = CAPABILITY_FLAGS.get(capability)
        if not flag or not getattr(self.settings, flag):
            raise CoreError("CAPABILITY_DISABLED", 403)
        row = self.store.one('SELECT consents.consent_id AS consent_id, consents.capabilities_json '
            'AS capabilities_json, consents.device_id AS device_id, consents.session_id AS '
            'session_id, consents.created_at AS created_at, consents.expires_at AS expires_at, '
            'consents.revoked_at AS revoked_at FROM consents AS consents WHERE consents.consent_id '
            '= %s', (consent_id,))
        if (
            not row
            or row["revoked_at"]
            or row["expires_at"] <= self.clock()
            or capability not in json.loads(row["capabilities_json"])
            or row["device_id"] != device_id
            or row["session_id"] != session_id
            or (observed_at and not row["created_at"] <= observed_at < row["expires_at"])
        ):
            raise CoreError("CONSENT_REQUIRED", 403)
        return row

    def revoke(self, consent_id):
        with self.store.transaction(control_write=True):
            changed = self.store.execute(
                'UPDATE consents SET revoked_at = %s WHERE consent_id = %s AND revoked_at IS NULL',
                (self.clock(), consent_id),
            )
            if changed:
                from companion.evaluation import revoke_jobs

                revoke_jobs(self.store, consent_id, self.clock())
                advance_epoch(self.store)
                self.store.audit("consent.revoked", consent_id, consent_id, self.clock())
        return {"consent_id": consent_id, "revoked": True}
