"""G0 structural contracts and route intents. No authentication, admission or dispatch."""

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from companion_delivery_protocol.authority import AuthorityRef

Mode = Literal["passthrough", "companion", "rp"]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Count = Annotated[int, Field(ge=0, le=2**53 - 1, strict=True)]
Positive = Annotated[int, Field(ge=1, le=2**53 - 1, strict=True)]


def _false(value):
    if value is not False:
        raise ValueError("G0_EXECUTION_DISABLED")
    return value


Disabled = Annotated[Literal[False], BeforeValidator(_false)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryScope(Contract):
    scope: Literal["none", "owner_private", "world_private"] = "none"
    read: bool = Field(default=False, strict=True)
    write: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def bounded(self):
        if self.scope == "none" and (self.read or self.write):
            raise ValueError("MEMORY_SCOPE_REQUIRED")
        return self


class GatewayPermissions(Contract):
    tools: Disabled = False
    proactive: Disabled = False
    external_operations: Disabled = False


class ModeGrant(Contract):
    mode: Mode
    memory_scope: MemoryScope = Field(default_factory=MemoryScope)
    world_ids: tuple[Identifier, ...] = Field(default=(), max_length=64)
    permissions: GatewayPermissions = Field(default_factory=GatewayPermissions)

    @model_validator(mode="after")
    def separate_real_and_fictional(self):
        allowed = {"passthrough": {"none"}, "companion": {"none", "owner_private"},
                   "rp": {"none", "world_private"}}
        if self.memory_scope.scope not in allowed[self.mode]:
            raise ValueError("GATEWAY_MEMORY_MODE_MISMATCH")
        if (self.mode == "rp") != bool(self.world_ids):
            raise ValueError("GATEWAY_WORLD_SCOPE_REQUIRED")
        if len(set(self.world_ids)) != len(self.world_ids):
            raise ValueError("GATEWAY_DUPLICATE_WORLD")
        return self


class ClientBudget(Contract):
    budget_journal_id: Identifier
    paid_contract_sha256: Sha256
    max_input_tokens: Positive
    max_output_tokens: Positive
    max_request_micro_usd: Count
    daily_calls: Count
    daily_micro_usd: Count
    concurrency: int = Field(ge=1, le=64, strict=True)


class ClientGrant(Contract):
    """Only trusted credential lookup can supply this snapshot; it is not a request body."""

    client_id: Identifier
    client_kind: Literal["api", "web", "qq", "sillytavern", "desktop_pet", "game"]
    key_id: Identifier
    grant_id: Identifier
    revision: Positive
    expires_at: AwareDatetime
    default_mode: Mode
    modes: tuple[ModeGrant, ...] = Field(min_length=1, max_length=3)
    model_aliases: tuple[Identifier, ...] = Field(min_length=1, max_length=64)
    owner_authority: AuthorityRef | None = None
    budget: ClientBudget

    @field_validator("owner_authority", mode="before")
    @classmethod
    def original_owner_reference(cls, value):
        return AuthorityRef.from_dict(value) if value is not None else None

    @model_validator(mode="after")
    def explicit_modes(self):
        modes = [item.mode for item in self.modes]
        if len(modes) != len(set(modes)) or self.default_mode not in modes:
            raise ValueError("GATEWAY_MODE_GRANT_INVALID")
        if len(set(self.model_aliases)) != len(self.model_aliases):
            raise ValueError("GATEWAY_DUPLICATE_ALIAS")
        if any(mode != "passthrough" for mode in modes) and self.owner_authority is None:
            raise ValueError("GATEWAY_OWNER_REFERENCE_REQUIRED")
        return self


class ModelAlias(Contract):
    model_alias: Identifier
    route_id: Identifier
    revision: Positive
    # Mode, identity and memory permissions deliberately have no alias fields.


class RequestContext(Contract):
    """Untrusted optional HTTP-header selectors, checked against the trusted grant."""

    mode: Mode | None = None
    conversation_id: Identifier | None = None
    world_id: Identifier | None = None


class SessionBinding(Contract):
    client_id: Identifier
    mode: Mode
    conversation_id: Identifier
    world_id: Identifier | None = None

    @model_validator(mode="after")
    def world(self):
        if (self.mode == "rp") != (self.world_id is not None):
            raise ValueError("GATEWAY_SESSION_WORLD_MISMATCH")
        return self

    def namespace(self):
        return self.client_id, self.mode, self.world_id, self.conversation_id


class TextMessage(Contract):
    role: Literal["system", "developer", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32000)


class ChatRequest(Contract):
    model: Identifier
    messages: tuple[TextMessage, ...] = Field(min_length=1, max_length=64)
    stream: Disabled = False
    max_completion_tokens: Positive | None = None
    max_tokens: Positive | None = None
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def bounded_text(self):
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("GATEWAY_AMBIGUOUS_TOKEN_LIMIT")
        if sum(len(item.content.encode("utf-8")) for item in self.messages) > 128000:
            raise ValueError("GATEWAY_INPUT_TOO_LARGE")
        if not any(item.role == "user" for item in self.messages):
            raise ValueError("GATEWAY_USER_MESSAGE_REQUIRED")
        return self


class RouteIntent(Contract):
    """A structural plan never substitutes for current authorization or ModelAdmission."""

    client_id: Identifier
    grant_id: Identifier
    grant_revision: Positive
    model_alias: Identifier
    alias_revision: Positive
    route_id: Identifier
    mode: Mode
    memory_scope: MemoryScope
    session: SessionBinding | None
    budget: ClientBudget
    max_output_tokens: Positive
    dispatch_allowed: Disabled = False


def route_intent(grant: ClientGrant, alias: ModelAlias, request: ChatRequest,
                 context: RequestContext | None = None):
    """Resolve only frozen values. G1/G2 must check live custody and reserve before I/O."""
    context = context or RequestContext()
    if request.model != alias.model_alias or request.model not in grant.model_aliases:
        raise ValueError("GATEWAY_MODEL_NOT_ALLOWED")
    mode = context.mode or grant.default_mode
    selected = next((item for item in grant.modes if item.mode == mode), None)
    if selected is None:
        raise ValueError("GATEWAY_MODE_NOT_ALLOWED")
    if (mode == "rp") != (context.world_id is not None):
        raise ValueError("GATEWAY_WORLD_SCOPE_REQUIRED")
    if mode == "rp" and context.world_id not in selected.world_ids:
        raise ValueError("GATEWAY_WORLD_NOT_ALLOWED")
    if mode != "passthrough" and context.conversation_id is None:
        raise ValueError("GATEWAY_CONVERSATION_REQUIRED")
    maximum = request.max_completion_tokens or request.max_tokens or grant.budget.max_output_tokens
    if maximum > grant.budget.max_output_tokens:
        raise ValueError("GATEWAY_OUTPUT_LIMIT")
    session = SessionBinding(
        client_id=grant.client_id, mode=mode, conversation_id=context.conversation_id,
        world_id=context.world_id,
    ) if context.conversation_id is not None else None
    return RouteIntent(
        client_id=grant.client_id, grant_id=grant.grant_id, grant_revision=grant.revision,
        model_alias=alias.model_alias, alias_revision=alias.revision, route_id=alias.route_id,
        mode=mode, memory_scope=selected.memory_scope, session=session, budget=grant.budget,
        max_output_tokens=maximum,
    )


class GatewayContract(Contract):
    """Schema catalog. Runtime listener configuration belongs to GatewayConfig."""

    schema_version: Literal[1]
    client_grant: ClientGrant
    model_alias: ModelAlias
    request_context: RequestContext
    chat_request: ChatRequest
    route_intent: RouteIntent

    @field_validator("schema_version", mode="before")
    @classmethod
    def version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("GATEWAY_CONTRACT_VERSION_UNSUPPORTED")
        return value
