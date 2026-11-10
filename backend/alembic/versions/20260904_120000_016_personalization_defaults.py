"""Move rows still on the previous personalization defaults onto the current ones.

The defaults are a violet sidebar (`#321f6f`) and accent `#8467e4`. The API merges a
stored `colors` object over the defaults, so a saved copy of the previous default would
outrank the current one. Only rows still on that default move:

  sidebar   `useCustomSidebarBg` false or absent AND `sidebarBgColor` '#ffffff' or
            absent. A row with the toggle on and a white colour asked for white.
  accent    `accentColor` exactly '#8b5cf6' or absent. Not keyed on `useCustomAccent`,
            which tracks only the freeform hex field: a preset can carry the flag false.

A row with a light-mode logo and no dark-mode logo keeps its white sidebar: on a dark
ground `useCurrentLogo` would swap the logo for the stock `/orca-logo-white.png`.
Data only; re-running is a no-op because the predicate stops matching.
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

# Moved even off a non-default value: this row served as the branding of an organization
# that had no preferences row of its own.
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
    """text() bound with only the parameters the statement names."""
    stmt = sa.text(sql)
    # Read the names the compiled statement found instead of substring-matching the SQL:
    # SQLAlchemy skips `:name` when a ':' follows it (a PostgreSQL cast), so a
    # `":" + name in sql` test disagrees with the parser.
    defined = set(stmt._bindparams)
    present = {k: v for k, v in params.items() if k in defined}
    return stmt.bindparams(**present) if present else stmt


def _move_sidebar(table: str, *, bg: str, text: str, use_custom: str, extra_where: str = "") -> None:
    # jsonb_set creates a missing key, so a colours object without one of these fields is
    # filled in. sidebarTextColor changes only where the operator has not pinned it
    # (useCustomSidebarText); writing it keeps the stored row consistent for any reader
    # that does not derive the text colour.
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
    # Bind only the placeholders this WHERE spells: text() rejects a bindparam it cannot
    # find, and the forced-reset predicate below names no :old_bg.
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

    # Only in user_preferences: organization_preferences has no email column.
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
    # Mirrors the predicate against the new values. A row that later chose the same colours
    # by hand cannot be told apart, so a downgrade restores the previous default, not a
    # per-row history.
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
