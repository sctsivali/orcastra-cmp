"""Every manual decision the catalogue build makes, with the reason and the evidence.

Nothing outside this file may special-case a region. A reviewer reads this one file to know
where the build departs from its sources.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# Kemendagri code -> geoBoundaries ADM2 shapeName, where the two disagree on the name. The
# geoBoundaries release is the 2020 BPS boundary set, older than several renames.
GEOBOUNDARIES_NAME: Dict[str, Tuple[str, str]] = {
    "12.12": ("Toba Samosir", "renamed Kabupaten Toba in 2020"),
    "16.02": ("Ogan Komering Ilir", "same regency; the Kemendagri source row is truncated (see NAME_FIXES)"),
    "64.11": ("Mahakam Hulu", "spelling variant of Mahakam Ulu"),
    "71.09": ("Siau Tagulandang Biaro", "the Kemendagri row abbreviates Kepulauan as Kep."),
    "76.01": ("Mamuju Utara", "renamed Kabupaten Pasangkayu in 2017"),
    "81.03": ("Maluku Tenggara Barat", "renamed Kabupaten Kepulauan Tanimbar in 2019"),
}

# Kemendagri code -> (corrected official name, evidence). Applied before anything else reads
# the name, so search, labels and the legacy mapping all see the corrected form.
NAME_FIXES: Dict[str, Tuple[str, str]] = {
    "16.02": (
        "Kabupaten Ogan Komering Ilir",
        "source row reads 'Kabupaten Ogan Komering'; Wikidata (P2588 16.02) and geoBoundaries both name it Ogan Komering Ilir",
    ),
    "71.09": (
        "Kabupaten Kepulauan Siau Tagulandang Biaro",
        "source row abbreviates Kepulauan as 'Kep.'; expanded to match every other Kepulauan regency",
    ),
}

# Kemendagri code -> (seat name, evidence), for rows whose seat column is empty.
SEAT_FIXES: Dict[str, Tuple[str, str]] = {
    "81.03": ("Saumlaki", "seat column empty in the source; Wikidata P36 for 81.03 is Saumlaki"),
}

# Kemendagri code -> (other names the seat town goes by, evidence). Added to the search keys
# for GeoNames and OpenStreetMap; the result still has to clear the corroboration rule.
SEAT_ALIASES: Dict[str, Tuple[List[str], str]] = {
    "94.06": (
        ["Moanemani"],
        "Kigamani is the statutory seat; the government centre is Moanemani, whose OpenStreetMap town "
        "node lies 0.4 km from the Kepmendagri coordinate (GeoNames PPLA2 'Kigamani' is 11.5 km off)",
    ),
}

# Kemendagri code -> (lat, lng, evidence). Only for a seat no automatic rule could place and
# corroborate. The build still requires the override to sit inside the regency's polygon.
COORDINATE_OVERRIDES: Dict[str, Tuple[float, float, str]] = {
    "73.14": (
        -3.9331, 119.7693,
        "Pangkajene Sidenreng has no GeoNames or OpenStreetMap settlement node. The Kepmendagri point is "
        "used; Wikidata repeats the same value (one origin), and OpenStreetMap water works named "
        "'BNA Pangkajene' lie 2.1 to 2.7 km from it",
    ),
}

# Countries left out of the picker, with the reason. Indonesia is listed per regency instead.
EXCLUDED_COUNTRIES: Dict[str, str] = {
    "ID": "listed per kabupaten and kota instead of as one country entry",
    "AN": "Netherlands Antilles, dissolved in 2010 (GeoNames keeps the historical row)",
    "CS": "Serbia and Montenegro, dissolved in 2006 (GeoNames keeps the historical row)",
    "EH": "disputed territory with no agreed capital: GeoNames gives Laayoune, Natural Earth gives "
          "Bir Lehlou, and Wikidata records none",
}

# ISO code -> (capital to place the country at, evidence), where the GeoNames capital cannot
# be verified against the country's own outline.
COUNTRY_CAPITAL_OVERRIDES: Dict[str, Tuple[str, str]] = {
    "PS": (
        "Ramallah",
        "GeoNames gives East Jerusalem, which Natural Earth draws outside the PS map unit. Ramallah is "
        "the seat of the Palestinian Authority government and one of the current capitals Wikidata records",
    ),
}

# Legacy entries that are not a kabupaten or kota, kept resolvable but hidden from the picker.
# Each is placed at a GeoNames populated place and checked against Wikidata like any other.
LEGACY_ONLY: Dict[str, Dict[str, str]] = {
    "id-jkt": {
        "name": "Jakarta",
        "city": "Jakarta",
        "province_code": "31",
        "geonames_id": "1642911",
        "wikidata": "Q3630",
        "reason": "province-wide Jakarta entry; superseded by the five kota administrasi and Kepulauan Seribu",
    },
    "id-ikn": {
        "name": "Ibu Kota Nusantara",
        "city": "Nusantara",
        "province_code": "",
        "geonames_id": "13680164",
        "wikidata": "Q110601629",
        "reason": "Otorita IKN is a special capital region, not a kabupaten or kota in the Kepmendagri list",
    },
}
