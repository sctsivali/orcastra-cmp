"""The OpenAPI title is the one product-name surface no deployment can show us.

``create_app`` sets ``openapi_url`` to None unless DEBUG is on, so on a normal install
``/openapi.json``, ``/docs`` and ``/redoc`` all answer 404 and the title cannot be read back
over HTTP. Worse, the operator tooling treats an exposed ``/docs`` as a deployment fault, so
turning DEBUG on to look is not an option either. Without this test the string is only ever
verified by reading the source, which is exactly how a rename leaves one surface behind.

``app.openapi()`` builds the schema in process regardless of ``openapi_url``, so the title is
checkable even though the route is not served.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def test_the_openapi_title_carries_the_product_name():
    assert create_app().openapi()["info"]["title"] == "Orcastra CMP API"


def test_the_title_still_comes_from_settings_not_a_literal():
    # An operator can override API_TITLE in the environment. If the title were hardcoded in
    # create_app the override would silently stop working, and nothing else would notice.
    assert Settings(api_title="Something Else").api_title == "Something Else"
    assert create_app().openapi()["info"]["title"] == Settings().api_title


def test_the_description_heading_matches_the_title():
    # The Swagger landing page renders both; if they drift it reads as two different products.
    assert "## Orcastra CMP API" in create_app().description
