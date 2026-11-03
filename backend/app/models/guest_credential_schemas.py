"""Request and response bodies of the guest credentials routes (`routers/guest_credentials.py`).

important: every request body is read with `core/json_body.read_json_body`, never declared as a
FastAPI body, and every model forbids extra fields and hides input in errors. A password or a
pasted private key that fails validation must not come back in a 422.
"""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr

_STRICT = ConfigDict(extra="forbid", hide_input_in_errors=True)


class AccountBody(BaseModel):
    model_config = _STRICT
    user: str = Field(..., min_length=1, max_length=64)


class KeyAddBody(BaseModel):
    model_config = _STRICT
    user: str = Field(..., min_length=1, max_length=64)
    # Longer than the parser's own 16 KiB limit, so an oversized paste gets its specific reason.
    public_key: str = Field(..., min_length=1, max_length=65536)


class KeyRemoveBody(BaseModel):
    model_config = _STRICT
    user: str = Field(..., min_length=1, max_length=64)
    fingerprint: str = Field(..., pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")


class PasswordSetBody(BaseModel):
    model_config = _STRICT
    user: str = Field(..., min_length=1, max_length=64)
    password: SecretStr = Field(..., max_length=1024)
    must_change: bool = False


class SudoBody(BaseModel):
    model_config = _STRICT
    user: str = Field(..., min_length=1, max_length=64)
    enabled: bool


class CreateUserBody(BaseModel):
    model_config = _STRICT
    name: str = Field(..., min_length=1, max_length=64)
    sudo: bool = False
    password: Optional[SecretStr] = Field(None, max_length=1024)
    must_change: bool = False
    public_key: Optional[str] = Field(None, max_length=65536)


class AccountView(BaseModel):
    name: str
    uid: int
    kind: str
    home: str
    shell: str
    password: str
    must_change: bool
    scheme: Optional[str] = None
    admin_groups: List[str] = []
    sudo: Optional[str] = None


class GuestView(BaseModel):
    os_id: str
    os_name: str
    family: str
    selinux: bool
    hash_default: Optional[str] = None
    instance_type: str


class Capabilities(BaseModel):
    create_user: bool
    set_password: bool
    expire: bool
    lock: bool
    sudo_group: Optional[str] = None
    sudo_installed: bool
    sudo_change: bool


class PasswordPolicy(BaseModel):
    min_length: int
    max_length: int


class OverviewResponse(BaseModel):
    guest: GuestView
    accounts: List[AccountView]
    accounts_omitted: int
    capabilities: Capabilities
    password_policy: PasswordPolicy


class HostKeyView(BaseModel):
    type: str
    bits: Optional[int] = None
    fingerprint: str


class AddressView(BaseModel):
    address: str
    interface: str
    family: str
    label: str


class UnitView(BaseModel):
    unit: str
    state: str


class SshServerResponse(BaseModel):
    installed: bool
    running: bool
    socket_activated: bool
    units: List[UnitView]
    effective: Optional[dict] = None
    notes: List[str]
    host_keys: List[HostKeyView]
    addresses: List[AddressView]


class KeyEntryView(BaseModel):
    line: int
    key_type: Optional[str] = None
    fingerprint: Optional[str] = None
    bits: Optional[int] = None
    comment: str = ""
    options: List[str] = []
    weak: bool = False


class KeysResponse(BaseModel):
    user: str
    state: str
    entries: List[KeyEntryView]
    warnings: List[str]


class KeyAddResponse(BaseModel):
    added: bool
    already_present: bool
    entry: KeyEntryView
    warnings: List[str]


class KeyRemoveResponse(BaseModel):
    removed: int


class PasswordResponse(BaseModel):
    user: str
    password: str
    must_change_applied: bool
    warnings: List[str]


class SudoResponse(BaseModel):
    user: str
    sudo: Optional[str] = None
    admin_groups: List[str]


class StepView(BaseModel):
    step: str
    result: str
    code: Optional[str] = None


class CreateUserResponse(BaseModel):
    account: Optional[AccountView] = None
    steps: List[StepView]
