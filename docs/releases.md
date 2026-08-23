# Releases

This repo uses wrapper tags such as `v3.0.12-aio.1`, aligned to the pinned Honcho version.

## Release Model

- App repos publish from `main` through the central `aio-fleet` control plane after required validation passes.
- Formal changelog entries and GitHub Releases are release-driven, not automatic for every merge.
- The XML `<Changes>` block is generated from `CHANGELOG.md` during release preparation. Do not edit it manually.

## Tag Scheme

Every normal `main` publish emits Docker Hub and GHCR tags for:

- `latest`
- `sha-<commit>`

Formal release publishes add the changelog release tag, such as `v3.0.12-aio.1`. Image: `dub19/honcho-aio` (GHCR: `ghcr.io/wgross19/honcho-aio`).

## Release Commands

Run these from the `aio-fleet` checkout:

```bash
uv run aio-fleet release status --repo honcho-aio
uv run aio-fleet release prepare --repo honcho-aio --dry-run
uv run aio-fleet release publish --repo honcho-aio --dry-run
uv run aio-fleet registry verify --repo honcho-aio --sha <commit-sha> --dry-run --verbose
```

## Upstream Tracking

The Dockerfile pins official `plastic-labs/honcho` via `HONCHO_VERSION` and `HONCHO_GIT_SHA`. Upstream bumps are initiated centrally with `aio-fleet upstream monitor`, which opens a PR for human review. Never auto-merge an upstream update without reviewing database, auth, and embedding-schema effects.
