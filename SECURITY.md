# Security Policy

## Supported versions

Security fixes are provided for the latest released version on the `main`
branch. Older versions may receive a fix only when the issue is critical and
the upgrade path is unsafe.

## Reporting a vulnerability

Do **not** open a public issue for a security problem.

Use GitHub's private security advisory:

https://github.com/X5Coder/X5Agent/security/advisories/new

Include:

- A short description of the issue and its impact.
- Steps to reproduce, or a minimal proof of concept.
- Affected version and platform.
- Any suggested fix or mitigation.

Do not include real credentials, private keys, customer data or production
secrets in the report. Use redacted examples.

## What to expect

- We aim to acknowledge a report within 7 days.
- We will confirm the issue, prepare a fix and coordinate disclosure.
- Please do not publish details before a fixed release is available.

## Out of scope

- Issues that require a modified local build with protection disabled.
- Reports that only show a missing security header without a demonstrated
  impact.
- Social engineering, spam or denial-of-service against hosted services.
- Vulnerabilities in third-party dependencies without a project-specific
  exploit path (report those upstream as well).
