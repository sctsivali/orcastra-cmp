"""The OpenAPI title carries the product name.

``create_app`` sets ``openapi_url`` to None unless DEBUG is on, so on a normal install
``/openapi.json``, ``/docs`` and ``/redoc`` answer 404 and the title cannot be read back over
HTTP. ``app.openapi()`` builds the schema in process regardless of ``openapi_url``, so the title
is checked here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def test_the_openapi_title_carries_the_product_name():
    assert create_app().openapi()["info"]["title"] == "Orcastra CMP API"


def test_the_title_still_comes_from_settings_not_a_literal():
    # An operator can override API_TITLE in the environment; a title hardcoded in create_app
    # would silently ignore the override.
    assert Settings(api_title="Something Else").api_title == "Something Else"
    assert create_app().openapi()["info"]["title"] == Settings().api_title


def test_the_description_heading_matches_the_title():
    # The Swagger landing page renders both; if they drift it reads as two different products.
    assert "## Orcastra CMP API" in create_app().description
