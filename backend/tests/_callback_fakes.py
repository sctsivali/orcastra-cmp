"""Answer the requests sent to a stored `app_url` from a test handler.

The callbacks go through `services/orcahub_callbacks.py`, which pins every connection to the
address it judged, so a request reaches a resolved literal rather than the name. Tests that are
about what is sent and what is done with the answer replace the two helpers here and keep the
helpers' contract: a non-2xx POST and a 4xx/5xx GET raise SafeHTTPError with the status. The
pinning itself is tested in test_a_callback_reaches_only_the_address_it_judged.py.
"""

from typing import Callable

import httpx

from app.core.safe_http import SafeHTTPError
from app.services import orcahub_callbacks, orcahub_sync, orcahub_webhook

Handler = Callable[[httpx.Request], httpx.Response]


def answer_callbacks(monkeypatch, handler: Handler) -> None:
    async def post_to_app(url, *, content, headers, transport=None):
        response = handler(httpx.Request("POST", url, headers=dict(headers), content=content))
        if not 200 <= response.status_code < 300:
            raise SafeHTTPError(f"the app answered {response.status_code}", status_code=response.status_code)
        return response.status_code, response.content[: orcahub_callbacks.RESPONSE_HEAD_BYTES]

    async def get_from_app(url, *, headers, transport=None):
        response = handler(httpx.Request("GET", url, headers=dict(headers)))
        if response.status_code >= 400:
            raise SafeHTTPError(f"upstream returned {response.status_code}", status_code=response.status_code)
        return response.content

    monkeypatch.setattr(orcahub_callbacks, "post_to_app", post_to_app)
    monkeypatch.setattr(orcahub_callbacks, "get_from_app", get_from_app)
    monkeypatch.setattr(orcahub_sync, "post_to_app", post_to_app)
    monkeypatch.setattr(orcahub_webhook, "post_to_app", post_to_app)
