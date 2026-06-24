"""Pydantic models for the global search response.

Kept out of the large ``schemas.py`` so the search surface stays self-contained.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SearchResultItem(BaseModel):
    """A single match across any resource type."""

    type: str = Field(..., description="Resource type, ex; instance, network, tag")
    id: str = Field(..., description="Stable identifier within the type")
    title: str = Field(..., description="Primary label shown to the user")
    subtitle: str = Field("", description="Secondary context line")
    score: int = Field(0, description="Relevance score (higher = better match)")
    match_field: Optional[str] = Field(None, description="Which field produced the match")
    href: str = Field("", description="In-app destination for this result")
    meta: Dict[str, Any] = Field(default_factory=dict, description="Extra fields for the UI")


class SearchGroup(BaseModel):
    """All matches of one resource type, capped to the per-type limit."""

    type: str = Field(..., description="Resource type key")
    label: str = Field(..., description="Human label for the group heading")
    total: int = Field(..., description="Total matches found (may exceed returned items)")
    items: List[SearchResultItem] = Field(default_factory=list)


class SearchResponse(BaseModel):
    """Unified, ranked, grouped search result."""

    query: str = Field(..., description="The query that was run")
    took_ms: int = Field(0, description="Server-side time spent, milliseconds")
    degraded: bool = Field(False, description="True if one or more sources failed/timed out")
    total: int = Field(0, description="Total matches across all groups")
    groups: List[SearchGroup] = Field(default_factory=list)
