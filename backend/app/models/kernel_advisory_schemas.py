"""Response models for host kernel advisories.

note: every field a cached or older payload might lack has a default, as FanoutReachability
asks of its subclasses.
"""

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.models.schemas import FanoutReachability

AdvisoryState = Literal["patched", "exposed", "unverified"]
AdvisorySeverity = Literal["critical", "warning", "info"]


class AdvisoryScoreOut(BaseModel):
    source: str
    score: Optional[float] = None
    vector: Optional[str] = None
    rating: Optional[str] = None


class KernelAdvisoryInfo(BaseModel):
    id: str
    name: Optional[str] = None
    summary: str
    scores: List[AdvisoryScoreOut] = Field(default_factory=list)
    reference_url: str
    data_as_of: date
    introduced: str
    fixed_upstream: List[str] = Field(default_factory=list)
    fixed_mainline: str


class KernelAdvisoryFinding(BaseModel):
    advisory_id: str
    state: AdvisoryState
    severity: AdvisorySeverity
    reason: str
    check_command: Optional[str] = None
    fixed_in: Optional[str] = None
    reference_url: str


class HostKernelRow(BaseModel):
    cluster_id: str
    display_name: str
    member: str
    clustered: bool = False
    members_listed: bool = True
    kernel_version: Optional[str] = None
    os_name: Optional[str] = None
    os_version: Optional[str] = None
    vm_capable: bool = False
    read_error: Optional[str] = None
    collected_at: Optional[datetime] = None
    findings: List[KernelAdvisoryFinding] = Field(default_factory=list)


class KernelAdvisoryCounts(BaseModel):
    exposed: int = 0
    unverified: int = 0
    patched: int = 0


class KernelAdvisoryResponse(FanoutReachability):
    advisories: List[KernelAdvisoryInfo] = Field(default_factory=list)
    hosts: List[HostKernelRow] = Field(default_factory=list)
    counts: KernelAdvisoryCounts = Field(default_factory=KernelAdvisoryCounts)


class KernelAdvisoryWarning(BaseModel):
    """One `/warnings/system/all` row: a member that is exposed or cannot be verified."""

    cluster_id: str
    display_name: str
    member: str
    warning_type: Literal["kernel_exposed", "kernel_unverified"]
    advisory_id: str
    state: AdvisoryState
    severity: AdvisorySeverity
    message: str
    kernel_version: Optional[str] = None
    os_name: Optional[str] = None
    os_version: Optional[str] = None
    check_command: Optional[str] = None
    reference_url: str
    data_as_of: date
