# Release Checklist

## Before a public image bump

- confirm `honcho-aio.xml` `Support`, `Project`, `TemplateURL`, and `Icon` URLs
- confirm `HONCHO_VERSION` and `HONCHO_GIT_SHA` in `Dockerfile`
- confirm `aio-fleet/fleet.yml` and `.aio-fleet.yml` match
- from `aio-fleet`, run `uv run aio-fleet validate-repo --repo honcho-aio --repo-path ../honcho-aio`
- run `pytest tests/integration -m integration`

## Before enabling a publish

- confirm the AIO Fleet GitHub App is installed on `honcho-aio` and `awesome-unraid`
- confirm `aio-fleet / required` passed for the target SHA
- confirm Docker Hub `dub19/honcho-aio` is public

## Before Unraid install (you do this in Community Apps)

- install from the catalog XML
- set `POSTGRES_PASSWORD` and model fields
- verify `/health` and first boot
- confirm `awesome-unraid` has `honcho-aio.xml` and `icons/honcho.png`
- do not delete `/docker-compose/honcho` unless you separately decide to
