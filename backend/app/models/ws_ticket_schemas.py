"""POST /ws-tickets: what a page asks to open, and the tickets it gets back."""

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field
from typing_extensions import Annotated

from app.core.lxd_names import LXD_NAME_PATTERN

_HOST = Field(..., min_length=1, max_length=253, description="Registered Cluster identifier")
_PROJECT = Field("default", pattern=LXD_NAME_PATTERN, description="LXD project name")
_INSTANCE = Field(..., pattern=LXD_NAME_PATTERN, description="Instance name")


class MonitoringTicketRequest(BaseModel):
    kind: Literal["monitoring"]


class TasksTicketRequest(BaseModel):
    kind: Literal["tasks"]


class EventsTicketRequest(BaseModel):
    kind: Literal["events"]
    host: str = _HOST


class TerminalTicketRequest(BaseModel):
    kind: Literal["terminal"]
    host: str = _HOST
    project: str = _PROJECT
    instance: str = _INSTANCE


class ConsoleTicketRequest(BaseModel):
    kind: Literal["console"]
    host: str = _HOST
    project: str = _PROJECT
    instance: str = _INSTANCE
    console_type: Literal["vga", "console"] = "vga"


WsTicketRequest = Annotated[
    Union[
        MonitoringTicketRequest,
        TasksTicketRequest,
        EventsTicketRequest,
        TerminalTicketRequest,
        ConsoleTicketRequest,
    ],
    Field(discriminator="kind"),
]


class WsTicketResponse(BaseModel):
    ticket: str = Field(..., description="Opens one socket: pass it as ?ticket= within expires_in seconds")
    tickets: Optional[List[str]] = Field(
        None,
        description="A graphical console's SPICE channels each open their own socket; one ticket per channel",
    )
    expires_in: int = Field(..., description="Seconds the tickets stay redeemable")
