# Upstream Tracking

Upstream tracking is owned by `aio-fleet`, not by app-local scripts. This repo declares upstream metadata in `.aio-fleet.yml`; the central `aio-fleet/fleet.yml` remains the source for generated manifests and control-plane policy.

## Required Inputs

- Upstream name and source repository: `plastic-labs/honcho`.
- Dockerfile ARGs that pin the upstream version: `HONCHO_VERSION` and `HONCHO_GIT_SHA`.
- Update strategy: `pr` for reviewed tag bumps (`stable_only`).

## Honcho-Specific Note

Honcho is source-built. The Dockerfile clones `plastic-labs/honcho` and checks out `HONCHO_GIT_SHA`. The fleet release label is `HONCHO_VERSION` (currently `v3.0.12`). When the monitor bumps the tag, move the commit SHA with it.

## Validation

Run this from `aio-fleet` after changing upstream metadata or Dockerfile pins:

```bash
uv run aio-fleet validate-repo --repo honcho-aio --repo-path ../honcho-aio
uv run aio-fleet release status --repo honcho-aio
uv run aio-fleet upstream monitor --repo honcho-aio --repo-path ../honcho-aio --dry-run
```
