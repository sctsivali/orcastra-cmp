"""uvicorn on a loopback port in a thread, for tests that need a real server and a real client.

Starlette's TestClient reports a WebSocket closed before accept as a close frame, while uvicorn
answers it with HTTP 403; only a real server shows what a browser sees.
"""

import socket
import threading
import time

from fastapi import FastAPI


class LoopbackServer:
    def __init__(self, app: FastAPI) -> None:
        import uvicorn

        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        config = uvicorn.Config(app, lifespan="off", log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [self._sock]}, daemon=True)

    def __enter__(self) -> "LoopbackServer":
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *_args) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        self._sock.close()
