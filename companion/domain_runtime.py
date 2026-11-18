"""Per-process domain services backed by one bootstrapped Runtime and Store."""

from functools import cached_property

from companion.affect import AffectEngine
from companion.bili_video import BiliVideoService
from companion.consolidation import ConsolidationSchedule
from companion.content import ContentService
from companion.diary import DiaryService
from companion.executive import Executive
from companion.exploration import ExplorationService
from companion.goals import GoalEngine
from companion.growth import ExpressionLearning
from companion.life_topics import LifeTopics
from companion.media import MediaService
from companion.meme_core import MemeCaptionService
from companion.memory import Memory
from companion.memory_context import MemoryContext
from companion.mood import MoodService
from companion.observation import ObservationService
from companion.reflection import ReflectionService
from companion.runtime import Runtime
from companion.social import SocialPlanner
from companion.store import utcnow
from companion.voice_flow import VoiceFlow


class DomainRuntime(Runtime):
    def __init__(self, settings, model=None, clock=utcnow):
        super().__init__(settings, clock)
        self.model = model

    @cached_property
    def memory(self):
        return Memory(self.store, self.settings, self.ledger)

    @cached_property
    def memory_context(self):
        return MemoryContext(self.memory)

    @cached_property
    def mood(self):
        return MoodService(self.memory, self.clock)

    @cached_property
    def diary(self):
        return DiaryService(self.store, self.settings, self.model, self.ledger, self.clock)

    @cached_property
    def observations(self):
        return ObservationService(self.store, self.settings, self.clock)

    @cached_property
    def media(self):
        return MediaService(self.store, self.settings, self.clock, self.observations)

    @cached_property
    def affect(self):
        return AffectEngine(self.store, self.settings, self.clock)

    @cached_property
    def expression_learning(self):
        return ExpressionLearning(self.store, self.settings, self.clock)

    @cached_property
    def reflections(self):
        return ReflectionService(
            self.store, self.settings, self.model, self.diary.repo, self.ledger, self.clock,
        )

    @cached_property
    def goals(self):
        return GoalEngine(self.store, self.settings, self.ledger, self.clock)

    @cached_property
    def consolidation(self):
        return ConsolidationSchedule(self.store, self.settings, self.diary, self.reflections,
                                     self.clock)

    @cached_property
    def social(self):
        return SocialPlanner(self.store, self.settings, self.clock)

    @cached_property
    def life_topics(self):
        return LifeTopics(self.store, self.settings, self.clock)

    @cached_property
    def exploration(self):
        return ExplorationService(
            self.store, self.settings, self.observations, self.goals, self.social, self.clock,
        )

    @cached_property
    def bili_video(self):
        return BiliVideoService(self.store, self.settings, self.exploration, self.model, self.clock)

    @cached_property
    def content(self):
        return ContentService(
            self.store, self.settings, self.observations, self.exploration.create, self.clock,
            model=lambda: self.model, video=self.bili_video,
        )

    @cached_property
    def memes(self):
        return MemeCaptionService(self.store, self.settings, lambda: self.model, self.clock)

    @cached_property
    def voice(self):
        return VoiceFlow(self.store, self.settings, self.clock)

    @cached_property
    def executive(self):
        return Executive(self.store, self.settings, self.clock)
