"""A download that did not finish must not look finished at the HTTP level.

Raising is not enough here. Every middleware in this app is `BaseHTTPMiddleware`, and on
Starlette 1.7 its `_StreamingResponse` sends the terminal chunk when the inner app stops, and only
then re-raises. A chunked archive cut mid-stream (ex; lxd-agent killed mid-archive) would end
normally with its gzip stream cut short, which a browser reports as a finished download of a
corrupt file.
"""

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.middleware.stream_cut import StreamCutMiddleware, mark_cut


class _Pass(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        return await call_next(request)


async def _cut(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"partial", "more_body": True})
    mark_cut(scope)
    raise RuntimeError("guest went away")


async def _complete(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"whole", "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _quiet_stop(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"partial", "more_body": True})
    mark_cut(scope)


def _stack(endpoint):
    """Four layers, like main.py, with the guard outermost."""
    app = endpoint
    for _ in range(4):
        app = _Pass(app)
    return StreamCutMiddleware(app)


def _scope():
    return {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"", "server": ("x", 1), "scheme": "http", "http_version": "1.1", "asgi": {"version": "3.0"}}


async def _run(app):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    raised = None
    try:
        await app(_scope(), receive, send)
    except Exception as exc:  # noqa: BLE001 - the point of the test is what comes out
        raised = exc
    return sent, raised


def _ended(sent):
    return any(m["type"] == "http.response.body" and not m.get("more_body", False) for m in sent)


async def test_without_the_guard_the_stack_finishes_a_cut_response():
    """Why the guard exists: if this ever stops being true, it can go."""
    app = _cut
    for _ in range(4):
        app = _Pass(app)
    sent, _ = await _run(app)
    assert _ended(sent)


async def test_a_cut_stream_reaches_the_server_with_no_terminal_chunk():
    sent, raised = await _run(_stack(_cut))
    assert any(m.get("body") == b"partial" for m in sent)
    assert not _ended(sent)
    assert raised is not None


async def test_a_marked_stream_stays_cut_even_if_nothing_raises():
    sent, _ = await _run(_stack(_quiet_stop))
    assert not _ended(sent)


async def test_a_complete_stream_is_untouched():
    sent, raised = await _run(_stack(_complete))
    assert _ended(sent)
    assert raised is None


async def test_an_ordinary_response_passes_through():
    app = Starlette(routes=[Route("/", lambda request: PlainTextResponse("ok"))])
    sent, raised = await _run(_stack(app))
    assert _ended(sent)
    assert raised is None


def test_the_guard_is_the_outermost_layer_of_the_app(monkeypatch):
    import os

    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
    os.environ.setdefault("SECRET_KEY", "test-secret-key")
    os.environ.setdefault("APP_VERSION", "0.0.0-test")
    os.environ.setdefault("CORS_ORIGINS", "http://localhost")
    from app.main import create_app

    app = create_app()
    # user_middleware is outermost first.
    assert app.user_middleware[0].cls is StreamCutMiddleware
