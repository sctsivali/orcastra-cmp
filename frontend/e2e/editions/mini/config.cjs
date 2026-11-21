// Mini's part of the browser suite config (see e2e/support/edition.cjs). Every CMP spec runs on the
// Mini build except the tests listed here, each under the CMP feature Mini does not ship. A title
// that changes in CMP makes its test run, and fail, on Mini: update the list, never widen it.

const NOT_IN_MINI = [
  {
    reason: "Settings > User Groups: roles come from Mini's operators, not identity-provider groups. Its deep link answers 404 like any unknown tab.",
    file: "settings-navigation-cache.spec.cjs",
    titles: [
      "admin mounted user-groups retires when the same actor is downgraded to Tenant",
      "partner mounted user-groups retires when the same actor is downgraded to Tenant",
      "admin settings tabs have reload-safe URLs and browser history",
      "partner settings tabs have reload-safe URLs and browser history",
      "tenant direct /settings/user-groups returns to General without privileged reads",
    ],
  },
  {
    reason: "Settings > User Groups and the synced organizations section of Integrations (no identity provider, no OrcaHub federation).",
    file: "settings-sections.spec.cjs",
    titles: [
      "admin integrations sections deep-link, switch and walk history",
      "partner integrations sections deep-link, switch and walk history",
      "an unknown or hostile section falls back to the default and is not reflected",
      "a section keeps its search when the operator switches away and back",
      "Back closes a dialog that the hidden section opened",
      "an administrator can assign a role in a local organization",
      "a partner is offered only the tenant role",
      "a refused removal stays in the dialog and says why",
      "tenant deep link with ?section=policies returns to General without privileged reads",
    ],
  },
  {
    reason: "Synced organizations and their owner keys (OrcaHub federation is hidden in Mini).",
    file: "integration-key-ownership.spec.cjs",
    titles: [
      "an admin sees what each key owns and moves a frozen organization",
      "a partner reads ownership and cannot transfer",
      "an admin manages an organization's owner keys",
    ],
  },
  {
    reason: "Signing in again through the identity provider; Mini signs in with the browser's certificate.",
    file: "terminal-session-recovery.spec.cjs",
    titles: [
      "a deep link whose session the IdP ended signs in again and comes back to it",
      "a second automatic sign-in within a minute waits for the operator",
    ],
  },
  {
    reason: "Walks the synced organizations and User Groups empty states, both hidden in Mini.",
    file: "card-illustrations.spec.cjs",
    titles: ["partner: first-run empty states in Settings draw their scene above the text"],
  },
];

const escape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// Playwright matches "<project> <file> <describe> <title> <tags>": anchor the file and the title.
const grepInvert = NOT_IN_MINI.map(
  ({ file, titles }) =>
    new RegExp(`(?:^|[ /])${escape(file)} (?:.* )?(?:${titles.map(escape).join("|")})(?: @\\S+)*$`),
);

module.exports = { testIgnore: [], grepInvert, NOT_IN_MINI };
