# Publishing container images

The `publish-broker.yml` and `publish-panel.yml` workflows build images for
amd64 and arm64 and publish them to GitHub Container Registry. They run for
changes to their respective source paths on `main` and can also be started
manually.

## Running the broker publish outside Actions

`scripts/publish-image.sh` builds and publishes the broker image from a machine
with Docker Buildx and access to the repository and registry.

- `CINDRAL_REPO`: GitHub owner and repository name, in `owner/name` form.
- `GHCR_TOKEN`: registry credential with package write access.
- `GHCR_USER`: optional registry username; defaults to the local account name.
- `CINDRAL_PUBLISH_DIR`: optional clone directory, defaults to
  `/var/lib/cindral/publish`.
- `CINDRAL_BUILDX_CACHE`: optional Buildx cache directory.
- `CINDRAL_BUILDER`: optional Buildx builder name.

The script checks prerequisites before cloning and never writes the token to a
file.

```sh
CINDRAL_REPO=owner/name GHCR_TOKEN=... scripts/publish-image.sh
```

It clones `main` at depth 1, builds both supported architectures, and prints
the digest read back from the registry. A deployment can pin that digest.

## Image identity

Both image workflows publish a version-and-commit tag and a commit tag. The
version-and-commit tag identifies one build, while the digest is the immutable
deployment reference. The broker image reports its version, commit, and build
time.

The registry credential is configured in the repository's Actions secrets. A
public contribution can run the test workflow, while publishing is limited to
trusted pushes to `main` and manual runs by repository maintainers.
