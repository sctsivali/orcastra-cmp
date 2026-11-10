"""A limited key's local image create naming source.project is judged by every record LXD may apply.

LXD resolves a local image source by version: 5.21 looks it up in the target project
(lxd/instances_post.go, getSourceImageFromInstanceSource with targetProject.Name) and later LXD in
source.project when one is named (lxd/instance.go resolveSourceImageFromCache, localLookupProject).
Both then read the image's profiles in the target project, and both fail the create when the project
they look in holds no such image. So both records are read and the profiles of every one that exists
are judged; reading the target's alone would judge a create in tenant-a from tenant-b's image by
tenant-a's image of the same alias.
"""

from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from tests._limited_write_fakes import (  # noqa: F401 - fixtures are registered by import
    PASSTHROUGH,
    PLAIN,
    TWO,
    FakeClient,
    ProjectSession,
    _no_webhook_events,
    proxy_call,
    raw_write,
)
from tests._raw_proxy_fakes import _clean_breaker  # noqa: F401


class TwoProjectImages(ProjectSession):
    """tenant-a and tenant-b each hold an image under the alias `shared`, with different profiles.

    Profiles are read in the target, tenant-a, where `gpu-host` passes a host block device through.
    """

    PROFILES = {"tenant-a": {"default": PLAIN, "plain": PLAIN, "gpu-host": PASSTHROUGH}}
    IMAGES = {
        "tenant-a": {"a-plain": {"profiles": ["default"]}, "a-reach": {"profiles": ["gpu-host"]}},
        "tenant-b": {"b-reach": {"profiles": ["gpu-host"]}, "b-plain": {"profiles": ["plain"]},
                     "b-only": {"profiles": ["plain"]}},
    }
    ALIASES = {
        "tenant-a": {"shared": "a-plain", "mixed": "a-reach", "clean": "a-plain"},
        "tenant-b": {"shared": "b-reach", "mixed": "b-plain", "clean": "b-plain"},
    }


def _create(source: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": "c1", "source": {"type": "image", **source}}


def _image_reads(session: ProjectSession) -> List[tuple]:
    return sorted({(url.split("/1.0", 1)[1], project) for verb, url, project in session.calls
                   if verb == "GET" and "/images/" in url})


@pytest.mark.parametrize(
    "source",
    [
        {"alias": "shared", "project": "tenant-b"},
        {"fingerprint": "b-reach", "project": "tenant-b"},
        {"alias": "mixed", "project": "tenant-b"},
    ],
    ids=["source-project-image-reaches", "source-project-fingerprint-reaches", "target-image-reaches"],
)
async def test_a_create_reaching_the_host_through_either_record_is_refused(source: Dict[str, Any]) -> None:
    outcome, session = await raw_write("POST", "/1.0/instances", "tenant-a", _create(source),
                                       session=TwoProjectImages())
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403, outcome
    assert "gpu-host" in outcome.detail
    assert not session.sent


async def test_both_records_are_read() -> None:
    session = TwoProjectImages()
    await raw_write("POST", "/1.0/instances", "tenant-a", _create({"alias": "clean", "project": "tenant-b"}),
                    session=session)
    assert _image_reads(session) == [
        ("/images/a-plain", "tenant-a"), ("/images/aliases/clean", "tenant-a"),
        ("/images/aliases/clean", "tenant-b"), ("/images/b-plain", "tenant-b"),
    ]


@pytest.mark.parametrize(
    "source",
    [
        {"alias": "clean", "project": "tenant-b"},
        {"fingerprint": "b-only", "project": "tenant-b"},
        {"alias": "clean"},
    ],
    ids=["both-plain", "only-the-source-project-holds-it", "no-source-project"],
)
async def test_a_create_whose_records_reach_nothing_is_sent(source: Dict[str, Any]) -> None:
    outcome, session = await raw_write("POST", "/1.0/instances", "tenant-a", _create(source),
                                       session=TwoProjectImages())
    assert outcome.status_code == 200 and session.sent, outcome


async def test_an_image_neither_project_holds_is_still_refused() -> None:
    outcome, session = await raw_write("POST", "/1.0/instances", "tenant-a",
                                       _create({"alias": "missing", "project": "tenant-b"}),
                                       session=TwoProjectImages())
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and not session.sent


async def test_proxy_create_is_judged_the_same_way() -> None:
    outcome, client = await proxy_call("create_instance", "tenant-a", _create({"alias": "shared", "project": "tenant-b"}),
                                       api_key=TWO, client=FakeClient(TwoProjectImages()))
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert client.created == []
