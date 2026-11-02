"""Bounded module cleanup contract; the Ledger owns durability and coordination.

Each owner keeps its own SQL. Handlers receive the already resolved event closure,
must be idempotent, run in the caller's transaction, and must not commit, append a
Ledger record, send I/O or refund accounting. New persistent projections register
here with their source-evidence eraser; adding a name also requests one targeted
repair of older COMPLETE snapshots via the existing runtime_meta projection.

Diary projection resolution runs before memory/evidence removal and may return
readback event IDs. The handlers below retain their original order after that
phase. Unattributed summaries/traits/Outbox still use Ledger's conservative raw-
fact fallback on a new erasure, never on the one-time COMPLETE repair.
"""

from collections.abc import Callable
from dataclasses import dataclass

from companion.store import Store

CONTRACT_KEY = "derived_erasure_contract"
# Bump when an existing handler's cleanup meaning changes without a new name.
# Names detect registration additions; they cannot detect arbitrary code edits.
CONTRACT_REVISION = 3


@dataclass(frozen=True)
class ErasureScope:
    event_ids: frozenset[str]
    clear_reply_derivatives: bool


@dataclass(frozen=True)
class DerivedCleanup:
    name: str
    erase: Callable[[Store, ErasureScope], None]


def event_cleanup(name, erase):
    """Adapt existing event-evidence erasers without changing their module APIs."""
    return DerivedCleanup(name, lambda store, scope: erase(store, scope.event_ids))


def cleanup_contracts():
    # Lazy imports preserve the existing modules' privacy helper dependencies.
    from companion.affect import erase_affect
    from companion.agent_images import erase_images
    from companion.agent_persistence import erase_conversations
    from companion.bili_video import erase_bili_video
    from companion.consolidation import erase_consolidation
    from companion.content import erase_content
    from companion.evaluation import erase as erase_evaluation
    from companion.executive import erase_executive
    from companion.exploration import erase_exploration
    from companion.goals import erase_goals
    from companion.image_generation import erase as erase_generated_images
    from companion.jargon import erase as erase_jargon
    from companion.life_topics import erase_topics
    from companion.media import erase_media_links
    from companion.meme_assets import erase as erase_meme_assets
    from companion.meme_core import erase_captions
    from companion.memory_enrichment import erase as erase_memory_enrichment
    from companion.memory_references import erase as erase_memory_references
    from companion.memory_work import erase_memory_work
    from companion.mood_appraisals import erase as erase_mood_appraisals
    from companion.observation import erase_observations
    from companion.owner_authority import erase_sources as erase_owner_authority
    from companion.owner_input import erase as erase_owner_inputs
    from companion.poke import erase as erase_poke
    from companion.reading import erase_reading
    from companion.reflection import erase_reflections
    from companion.rem import erase as erase_rem
    from companion.social_autonomy import erase_social_autonomy
    from companion.style_distill import erase as erase_style_distill
    from companion.voice_jobs import erase_voice_jobs
    from companion.watch_ledger import erase as erase_watch_ledger

    return (
        event_cleanup("agent-conversations", erase_conversations),
        event_cleanup("captured-images", erase_images),
        event_cleanup("owner_authority", erase_owner_authority),
        event_cleanup("owner_input", erase_owner_inputs),
        event_cleanup("observations", erase_observations),
        event_cleanup("affect", erase_affect),
        DerivedCleanup(
            "reflections",
            lambda store, scope: erase_reflections(
                store, scope.event_ids, scope.clear_reply_derivatives
            ),
        ),
        event_cleanup("consolidation", erase_consolidation),
        event_cleanup("rem", erase_rem),
        event_cleanup("memory-work", erase_memory_work),
        event_cleanup("memory-enrichment", erase_memory_enrichment),
        event_cleanup("memory-references", erase_memory_references),
        event_cleanup("mood-appraisals", erase_mood_appraisals),
        event_cleanup("goals", erase_goals),
        event_cleanup("life-topics", erase_topics),
        event_cleanup("reading", erase_reading),
        event_cleanup("exploration", erase_exploration),
        event_cleanup("executive", erase_executive),
        event_cleanup("media", erase_media_links),
        event_cleanup("generated-images", erase_generated_images),
        event_cleanup("meme-captions", erase_captions),
        event_cleanup("meme-assets", erase_meme_assets),
        event_cleanup("content", erase_content),
        event_cleanup("poke", erase_poke),
        event_cleanup("social-autonomy", erase_social_autonomy),
        event_cleanup("voice", erase_voice_jobs),
        event_cleanup("bili-video", erase_bili_video),
        event_cleanup("watch-ledger", erase_watch_ledger),
        event_cleanup("style-distill", erase_style_distill),
        event_cleanup("jargon", erase_jargon),
        event_cleanup("evaluation", erase_evaluation),
    )


def contract_signature():
    # One rebuildable marker, not a new authority or Ledger format/version.
    return ",".join(
        (str(CONTRACT_REVISION), "diary-projections", *(item.name for item in cleanup_contracts()))
    )


def erase_derivatives(store, scope):
    if not store.db.in_transaction:
        raise RuntimeError("ERASURE_REQUIRES_TRANSACTION")
    for contract in cleanup_contracts():
        contract.erase(store, scope)
