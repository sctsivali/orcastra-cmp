"""Request and response shapes for the add-on registry, installations and their settings.

Every boundary rule an add-on is held to lives here or in `core/extension_manifest.py`. The
division is that the manifest module holds the vocabulary and the pure functions, and this module
holds the shapes a request arrives in.

important: cluster grants reuse `ClusterPermissionRequest` from the integrations router rather
than restating its rules. That model already normalises the project allow-list, refuses the
`["tenant-a", "*"]` mix, caps the list, and refuses a grant that grants nothing. A second
declaration of the same shape is how one of them ends up a rule behind the other, which is the
same reason this codebase now has exactly one private-network table.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.api_key_capabilities import ELEVATED_REASONS, GUEST_ACCESS_CAPABILITIES
from app.core.extension_manifest import (
    EXTENSION_CAPABILITIES,
    EXTENSION_EVENTS,
    MAX_ENUM_OPTIONS,
    MAX_SETTINGS_FIELDS,
    RESERVED_SLUGS,
    SETTING_KEY_RE,
    SETTING_TYPES,
    SLUG_MAX_LENGTH,
    SLUG_MIN_LENGTH,
    SLUG_RE,
    VERSION_RE,
)
from app.core.url_safety import assert_url_is_fetchable, describe_url_risk
from app.routers.integrations import ClusterPermissionRequest


# ── the declared settings form ─────────────────────────────────────────────────────────────────

class SettingField(BaseModel):
    """One configuration field the dashboard renders on the add-on's behalf.

    A closed vocabulary rather than JSON Schema. The add-on ships no UI, so the host draws this
    form, and an open schema language would let an author describe something we cannot render
    consistently or safely.
    """

    key: str = Field(..., description="Stable identifier the add-on reads its value back by")
    type: str = Field(..., description=" | ".join(SETTING_TYPES))
    label: str = Field(..., min_length=1, max_length=100)
    help: Optional[str] = Field(None, max_length=500)
    required: bool = False
    default: Optional[Any] = None
    options: Optional[List[str]] = Field(None, description="enum only")
    min: Optional[int] = Field(None, description="integer only")
    max: Optional[int] = Field(None, description="integer only")

    @field_validator("key")
    @classmethod
    def _key_is_a_stable_identifier(cls, value: str) -> str:
        if not SETTING_KEY_RE.match(value):
            raise ValueError(
                "key must be lowercase ASCII, start with a letter, and use only letters, "
                "digits and underscores"
            )
        return value

    @field_validator("type")
    @classmethod
    def _type_is_one_we_can_draw(cls, value: str) -> str:
        if value not in SETTING_TYPES:
            raise ValueError(f"type must be one of: {', '.join(sorted(SETTING_TYPES))}")
        return value

    @model_validator(mode="after")
    def _the_shape_matches_the_type(self) -> "SettingField":
        if self.type == "enum":
            if not self.options:
                raise ValueError("an enum field must list its options")
            if len(self.options) > MAX_ENUM_OPTIONS:
                raise ValueError(f"an enum field may list at most {MAX_ENUM_OPTIONS} options")
            if len(set(self.options)) != len(self.options):
                raise ValueError("enum options must be unique")
            if self.default is not None and self.default not in self.options:
                raise ValueError("the default must be one of the options")
        elif self.options:
            raise ValueError("options are only meaningful for an enum field")

        if self.type != "integer" and (self.min is not None or self.max is not None):
            raise ValueError("min and max are only meaningful for an integer field")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not exceed max")

        # important: a secret is write-only. A default would be a credential written into a
        # manifest that every operator considering the add-on can read.
        if self.type == "secret" and self.default is not None:
            raise ValueError("a secret field cannot declare a default")
        return self


# ── the manifest ───────────────────────────────────────────────────────────────────────────────

class RequestedScope(BaseModel):
    """The shape a manifest asks for. Not values: an author does not name a customer's clusters."""

    permission: str = Field("read", description="read | write | admin")
    clusters: str = Field("selected", description="all | selected")
    projects: str = Field("selected", description="all | selected")

    @field_validator("permission")
    @classmethod
    def _permission_is_known(cls, value: str) -> str:
        if value not in ("read", "write", "admin"):
            raise ValueError("permission must be read, write or admin")
        return value

    @field_validator("clusters", "projects")
    @classmethod
    def _breadth_is_known(cls, value: str) -> str:
        if value not in ("all", "selected"):
            raise ValueError("must be 'all' or 'selected'")
        return value


class ExtensionManifest(BaseModel):
    """What an author submits to publish an add-on."""

    slug: str = Field(..., min_length=SLUG_MIN_LENGTH, max_length=SLUG_MAX_LENGTH)
    name: str = Field(..., min_length=1, max_length=100)
    publisher: str = Field(..., min_length=1, max_length=100)
    version: str = Field(..., max_length=32)
    description: Optional[str] = Field(None, max_length=2000)
    homepage_url: Optional[str] = Field(None, max_length=512)
    support_url: Optional[str] = Field(None, max_length=512)
    capabilities: List[str] = Field(..., min_length=1, max_length=len(EXTENSION_CAPABILITIES))
    scope: RequestedScope = Field(default_factory=RequestedScope)
    settings: List[SettingField] = Field(default_factory=list)
    webhook_url: Optional[str] = Field(None, max_length=512)
    webhook_events: Optional[List[str]] = Field(None, max_length=len(EXTENSION_EVENTS))

    @field_validator("slug")
    @classmethod
    def _slug_is_lowercase_ascii(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(
                "slug must be lowercase ASCII letters, digits and hyphens, "
                "starting and ending with a letter or digit"
            )
        # note: refuse rather than normalise. NFKC folds FULLWIDTH ASTERISK onto `*`, which is
        # how a lookalike becomes a wildcard elsewhere in this codebase, so a charset that
        # rejects is safer than one that transforms.
        if value in RESERVED_SLUGS:
            raise ValueError("that slug is reserved")
        return value

    @field_validator("version")
    @classmethod
    def _version_is_semver(cls, value: str) -> str:
        if not VERSION_RE.match(value):
            raise ValueError("version must be MAJOR.MINOR.PATCH")
        return value

    @field_validator("homepage_url", "support_url")
    @classmethod
    def _a_rendered_link_is_checked_without_a_lookup(cls, value: Optional[str]) -> Optional[str]:
        """These are shown to an operator and never fetched by this server.

        So they are checked for scheme and shape but not resolved. Resolving costs a DNS lookup
        on every write for no security benefit, and would refuse a legitimate homepage behind a
        CDN that happens to answer with a private address from our vantage point. Fetched versus
        rendered is what decides which guard applies.
        """
        if value is None:
            return None
        reason = describe_url_risk(value, require_https=True, resolve=False)
        if reason is not None:
            raise ValueError(f"not a usable link: {reason}")
        return value

    @field_validator("webhook_url")
    @classmethod
    def _a_delivery_endpoint_is_resolved(cls, value: Optional[str]) -> Optional[str]:
        """The one URL this server stores and later contacts, so it gets the full check."""
        if value is None:
            return None
        return assert_url_is_fetchable(value, field="webhook_url", require_https=True)

    @field_validator("capabilities")
    @classmethod
    def _capabilities_are_in_the_vocabulary(cls, value: List[str]) -> List[str]:
        unknown = sorted(set(value) - EXTENSION_CAPABILITIES)
        if unknown:
            raise ValueError(f"unknown capabilities: {', '.join(unknown)}")
        if len(set(value)) != len(value):
            raise ValueError("capabilities must be unique")
        return sorted(set(value))

    @field_validator("webhook_events")
    @classmethod
    def _events_are_in_the_catalogue(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        unknown = sorted(set(value) - set(EXTENSION_EVENTS))
        if unknown:
            raise ValueError(f"unknown events: {', '.join(unknown)}")
        return sorted(set(value))

    @model_validator(mode="after")
    def _the_settings_form_is_coherent(self) -> "ExtensionManifest":
        if len(self.settings) > MAX_SETTINGS_FIELDS:
            raise ValueError(f"at most {MAX_SETTINGS_FIELDS} settings fields")
        keys = [field.key for field in self.settings]
        if len(set(keys)) != len(keys):
            raise ValueError("settings keys must be unique")
        if self.webhook_events and not self.webhook_url:
            raise ValueError("webhook_events needs a webhook_url to deliver to")
        return self


class ExtensionUpdate(BaseModel):
    """A published add-on may change everything except who it is."""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=2000)
    homepage_url: Optional[str] = Field(None, max_length=512)
    support_url: Optional[str] = Field(None, max_length=512)
    status: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _status_is_known(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in ("draft", "published", "deprecated", "disabled"):
            raise ValueError("status must be draft, published, deprecated or disabled")
        return value


# ── consent and installation ───────────────────────────────────────────────────────────────────

class ConsentBlock(BaseModel):
    """What the operator was actually shown, echoed back so the server can check it still holds.

    important: this is not ceremony. Without it the attack is publish a benign manifest, wait for
    the consent screen to render, update the manifest to ask for more, and let the install request
    land against the new one. One round trip, one real control.
    """

    version: str = Field(..., max_length=32)
    manifest_digest: str = Field(..., min_length=64, max_length=64)
    accepted_capabilities: List[str] = Field(..., min_length=1)

    @field_validator("manifest_digest")
    @classmethod
    def _digest_is_a_sha256(cls, value: str) -> str:
        if not all(c in "0123456789abcdef" for c in value.lower()):
            raise ValueError("manifest_digest must be a hex sha256")
        return value.lower()


class InstallRequest(BaseModel):
    organization_id: int
    consent: ConsentBlock
    capabilities: List[str] = Field(..., min_length=1)
    cluster_permissions: List[ClusterPermissionRequest] = Field(..., min_length=1)
    settings: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = Field(None, max_length=128)

    @field_validator("capabilities")
    @classmethod
    def _capabilities_are_in_the_vocabulary(cls, value: List[str]) -> List[str]:
        unknown = sorted(set(value) - EXTENSION_CAPABILITIES)
        if unknown:
            raise ValueError(f"unknown capabilities: {', '.join(unknown)}")
        return sorted(set(value))

    @model_validator(mode="after")
    def _consent_covers_what_is_being_granted(self) -> "InstallRequest":
        """A grant wider than what was consented to is refused before it reaches a service."""
        extra = sorted(set(self.capabilities) - set(self.consent.accepted_capabilities))
        if extra:
            raise ValueError(
                f"capabilities not covered by the consent shown: {', '.join(extra)}"
            )
        return self


class ScopeRequest(BaseModel):
    """Re-scoping is declarative: the request states the whole grant, not a delta."""

    consent: ConsentBlock
    capabilities: List[str] = Field(..., min_length=1)
    cluster_permissions: List[ClusterPermissionRequest] = Field(..., min_length=1)


class SettingsUpdate(BaseModel):
    """Values for the fields the manifest declared.

    A secret field's value is accepted here and never returned. Omitting a secret key leaves the
    stored value alone, which is what lets a form save without re-typing every credential.
    """

    values: Dict[str, Any] = Field(default_factory=dict)


# ── responses ──────────────────────────────────────────────────────────────────────────────────

class CapabilityRequestInfo(BaseModel):
    """One requested capability as the consent screen needs it, with the server's own reason."""

    name: str
    description: str
    elevated: bool
    elevated_reason: Optional[str] = None
    reaches_inside_a_guest: bool


def capability_request_info(name: str, description: str) -> CapabilityRequestInfo:
    return CapabilityRequestInfo(
        name=name,
        description=description,
        elevated=name in ELEVATED_REASONS,
        elevated_reason=ELEVATED_REASONS.get(name),
        reaches_inside_a_guest=name in GUEST_ACCESS_CAPABILITIES,
    )


class ExtensionResponse(BaseModel):
    id: int
    slug: str
    name: str
    publisher: str
    version: str
    status: str
    description: Optional[str] = None
    homepage_url: Optional[str] = None
    support_url: Optional[str] = None
    owner_email: str
    requested_capabilities: List[str]
    requested_scope: Dict[str, Any]
    settings_schema: Optional[List[Dict[str, Any]]] = None
    default_webhook_url: Optional[str] = None
    default_webhook_events: Optional[List[str]] = None
    manifest_digest: str
    created_at: datetime
    updated_at: datetime


class InstallResponse(BaseModel):
    """An installation as it is read back. Never carries a secret of any kind."""

    id: int
    extension_id: int
    extension_slug: str
    extension_name: str
    # note: the version this installation consented to, not what the add-on has published since.
    extension_version: str
    # Present only when the add-on has moved on, so the difference is visible to the operator who
    # would have to approve it. Null when the installation is on the current version.
    available_version: Optional[str] = None
    organization_id: int
    organization_slug: Optional[str] = None
    status: str
    granted_capabilities: List[str]
    granted_scope: List[Dict[str, Any]]
    settings: Dict[str, Any] = Field(default_factory=dict)
    # important: the NAMES of the secret fields that hold a value, never the values. A form needs
    # to show a field as configured without the server handing back what was typed into it.
    configured_secrets: List[str] = Field(default_factory=list)
    api_key_id: str
    credential_generation: int
    consent_version: str
    consented_at: datetime
    installed_by: str
    suspended_at: Optional[datetime] = None
    suspended_by: Optional[str] = None
    suspend_reason: Optional[str] = None
    # Credential health, so "is this add-on working" is answerable without reading a log.
    # `credential_readable` false means the key could not be read, which is a different thing from
    # a credential that has never been used and must not render as one.
    credential_last_used_at: Optional[str] = None
    credential_expires_at: Optional[str] = None
    credential_readable: bool = True
    created_at: datetime
    updated_at: datetime


class InstallCreateResponse(BaseModel):
    """The one response that carries secrets, and it is shown once.

    Two of them, deliberately separate. The API credential authenticates the add-on to us; the
    signing secret lets it verify that a delivery came from us. Making them the same value, which
    is what the existing OrcaHub emitter does, means anyone who can merely verify a signature
    holds a live control-plane credential.
    """

    install: InstallResponse
    api_key_id: str
    api_key_secret: str
    webhook_signing_secret: str
