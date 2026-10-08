"""Location text from a client must not carry control characters.

A place name, a city or a facility reaches the backend log, the integrator's storefront and an
operator's terminal, where the region backfill prints stored names. A newline in one forges a log
line, and an escape sequence rewrites what the operator reads. GeoLocationInput refuses C0, DEL
and C1 control characters in every free-text field and still takes any printable script.
"""

import pytest
from pydantic import ValidationError

from app.models.schemas import GeoLocation, GeoLocationInput

BASE = {
    "region_id": "acme-dc-7", "region_name": "Acme DC 7", "country_code": "DE",
    "country_name": "Germany", "city": "Frankfurt", "latitude": 50.11, "longitude": 8.68,
    "timezone": "Europe/Berlin", "continent": "EU",
}
TEXT_FIELDS = ["region_name", "country_name", "city", "timezone", "facility_name",
               "facility_provider", "province", "official_name"]
CONTROL = ["line\nbreak", "car\rriage", "\x1b[31mred", "nul\x00byte", "tab\there", "del\x7f", "csi\x9bx"]


@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize("value", CONTROL, ids=["lf", "cr", "esc", "nul", "tab", "del", "c1"])
def test_a_control_character_is_refused(field, value):
    with pytest.raises(ValidationError):
        GeoLocationInput(**{**BASE, field: value})


@pytest.mark.parametrize("field,value", [
    ("city", "São Paulo"), ("region_name", "Zürich"), ("city", "東京"),
    ("official_name", "Kota Administrasi Jakarta Selatan"), ("facility_name", "Rack #7 (Row B)"),
    ("facility_name", ""),
])
def test_printable_text_in_any_script_is_kept(field, value):
    assert getattr(GeoLocationInput(**{**BASE, field: value}), field) == value


def test_a_stored_location_still_reads_back_whatever_it_holds():
    """The read model stays unconstrained: a record saved before the rule must not fail a read."""
    assert GeoLocation(**{**BASE, "city": "old\nvalue"}).city == "old\nvalue"
