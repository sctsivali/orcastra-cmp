"""Both layers must give the same answer for an image whose type LXD did not report.

`ImageInfo.type` carries a default so that one malformed image cannot 500 the whole list, and
`seedTypeForImage` in the frontend carries a fallback so a row action can still seed the create
form. They answered the same question in opposite directions for as long as both existed: the
schema said `virtual-machine`, `prefill.ts` said `container`.

Nothing broke, because neither branch can currently fire. `services/lxd/images.py:103` always
passes the daemon's own value and the frontend declares `type` as a required string. That is
exactly what made it worth pinning: two dead branches stating contradictory beliefs, with
nothing to tell the next person editing one that the other exists.

The vocabularies differ on purpose. LXD says `container` / `virtual-machine`; the create form's
own enum is `container` / `vm`. This maps between them rather than demanding they be spelled the
same, because the form's enum is what ends up in a URL.
"""

import pathlib
import re

from app.models.schemas import ImageInfo

PREFILL = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "components"
    / "instance-create"
    / "prefill.ts"
)

# The form's two-value enum, keyed by the LXD type it stands for.
_SEED_FOR_LXD_TYPE = {"container": "container", "virtual-machine": "vm"}


def _frontend_fallback() -> str:
    source = PREFILL.read_text()
    match = re.search(
        r'export const SEED_TYPE_FALLBACK:\s*SeedInstanceType\s*=\s*"([a-z-]+)"', source
    )
    assert match, (
        f"could not find SEED_TYPE_FALLBACK in {PREFILL.name}. If it was renamed or inlined, "
        "this guard is now blind and the two layers can drift apart again silently."
    )
    return match.group(1)


def test_the_frontend_fallback_is_a_type_the_form_can_actually_hold():
    assert _frontend_fallback() in set(_SEED_FOR_LXD_TYPE.values())


def test_the_image_type_fallback_agrees_across_layers():
    schema_default = ImageInfo.model_fields["type"].default
    assert schema_default in _SEED_FOR_LXD_TYPE, (
        f"ImageInfo.type defaults to {schema_default!r}, which is not an LXD image type. "
        "LXD reports 'container' or 'virtual-machine'."
    )

    expected_seed = _SEED_FOR_LXD_TYPE[schema_default]
    assert _frontend_fallback() == expected_seed, (
        f"ImageInfo.type falls back to {schema_default!r} (so the create form should seed "
        f"{expected_seed!r}), but prefill.ts falls back to {_frontend_fallback()!r}. One side "
        "was changed without the other, which is how these two came to hold opposite beliefs "
        "about the same unknown in the first place."
    )
