"""Move rows still on the old personalization defaults onto the new ones.

The product's default sidebar changed from "follow the theme, painting bg-card" to a
violet ground, `#321f6f`, and the accent moved from `#8b5cf6` to `#8467e4` in the same
hue family. Changing the code defaults alone would have reached almost nobody: every
account and organization that has ever opened Settings carries a stored `colors` object,
and the API merges the stored object over the defaults, so a saved copy of the OLD
default wins over the new one forever.

This migration moves those saved copies, and nothing else. The predicate is deliberately
narrow, because "still on the shipped default" and "chose a colour that happens to look
like the default" are the same bytes, and only the first should move:

  sidebar   `useCustomSidebarBg` is false or absent AND `sidebarBgColor` is '#ffffff'
            or absent. Both conditions, because a row that turned the toggle ON while
            leaving the colour white asked for a white sidebar.
  accent    `accentColor` is exactly '#8b5cf6' or absent. NOT keyed on
            `useCustomAccent`, which turns out to track only the freeform hex field:
            rows exist that picked the Indigo preset with the flag still false, and
            keying on the flag would have overwritten a deliberate choice.

Two carve-outs.

`logo` first. `useCurrentLogo` in the frontend swaps to `logo.darkModeLogo` once the
sidebar's relative luminance falls below 0.5, and `#321f6f` measures 0.028. A row that
uploaded only a light-mode mark would therefore be flipped onto a dark ground and fall
through to the stock `/orca-logo-white.png`, replacing a customer's own logo with ours.
Those rows keep their white sidebar until somebody chooses otherwise in the UI.

`ops@sivali.co.id` second, in the opposite direction: it carries a deliberate `#2c3539`
and is moved anyway, by explicit request of the account owner. It is the first partner
of an organization that has no `organization_preferences` row, and
`services/organization_preferences.py` hands such an organization its first partner's
PERSONAL settings, so that one row was silently acting as the branding for every tenant
in the org.

Data only. No schema change, no lock of consequence, and re-running it is a no-op
because the predicate stops matching after the first pass.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "016_personalization_defaults"
down_revision = "015_session_audit_bigint"
branch_labels = None
depends_on = None

OLD_ACCENT = "#8b5cf6"
NEW_ACCENT = "#8467e4"
OLD_SIDEBAR_BG = "#ffffff"
NEW_SIDEBAR_BG = "#321f6f"
OLD_SIDEBAR_TEXT = "#1e293b"
NEW_SIDEBAR_TEXT = "#f8fafc"

TABLES = ("user_preferences", "organization_preferences")

# Reset by explicit owner instruction. See the module docstring.
FORCED_RESET_EMAILS = ("ops@sivali.co.id",)

# A row that has a light-mode logo and no dark-mode logo would lose its mark to the stock
# one the moment the ground goes dark, so it is excluded from the sidebar half.
_LOGO_SAFE = """
  AND NOT (
        settings #>> '{logo,lightModeLogo}' IS NOT NULL
    AND settings #>> '{logo,darkModeLogo}'  IS NULL
  )
"""

_SIDEBAR_IS_OLD_DEFAULT = """
      COALESCE(settings #>> '{colors,useCustomSidebarBg}', 'false') = 'false'
  AND COALESCE(settings #>> '{colors,sidebarBgColor}', :old_bg) = :old_bg
"""

_ACCENT_IS_OLD_DEFAULT = """
  COALESCE(settings #>> '{colors,accentColor}', :old_accent) = :old_accent
"""


def _table_exists(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _bind(sql: str, **params) -> sa.TextClause:
    """text() with only the parameters the statement actually names."""
    stmt = sa.text(sql)
    # important: ask the compiled statement which names it actually found, rather than
    # substring-matching the SQL. SQLAlchemy skips `:name` when a ':' follows it, so that
    # a PostgreSQL cast keeps working, and a naive `":" + name in sql` test disagrees with
    # the parser on exactly the shape that caused this helper to raise.
    defined = set(stmt._bindparams)
    present = {k: v for k, v in params.items() if k in defined}
    return stmt.bindparams(**present) if present else stmt


def _move_sidebar(table: str, *, bg: str, text: str, use_custom: str, extra_where: str = "") -> None:
    # jsonb_set creates the key when it is missing, so a row whose colours object predates
    # one of these fields is filled in rather than skipped. sidebarTextColor is only touched
    # when the operator has not pinned it, because pinning it is the one way to say "this
    # exact text colour"; autoTextColor rows get the right value from the provider anyway,
    # and writing it keeps the stored row honest instead of carrying dark text on a dark
    # ground for anything that reads the row without deriving.
    sql = f"""
            UPDATE {table}
               SET settings = jsonb_set(
                     jsonb_set(
                       jsonb_set(settings, '{{colors,useCustomSidebarBg}}', CAST(:use_custom AS jsonb), true),
                       '{{colors,sidebarBgColor}}', to_jsonb(CAST(:bg AS text)), true
                     ),
                     '{{colors,sidebarTextColor}}',
                     CASE
                       WHEN COALESCE(settings #>> '{{colors,useCustomSidebarText}}', 'false') = 'false'
                         THEN to_jsonb(CAST(:text AS text))
                       ELSE settings -> 'colors' -> 'sidebarTextColor'
                     END,
                     true
                   ),
                   updated_at = now()
             WHERE jsonb_exists(settings, 'colors')
               AND ({extra_where})
            """
    # important: bind only the placeholders this particular WHERE actually spells. text()
    # rejects a bindparam it cannot find in the string, and the forced-reset call below uses a
    # predicate that names no :old_bg at all.
    op.execute(_bind(sql, bg=bg, text=text, use_custom=use_custom, old_bg=OLD_SIDEBAR_BG))


def _move_accent(table: str, *, accent: str, extra_where: str) -> None:
    sql = f"""
            UPDATE {table}
               SET settings = jsonb_set(settings, '{{colors,accentColor}}', to_jsonb(CAST(:accent AS text)), true),
                   updated_at = now()
             WHERE jsonb_exists(settings, 'colors')
               AND ({extra_where})
            """
    op.execute(_bind(sql, accent=accent, old_accent=OLD_ACCENT))


def upgrade() -> None:
    for table in TABLES:
        if not _table_exists(table):
            continue
        _move_sidebar(
            table,
            bg=NEW_SIDEBAR_BG,
            text=NEW_SIDEBAR_TEXT,
            use_custom="true",
            extra_where=_SIDEBAR_IS_OLD_DEFAULT + _LOGO_SAFE,
        )
        _move_accent(table, accent=NEW_ACCENT, extra_where=_ACCENT_IS_OLD_DEFAULT)

    # The one row moved off a deliberate value, and only in user_preferences: the address
    # column does not exist on the organization table.
    if _table_exists("user_preferences") and FORCED_RESET_EMAILS:
        emails = ", ".join(f"'{e}'" for e in FORCED_RESET_EMAILS)
        _move_sidebar(
            "user_preferences",
            bg=NEW_SIDEBAR_BG,
            text=NEW_SIDEBAR_TEXT,
            use_custom="true",
            extra_where=f"user_email IN ({emails})" + _LOGO_SAFE,
        )
        _move_accent("user_preferences", accent=NEW_ACCENT, extra_where=f"user_email IN ({emails})")


def downgrade() -> None:
    # Mirrors the predicate against the new values. It cannot distinguish a row this
    # migration moved from one that later chose the same colours by hand, which is the
    # same limitation the upgrade has in the other direction and is stated rather than
    # hidden: a downgrade restores the previous DEFAULT, not a per-row history.
    for table in TABLES:
        if not _table_exists(table):
            continue
        op.execute(
            sa.text(
                f"""
                UPDATE {table}
                   SET settings = jsonb_set(
                         jsonb_set(
                           jsonb_set(settings, '{{colors,useCustomSidebarBg}}', CAST('false' AS jsonb), true),
                           '{{colors,sidebarBgColor}}', to_jsonb(CAST(:old_bg AS text)), true
                         ),
                         '{{colors,sidebarTextColor}}',
                         CASE
                           WHEN COALESCE(settings #>> '{{colors,useCustomSidebarText}}', 'false') = 'false'
                             THEN to_jsonb(CAST(:old_text AS text))
                           ELSE settings -> 'colors' -> 'sidebarTextColor'
                         END,
                         true
                       ),
                       updated_at = now()
                 WHERE jsonb_exists(settings, 'colors')
                   AND COALESCE(settings #>> '{{colors,useCustomSidebarBg}}', 'false') = 'true'
                   AND settings #>> '{{colors,sidebarBgColor}}' = :new_bg
                """
            ).bindparams(old_bg=OLD_SIDEBAR_BG, old_text=OLD_SIDEBAR_TEXT, new_bg=NEW_SIDEBAR_BG)
        )
        op.execute(
            sa.text(
                f"""
                UPDATE {table}
                   SET settings = jsonb_set(settings, '{{colors,accentColor}}', to_jsonb(CAST(:old_accent AS text)), true),
                       updated_at = now()
                 WHERE jsonb_exists(settings, 'colors')
                   AND settings #>> '{{colors,accentColor}}' = :new_accent
                """
            ).bindparams(old_accent=OLD_ACCENT, new_accent=NEW_ACCENT)
        )
