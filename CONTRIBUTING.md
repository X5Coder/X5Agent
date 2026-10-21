# Contributing to X5Agent

Thank you for helping. Contributions are accepted only under the terms below.

## Before you start

- Read `Master Code Rules.md`, `AGENTS.md`, `PROJECT_MAP.md` and
  `X5_AGENT_PROTECTION_PLAN.md`.
- Open an issue at https://x5agent.com/community before a large change.
- Never include secrets, private keys, customer data or generated user data.
- Keep changes small, tested and inside the existing architecture.

## Contributor License Agreement (CLA)

By submitting a pull request, patch, issue text or any other contribution, you
agree that:

1. You have the right to submit the contribution.
2. You grant X5Coder a perpetual, worldwide, irrevocable, royalty-free,
   transferable and sublicensable license to use, reproduce, modify, publish,
   distribute, sublicense and relicense your contribution, including under the
   project's current and future license terms.
3. You waive any moral rights in the contribution to the extent permitted by
   applicable law.
4. Your contribution is provided "as is", without warranty of any kind.

Do not submit code you cannot license on these terms.

## Protected code

Do not remove, translate, shorten, reorder or bypass the X5Agent protected-code
notice or any license, update, repair, dispatch, gateway or tool-execution
guard. A pull request that does so will be closed.

Enable the local protection hook once per clone:

```sh
git config core.hooksPath .githooks
```

The hook runs the signed-policy/manifest check and the notice check. A
maintainer re-signs protected changes with
`python project/scripts/agent_build.py --write` before committing.

## Pull request checklist

- The change has a single clear purpose.
- Tests cover the new behavior and the failure path.
- Existing tests still pass.
- No secrets, keys or user data are included.
- `PROJECT_MAP.md` is updated when files or folders change.
- The license, copyright and protected notices are intact.

## Security reports

Do not open a public issue for a vulnerability. Follow `SECURITY.md`.
