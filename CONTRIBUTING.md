# Contributing

## Before opening a pull request

- Keep changes focused and update the relevant documentation and tests.
- Run the Python checks from the repository root:

  ```sh
  uv run python scripts/check.py
  ```

- For changes under `web/`, also run:

  ```sh
  pnpm install --frozen-lockfile
  pnpm run ci
  ```

- Include the checks you ran and any remaining limitations in the pull request.

## Changes to routing and execution

Changes that affect routing, workflow interpretation, credentials, or execution
boundaries should include tests for both the intended result and the refusal
case. Unsupported workflow constructs must fail clearly rather than be skipped.
