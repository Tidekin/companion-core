"""Owner-controlled, credential-free source authorization and evidence attribution."""

from datetime import UTC

from pydantic import AwareDatetime, Field

from companion.contracts import CoreError, StrictModel
from companion.sources import supports


class SourceGrantSnapshot(StrictModel):
    grant_id: str = Field(pattern=r"^grant_[0-9a-f]{32}$")
    epoch: int = Field(ge=1, le=2147483647, strict=True)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: AwareDatetime | None

    def snapshot(self):
        return {
            "grant_id": self.grant_id,
            "epoch": self.epoch,
            "fingerprint": self.fingerprint,
            "expires_at": (self.expires_at.astimezone(UTC).isoformat(timespec="microseconds")
                           if self.expires_at is not None else None),
        }


class SourceGrantRegistration(SourceGrantSnapshot):
    adapter_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class ReaderLeaseRequest(StrictModel):
    source_grant: SourceGrantSnapshot | None = None


class ReaderReadValidation(StrictModel):
    lease_token: str = Field(min_length=8, max_length=128)
    source_grant: SourceGrantSnapshot | None = None


def _check_expiry(source, expires_at, now):
    if expires_at is None:
        if source != "bilibili":
            raise CoreError("SOURCE_GRANT_EXPIRY_REQUIRED", 400)
    elif expires_at <= now:
        raise CoreError("SOURCE_GRANT_EXPIRED", 403)


def grant_tombstone(grant_id):
    return "erased_source_grant:" + grant_id


def cancel_grant_reads(store, grant_id, now):
    store.execute(
        "UPDATE exploration_requests SET state='CANCELLED',url='',goal_step_json='{}',"
        "lease_token=NULL,last_error_code='SOURCE_GRANT_REVOKED',updated_at=? "
        "WHERE source_grant_id=? AND state IN ('QUEUED','RUNNING')",
        (now, grant_id),
    )


class SourceGrantService:
    def __init__(self, store, settings, clock):
        self.store, self.settings, self.clock = store, settings, clock

    def legacy(self, offset=0):
        if type(offset) is not int or not 0 <= offset <= 2147483647:
            raise CoreError("INVALID_REVIEW_OFFSET", 400)
        return self.store.rows(
            "SELECT r.event_id,r.reason,o.adapter_id,e.source FROM source_grant_legacy_review r "
            "JOIN observations o USING(event_id) JOIN events e USING(event_id) "
            "WHERE e.redacted=0 ORDER BY r.event_id LIMIT 100 OFFSET ?",
            (offset,),
        )

    def list(self):
        return self.store.rows(
            "SELECT g.grant_id,g.adapter_id,g.source,g.revoked_at,g.forgotten_at,"
            "r.epoch,r.fingerprint,r.expires_at FROM source_grants g "
            "JOIN source_grant_revisions r USING(grant_id) WHERE r.epoch="
            "(SELECT MAX(v.epoch) FROM source_grant_revisions v WHERE v.grant_id=g.grant_id) "
            "ORDER BY g.rowid DESC LIMIT 100"
        )

    def register(self, request):
        data, s = request.snapshot(), self.store
        with s.transaction():
            now = self.clock()
            policy = self.settings.observation_adapters.get(request.adapter_id)
            if policy is None or not supports(policy.source, "source_grant"):
                raise CoreError("SOURCE_GRANT_ADAPTER_INVALID", 400)
            _check_expiry(policy.source, data["expires_at"], now)
            if s.one(
                "SELECT 1 FROM runtime_meta WHERE key=?", (grant_tombstone(request.grant_id),)
            ):
                raise CoreError("SOURCE_GRANT_FORGOTTEN", 410)
            prior = s.one("SELECT * FROM source_grants WHERE grant_id=?", (request.grant_id,))
            if prior and (
                prior["adapter_id"] != request.adapter_id or prior["source"] != policy.source
            ):
                raise CoreError("SOURCE_GRANT_IDENTITY_CHANGED", 409)
            if prior and (prior["revoked_at"] or prior["forgotten_at"]):
                raise CoreError("SOURCE_GRANT_REVOKED", 403)
            revisions = s.rows(
                "SELECT * FROM source_grant_revisions WHERE grant_id=? ORDER BY epoch DESC LIMIT 1",
                (request.grant_id,),
            )
            if revisions and request.epoch <= revisions[0]["epoch"]:
                if all(revisions[0][key] == data[key] for key in data):
                    return {"grant_id": request.grant_id, "epoch": request.epoch, "duplicate": True}
                raise CoreError("SOURCE_GRANT_REVISION_CONFLICT", 409)
            if not prior:
                s.insert(
                    "source_grants",
                    grant_id=request.grant_id,
                    adapter_id=request.adapter_id,
                    source=policy.source,
                    created_at=now,
                )
            s.insert("source_grant_revisions", **data, created_at=now)
            s.audit(
                "source_grant.registered",
                request.grant_id,
                request.grant_id,
                now,
                epoch=request.epoch,
            )
        return {"grant_id": request.grant_id, "epoch": request.epoch, "duplicate": False}

    def require_available(self, adapter_id):
        """Before a lease binds one, a required grant only has to exist and be valid now.

        The Reader brings its grant to the lease, which records it on the request; from
        then on `require` checks that exact binding. Returns the grant that passed.
        """
        rows = self.store.rows(
            "SELECT r.grant_id,r.epoch,r.fingerprint,r.expires_at FROM source_grants g "
            "JOIN source_grant_revisions r USING(grant_id) WHERE g.adapter_id=? AND "
            "r.epoch=(SELECT MAX(v.epoch) FROM source_grant_revisions v "
            "WHERE v.grant_id=g.grant_id) ORDER BY r.grant_id", (adapter_id,))
        first = None
        for row in rows:
            try:
                return self.require(adapter_id, SourceGrantSnapshot.model_validate(row))
            except CoreError as error:
                first = first or error
        raise first or CoreError("SOURCE_GRANT_REQUIRED", 403)

    def require(self, adapter_id, snapshot):
        if snapshot is None:
            raise CoreError("SOURCE_GRANT_REQUIRED", 403)
        data, s = snapshot.snapshot(), self.store
        row = s.one(
            "SELECT g.*,r.epoch,r.fingerprint,r.expires_at FROM source_grants g "
            "JOIN source_grant_revisions r USING(grant_id) WHERE g.grant_id=? "
            "ORDER BY r.epoch DESC LIMIT 1",
            (snapshot.grant_id,),
        )
        if s.one("SELECT 1 FROM runtime_meta WHERE key=?", (grant_tombstone(snapshot.grant_id),)):
            raise CoreError("SOURCE_GRANT_FORGOTTEN", 410)
        if not row or row["adapter_id"] != adapter_id or row["revoked_at"] or row["forgotten_at"]:
            raise CoreError("SOURCE_GRANT_REVOKED", 403)
        policy = self.settings.observation_adapters.get(adapter_id)
        if not policy or policy.source != row["source"]:
            raise CoreError("SOURCE_GRANT_IDENTITY_CHANGED", 409)
        _check_expiry(row["source"], row["expires_at"], self.clock())
        if any(row[key] != data[key] for key in data):
            raise CoreError("SOURCE_GRANT_REVISION_CHANGED", 409)
        return data

    def captured(self, row):
        if not row["source_grant_id"]:
            return None
        revision = self.store.one(
            "SELECT grant_id,epoch,fingerprint,expires_at FROM source_grant_revisions "
            "WHERE grant_id=? AND epoch=?",
            (row["source_grant_id"], row["source_grant_epoch"]),
        )
        return SourceGrantSnapshot.model_validate(revision)

    def revoke(self, grant_id, *, owner_check=None):
        with self.store.transaction():
            if owner_check is not None:
                owner_check()
            row = self.store.one("SELECT 1 FROM source_grants WHERE grant_id=?", (grant_id,))
            if not row:
                raise CoreError("SOURCE_GRANT_NOT_FOUND", 404)
            now = self.clock()
            self.store.execute(
                "UPDATE source_grants SET revoked_at=COALESCE(revoked_at,?) WHERE grant_id=?",
                (now, grant_id),
            )
            cancel_grant_reads(self.store, grant_id, now)
            self.store.audit("source_grant.read_revoked", grant_id, grant_id, now)
        return {"grant_id": grant_id, "future_reads_revoked": True, "history_erased": False}

    def forget(self, grant_id, ledger, *, owner_check=None):
        with self.store.transaction():
            if owner_check is not None:
                owner_check()
            if not self.store.one("SELECT 1 FROM source_grants WHERE grant_id=?", (grant_id,)):
                raise CoreError("SOURCE_GRANT_NOT_FOUND", 404)
            if not self.store.one(
                "SELECT 1 FROM runtime_meta WHERE key=?", (grant_tombstone(grant_id),)
            ):
                ledger.forget_source_grant(self.store, grant_id, self.clock())
        return {"grant_id": grant_id, "future_reads_revoked": True, "history_erased": True}
