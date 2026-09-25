# Package manager policy

Runner Relay selects the execution lane. It does not impose a package manager on repositories.

## Default

Use `pnpm` for new and maintained product/web repositories.

Current local examples:

| Repository | Package manager | Evidence |
| --- | --- | --- |
| `example-app` | pnpm | `packageManager`, `pnpm-lock.yaml`, `preinstall: only-allow pnpm` |
| `example-web` | pnpm | `packageManager`, `pnpm-lock.yaml` |
| `example-store` | pnpm | `packageManager`, `pnpm-lock.yaml` |
| `opencode-telegram-bot` | npm | `package-lock.json`, repository instructions |

`opencode-telegram-bot` is an intentional repository-level exception. Keep its npm workflow until the repository explicitly migrates its lockfile and instructions.

## Detection order

For each repository, use the first available source of truth:

1. Repository agent instructions.
2. `packageManager` in `package.json`.
3. The committed lockfile.
4. The existing CI workflow.

Never generate or switch a lockfile as part of Runner Relay integration.

## CI setup

For pnpm repositories, pin the repository's declared pnpm version and use a frozen install:

```yaml
- uses: pnpm/action-setup@v4
  with:
    version: <declared pnpm version>
- uses: actions/setup-node@v4
  with:
    node-version-file: .nvmrc
    cache: pnpm
- run: pnpm install --frozen-lockfile
```

For npm repositories:

```yaml
- uses: actions/setup-node@v4
  with:
    node-version-file: .nvmrc
    cache: npm
- run: npm ci
```

The repository's own scripts remain authoritative:

```sh
pnpm lint
pnpm test
pnpm build
```

or:

```sh
npm run lint
npm test
npm run build
```

## Migration rule

A package-manager migration is a repository change. It requires:

- updating the repository instructions,
- replacing the lockfile intentionally,
- verifying the CI workflow,
- running the full repository checks,
- and reviewing the dependency and script changes separately from Runner Relay integration.

Do not migrate a working repository to npm only to make the fleet uniform. Consistency at the routing boundary is more valuable than forcing every repository to use the same package manager.
