"""Storage-free configuration for media sessions and owner-shared content."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from companion.trial_limits import CountLimit
from companion_delivery_protocol.authority import MAX_OWNER_IMAGE_BYTES


class MediaEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = ""
    key: SecretStr = SecretStr("")
    model: str = Field(default="", max_length=128)
    protocol: Literal["openai", "minimax"] = "openai"
    # Configured estimate for records without provider billing evidence.
    estimated_request_micro_usd: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_url(self):
        if self.url:
            parts = urlsplit(self.url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username
                or parts.query
                or parts.fragment
            ):
                raise ValueError("media endpoint must be HTTPS without credentials/query/fragment")
        return self


class MediaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    max_session_seconds: int = Field(default=180, ge=5, le=600)
    max_session_bytes: int = Field(default=2_000_000, ge=1024, le=50_000_000)
    max_daily_bytes: int = Field(default=10_000_000, ge=0, le=1_000_000_000)
    max_chunk_bytes: int = Field(default=64_000, ge=1024, le=64_000)
    max_output_bytes: int = Field(default=256_000, ge=1024, le=2_000_000)
    max_active_sessions: int = Field(default=2, ge=1, le=5)
    provider_timeout_seconds: int = Field(default=20, ge=1, le=60)
    speech_voice: str = Field(default="alloy", min_length=1, max_length=128)
    voice_adapter: str = "media_voice"
    camera_adapter: str = "media_camera"
    android_adapter: str = "media_android"
    stt: MediaEndpoint = Field(default_factory=MediaEndpoint)
    tts: MediaEndpoint = Field(default_factory=MediaEndpoint)
    vision: MediaEndpoint = Field(default_factory=MediaEndpoint)


ImageSize = Literal["512x512", "768x768", "1024x1024", "1024x1536", "1536x1024"]


class ImageGenerationConfig(BaseModel):
    """One owner-chat tool call per reply; see companion/image_generation.py."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    # OpenAI-compatible POST .../images/generations returning b64_json PNG/JPEG.
    endpoint: MediaEndpoint = Field(default_factory=MediaEndpoint)
    sizes: tuple[ImageSize, ...] = Field(default=("1024x1024",), min_length=1, max_length=5)
    # Each call also needs this much left on the chat job lease, else it is refused.
    provider_timeout_seconds: int = Field(default=60, ge=5, le=240)
    # The Bridge accepts at most 4 MiB, 2048 px per side; see companion_meme_protocol.
    max_output_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=4 * 1024 * 1024)
    max_daily_images: int = Field(default=10, ge=0, le=100)
    cooldown_seconds: int = Field(default=300, ge=0, le=86400)
    retention_seconds: int = Field(default=3600, ge=60, le=86400)

    @model_validator(mode="after")
    def valid_provider(self):
        if self.enabled:
            endpoint = self.endpoint
            if not endpoint.url or not endpoint.model or not endpoint.key.get_secret_value():
                raise ValueError("image generation needs endpoint url, model and key")
            if endpoint.protocol != "openai":
                raise ValueError("image generation supports the openai protocol only")
        return self


class ContentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_links_per_event: int = Field(default=2, ge=1, le=2)
    max_images_per_event: CountLimit = 4
    max_images_per_article: int = Field(default=32, ge=1, le=32)
    use_chat_model_for_vision: bool = False
    max_image_bytes: int = Field(default=1_000_000, ge=1024, le=MAX_OWNER_IMAGE_BYTES)
    max_daily_image_bytes: int = Field(default=32_000_000, ge=0, le=800 * 1024 * 1024)
    max_daily_images: int = Field(default=32, ge=0, le=50)
    wait_seconds: int = Field(default=30, ge=1, le=90)
    upload_window_seconds: int = Field(default=300, ge=30, le=900)
    provider_timeout_seconds: int = Field(default=20, ge=1, le=30)
    max_context_chars: int = Field(default=9000, ge=1000, le=16000)
    image_adapter: str = "content_image"
    vision: MediaEndpoint = Field(default_factory=MediaEndpoint)
