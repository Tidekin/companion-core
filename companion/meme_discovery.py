"""One Core-native FILE acquisition unit over the original durable discovery attempt."""

import asyncio
import base64
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

from companion import job_queue, meme_assets, meme_service, private_life
from companion.contracts import CoreError
from companion.store import encode
from companion_meme_protocol import CaptionContext, source_evidence
from companion_meme_sources import (
    DiscoveryLimits,
    Metadata,
    RepositoryImage,
    ReviewApproval,
    ReviewEvidence,
    Usage,
)
from companion_meme_sources.community import CommunityPacks, verify_repository_image
from companion_meme_sources.fetch import PublicFetcher, resolve_public
from companion_meme_sources.gallery import AigengtuGallery, GalleryImage
from companion_meme_sources.review import TrustedReviewer


class MemeDiscovery:
    def __init__(self, service):
        self.service = service
        self.store, self.settings, self.clock = service.store, service.settings, service.clock
        self.transport, self.resolver = None, resolve_public

    def _witness(self, claim):
        return {key: claim[key] for key in ("lease_token", "attempt_key")}

    def _check(self, claim):
        s = self.store
        with s.transaction():
            meme_service._check_native(s, self.settings, self.clock, claim["job_id"],
                meme_service.DiscoveryCheck(**self._witness(claim)))
            if (private_life.owner_waiting(s, self.settings, self.clock())
                or private_life.busy(s, self.clock())
                or s.one("SELECT 1 FROM outbox WHERE state IN ('PENDING','LEASED','SENDING')")):
                raise CoreError("MEME_DISCOVERY_PREEMPTED", 409)

    def _held(self, event_id):
        """Approved file URLs already staged under this attempt's custody.

        Erased records and assets of a rotated capture custody are not skipped, so they are
        neither reused nor allowed to block acquisition under the current custody.
        """
        try:
            custody = meme_assets.custody_digest(
                meme_assets.original_custody(self.store, self.settings, event_id))
        except (CoreError, ValueError):
            return set()  # The original commit path still refuses an unattributed source.
        if self.settings.meme_gallery_source is not None:
            from companion.meme_policy import source_allowed

            # The operator selection is an import list. A retained failed/uncertain
            # caption is not permission to re-download that entry or charge it again;
            # leave its original responsibility intact and advance to other entries.
            retained = self.store.rows(
                "SELECT DISTINCT a.metadata_json FROM meme_assets a "
                "JOIN meme_asset_sources src USING(asset_id) "
                "JOIN meme_captions c ON c.job_id=src.caption_job_id "
                "WHERE a.state='STAGED' AND a.custody_sha256=?", (custody,))
            result = set()
            for row in retained:
                meta = json.loads(row["metadata_json"])
                if source_allowed(meta, self.settings):
                    result.add(meta["asset_url"])
            return result
        # Only material that is captioned or still awaiting its caption counts as held; a
        # failed or UNKNOWN caption keeps its original refusal path on re-acquisition.
        return {json.loads(row["metadata_json"])["asset_url"] for row in self.store.rows(
            "SELECT DISTINCT a.metadata_json FROM meme_assets a "
            "JOIN meme_asset_sources src USING(asset_id) "
            "JOIN jobs j ON j.job_id=src.caption_job_id "
            "JOIN meme_captions c ON c.job_id=src.caption_job_id "
            "WHERE a.state='STAGED' AND a.custody_sha256=? AND (j.state IN "
            "('QUEUED','RUNNING','RETRY_WAIT') OR (j.state='SUCCEEDED' "
            "AND c.result_json IS NOT NULL))", (custody,))}

    def _result(self, claim, outcome, usage):
        return meme_service.DiscoveryResult(**self._witness(claim), outcome=outcome,
            usage=meme_service.DiscoveryUsage(**asdict(usage)))

    def _commit(self, claim, usage, candidates):
        from companion.meme_core import AssetBody, CaptionBody, identity
        from companion.meme_retrieval import media

        s, expected = self.store, None
        with s.transaction():
            meme_service._check_native(s, self.settings, self.clock, claim["job_id"],
                meme_service.DiscoveryCheck(**self._witness(claim)))
            if (private_life.owner_waiting(s, self.settings, self.clock())
                or private_life.busy(s, self.clock())
                or s.one("SELECT 1 FROM outbox WHERE state IN ('PENDING','LEASED','SENDING')")):
                raise CoreError("MEME_DISCOVERY_PREEMPTED", 409)
            custody = meme_assets.original_custody(s, self.settings, claim["event_id"])
            new_ids = {}
            for image, meta in candidates:
                item = {"asset_sha256": image.sha256,
                        "source_sha256": meme_assets.source_digest(meta), "metadata": meta}
                asset_id = meme_assets.asset_id(custody, item)
                prior = s.one("SELECT * FROM meme_assets WHERE asset_id=?", (asset_id,))
                if prior and (prior["state"] != "STAGED"
                              or prior["record_sha256"] != meme_assets.record_digest(item)):
                    raise CoreError("MEME_ASSET_UNAVAILABLE", 409)
                if not prior:
                    new_ids[asset_id] = len(image.data)
            storage = sum(new_ids.values())
            limits = meme_service._metadata(s, claim["job_id"])["limits"]
            if storage > limits["storage_bytes"]:
                raise CoreError("MEME_DISCOVERY_STORAGE_EXHAUSTED", 429)
            expected = replace(usage, storage_bytes=storage)
            self.expected = expected  # RAM observation for authoritative commit readback only.
            settled = meme_service._settle(s, self.settings, self.clock, claim["job_id"],
                                           self._result(claim, "SETTLED", expected))
            if settled["state"] != "SETTLED":
                raise CoreError("MEME_DISCOVERY_ATTEMPT_FINISHED", 409)
            inserted = 0
            for image, meta in candidates:
                if media(self.settings, image.data) is None:
                    raise CoreError("MEME_ASSET_INVALID", 400)
                proof = source_evidence(SimpleNamespace(**meta))
                caption = self.service.enqueue_native(CaptionBody(
                    asset=AssetBody(data_base64=base64.b64encode(image.data).decode(),
                        asset_sha256=image.sha256,
                        source_evidence_sha256=meme_assets.source_digest(meta),
                        mime=image.mime_type, byte_length=len(image.data)),
                    context=CaptionContext(event_id=claim["event_id"],
                        reply_revision=claim["reply_revision"], discovery_job_id=claim["job_id"],
                        attempt_key=claim["attempt_key"]),
                    identity=identity(self.settings), source_evidence=proof,
                ), custody)
                _, added = meme_assets.stage_native(s, self.settings, self.clock,
                    caption_job_id=caption["job_id"], metadata_value=meta, data=image.data,
                    custody=custody)
                inserted += added
            if inserted != storage:
                raise CoreError("MEME_DISCOVERY_RESULT_CONFLICT", 409)
        return True  # No await between successful commit and the caller's committed marker.

    def _close(self, claim, usage, complete):
        """Read the original attempt after the writer ended; never replay acquisition."""
        s = self.store
        if s.db.in_transaction:
            raise CoreError("MEME_DISCOVERY_COMMIT_UNCERTAIN", 503)
        meme_service._owned(s, claim["job_id"],
                            meme_service.DiscoveryCheck(**self._witness(claim)))
        row = s.one("SELECT * FROM meme_discovery_reservations WHERE job_id=? AND attempt_key=?",
                    (claim["job_id"], claim["attempt_key"]))
        if not row:
            raise CoreError("MEME_DISCOVERY_COMMIT_UNCERTAIN", 503)
        if row["state"] == "SETTLED":
            expected = self.expected or replace(usage, storage_bytes=0)
            if any(getattr(expected, key) != row[key] for key in meme_service.FIELDS):
                raise CoreError("MEME_DISCOVERY_RESULT_CONFLICT", 409)
            return
        # A complete original transaction cannot leave caption writes with RESERVED.
        # Such a contradictory readback grants neither a retry nor a guessed settlement.
        if row["state"] == "RESERVED" and s.one(
            "SELECT 1 FROM meme_captions WHERE json_extract(context_json,'$.discovery_job_id')=?",
            (claim["job_id"],),
        ):
            raise CoreError("MEME_DISCOVERY_COMMIT_UNCERTAIN", 503)
        outcome = "SETTLED" if complete and row["state"] == "RESERVED" else "UNKNOWN"
        actual = replace(usage, storage_bytes=0)
        try:
            meme_service.result(s, self.settings, self.clock, claim["job_id"],
                                self._result(claim, outcome, actual))
        except BaseException:
            if s.db.in_transaction:
                raise
            saved = s.one("SELECT * FROM meme_discovery_reservations WHERE job_id=? "
                          "AND attempt_key=?", (claim["job_id"], claim["attempt_key"]))
            if (not saved or saved["state"] != outcome
                or any(saved[key] != getattr(actual, key) for key in meme_service.FIELDS)):
                raise

    async def run_once(self, worker_id):
        from companion import meme_policy
        from companion.meme_retrieval import media

        self.expected = None
        claim = meme_service.lease_native(self.store, self.settings, self.clock,
            meme_service.DiscoveryLeaseRequest(worker_id=worker_id))["item"]
        if claim is None:
            return False
        try:
            reserved = meme_service.reserve_native(self.store, self.settings, self.clock,
                claim["job_id"], meme_service.DiscoveryReserve(**self._witness(claim),
                    limits=meme_service.DiscoveryLimits()))
        except CoreError as error:
            with self.store.transaction():
                job_queue.release(
                    self.store,
                    claim["job_id"],
                    "FAILED",
                    self.clock(),
                    reason=error.code,
                    token=claim["lease_token"],
                    condition="NOT EXISTS (SELECT 1 FROM "
                    "meme_discovery_reservations WHERE job_id=jobs.job_id)",
                )
            return True
        usage, complete, committed = Usage(), False, False

        async def fence():
            self._check(claim)

        fetcher = PublicFetcher(DiscoveryLimits(**reserved["limits"]), usage, fence,
                                transport=self.transport, resolver=self.resolver)
        rules = [r.model_dump() for r in meme_policy.source_rules(self.settings)]
        reviewer = TrustedReviewer(rules, require_semantics=False)
        known = self._held(claim["event_id"])
        provider = (AigengtuGallery(self.settings.meme_gallery_source.selection(), known=known)
                    if claim["provider"] == "gallery"
                    else CommunityPacks(reviewer.license_files, known=known))
        candidates = []
        try:
            await fence()
            async for candidate, supplied in provider.discover(claim["intent"], fetcher):
                await fence()
                if not isinstance(supplied, (RepositoryImage, GalleryImage)):
                    raise ValueError("MEME_NATIVE_FILE_REQUIRED")
                resources = ((supplied.resource, supplied.terms)
                             if isinstance(supplied, GalleryImage) else
                             (supplied.resource, supplied.manifest, supplied.license,
                              supplied.listing))
                for resource in resources:
                    fetcher.verify(resource)
                if isinstance(supplied, RepositoryImage):
                    verify_repository_image(candidate, supplied)
                image = media(self.settings, supplied.resource.data)
                if image is None or image.sha256 in self.settings.meme_excluded_sha256:
                    continue
                if isinstance(supplied, GalleryImage):
                    if not isinstance(provider, AigengtuGallery):
                        raise ValueError("MEME_NATIVE_FILE_REQUIRED")
                    reviewed, evidence = provider.review(candidate, supplied, image, fetcher)
                else:
                    evidence = ReviewEvidence(candidate.source_page, candidate.asset_url,
                        candidate.revision, supplied.resource.evidence, image.sha256,
                        source_files={role: getattr(supplied, role).evidence
                                      for role in ("manifest", "license", "listing")})
                    reviewed = await reviewer(replace(candidate,
                        asset_url=supplied.resource.evidence.final_url), image, evidence)
                await fence()
                if reviewed is None:
                    continue
                if (not isinstance(reviewed, ReviewApproval)
                    or not isinstance(reviewed.metadata, Metadata)
                    or reviewed.evidence_sha256 != evidence.sha256):
                    raise ValueError("MEME_REVIEW_INVALID")
                meta = replace(reviewed.metadata, acquisition_json=evidence.json)
                candidates.append((image, meme_assets.metadata(json.loads(encode(asdict(meta))))))
            complete = True
            await fence()
            self._commit(claim, usage, candidates)
            committed = True
            return True
        except BaseException as error:
            if not committed:
                # These are local document/license validation failures after bounded
                # fetch completion. Transport/partial-body/cancellation remains UNKNOWN.
                known_rejection = (isinstance(error, json.JSONDecodeError)
                    or isinstance(error, ValueError)
                    and str(error).startswith(("MEME_PACK_", "MEME_GALLERY_")))
                self._close(claim, usage, complete or known_rejection)
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return True
