# Security Policy

## Supported Versions

Security updates are prioritized for the latest active code line.

| Version | Supported |
| --- | --- |
| Latest `main` | Yes |
| Latest tagged release | Yes |
| Older release candidates | No |

## Reporting a Vulnerability

Please do not open a public GitHub issue for vulnerabilities.

Send a private report to: dev@orca.id

Include:

- Affected component and version
- Steps to reproduce
- Proof of concept or logs (if available)
- Impact assessment (what can be exploited)
- Suggested mitigation (optional)

## Response Targets

- Acknowledgement target: within 3 business days
- Initial triage target: within 7 business days
- Remediation timeline: depends on severity and operational impact

## Disclosure Process

1. Private report is received and acknowledged.
2. Team validates and triages severity.
3. Patch is developed and tested.
4. Fix is released.
5. Coordinated public disclosure is published after patch availability.

## Security Best Practices for Contributors

- Never commit secrets, tokens, certs, or private keys.
- Use placeholders in docs and examples.
- Avoid logging secrets or credentials in plain text.
- Prefer secure defaults and least privilege access.
