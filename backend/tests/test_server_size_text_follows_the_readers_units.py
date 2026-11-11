"""Size text the server writes follows the reader's byte units (core/units.py).

The console sends X-Byte-Units with every API call. Anything but an exact `binary` reads decimal,
and a caller that sends nothing, such as an API client or the MCP server, keeps the decimal text it
always had. Text stored or hashed for later keeps one system and is re-rendered for each reader.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import units
from app.core.units import format_size, render_sized, reset_byte_units, use_byte_units
from app.middleware.byte_units import ByteUnitsMiddleware


def test_decimal_is_unchanged_without_a_choice() -> None:
    assert format_size(8589934592) == "8.59 GB"
    assert format_size(999) == "999 B"
    assert format_size(1000) == "1 kB"
    assert format_size(0) == "0 B"
    assert format_size(-5) == "0 B"


def test_binary_reads_whole_binary_units() -> None:
    assert format_size(8589934592, "binary") == "8 GiB"
    assert format_size(1024, "binary") == "1 KiB"
    assert format_size(1000, "binary") == "1000 B"
    assert format_size(1536 * 1024**2, "binary") == "1.5 GiB"
    assert format_size(5 * 1024**5, "binary") == "5 PiB"


def test_the_readers_choice_applies_until_it_is_reset() -> None:
    token = use_byte_units("binary")
    try:
        assert format_size(8589934592) == "8 GiB"
        assert format_size(8589934592, "decimal") == "8.59 GB", "an explicit system wins"
    finally:
        reset_byte_units(token)
    assert format_size(8589934592) == "8.59 GB"


@pytest.mark.parametrize("value", [None, "", "BINARY", "Binary", " binary", "binary ", "iec", "decimal;binary", "x" * 500])
def test_a_header_that_is_not_exactly_binary_reads_decimal(value) -> None:
    assert units.byte_units_from_header(value) == "decimal"


def test_an_unknown_system_passed_in_reads_decimal() -> None:
    assert format_size(8589934592, "nonsense") == "8.59 GB"


async def test_concurrent_requests_keep_their_own_units() -> None:
    async def one(choice: str) -> str:
        token = use_byte_units(choice)
        try:
            await asyncio.sleep(0.01)
            return format_size(8589934592)
        finally:
            reset_byte_units(token)

    results = await asyncio.gather(*(one("binary" if i % 2 else "decimal") for i in range(20)))
    assert results == ["8 GiB" if i % 2 else "8.59 GB" for i in range(20)]


def test_render_sized_prints_only_named_sizes() -> None:
    template = "Pool {pool_free} free; {not_a_size} stays; {{literal}}"
    sizes = {"pool_free": 8589934592}
    assert render_sized(template, sizes) == "Pool 8.59 GB free; {not_a_size} stays; {{literal}}"
    assert render_sized(template, sizes, "binary") == "Pool 8 GiB free; {not_a_size} stays; {{literal}}"


def test_a_text_slot_is_inserted_as_written_and_never_read_as_a_token() -> None:
    template = "Target pool '{pool}' has {free} free."
    rendered = render_sized(template, {"free": 2 * 1024**3}, "binary", texts={"pool": "{free}"})
    assert rendered == "Target pool '{free}' has 2 GiB free."


def test_render_sized_names_a_missing_size() -> None:
    assert render_sized("{used} used", {"used": None}) == "unknown used"
    assert render_sized("{used} used", {"used": None}, missing="n/a") == "n/a used"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/size")
    async def size() -> dict:
        return {"text": format_size(8589934592)}

    app.add_middleware(ByteUnitsMiddleware)
    return app


def test_the_middleware_reads_the_header_per_request() -> None:
    client = TestClient(_app())
    assert client.get("/size", headers={"X-Byte-Units": "binary"}).json() == {"text": "8 GiB"}
    assert client.get("/size", headers={"X-Byte-Units": "decimal"}).json() == {"text": "8.59 GB"}
    assert client.get("/size", headers={"X-Byte-Units": "<script>"}).json() == {"text": "8.59 GB"}
    assert client.get("/size").json() == {"text": "8.59 GB"}
    assert format_size(8589934592) == "8.59 GB", "nothing leaks out of a request"
