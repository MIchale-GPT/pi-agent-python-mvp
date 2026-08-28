# Tau TUI ARM64 container delivery

## What was added

Tau now has a production image contract (`Dockerfile`), an ARM64 build/export
script, a long-running TUI container launcher, a non-secret environment template,
and deployment documentation. The build exports `dist/tau-arm64-tui.tar` plus a
checksum after running the CLI inside the ARM64 image.

## Why it exists

The data-query flow already joined SAG planning to read-only DWS execution, but
it had no offline artifact for the ARM64 intranet host. These files make the
existing CLI/TUI application deployable without adding a web server or changing
the reusable harness.

## Architecture mapping

The container packages `tau_coding` as one frontend over the existing
`tau_agent` and `tau_ai` layers. No Docker, networking, or Textual concern enters
the portable harness. The image still exposes the normal `tau` CLI entrypoint;
the launcher overrides it with `sleep infinity` only to provide a stable target
for `docker exec -it tau tau`.

The PRD proposed both host networking and a later connection to the SAG bridge.
Docker rejects that combination. The launcher keeps host networking so a DWS
bound to `127.0.0.1` remains reachable, resolves the SAG `api` container IP from
`sag_default`, and adds that IP as the container's `api` host entry. Native
Linux can route from the host namespace to its Docker bridge, so Tau reaches
`api:8000` without joining a second network or using the `/sag` proxy prefix.

Tau's home also needs two different access modes: credentials and provider
configuration are read-only, while sessions and diagnostic logs are writable.
The launcher binds `tau-runtime/.tau` read-write and overlays known configuration
files read-only.

## How to test

```bash
uv run pytest tests/test_docker_deployment.py
DRY_RUN=1 ./build-tau-arm64.sh
./build-tau-arm64.sh tau:arm64-tui-latest
sha256sum -c dist/tau-arm64-tui.tar.sha256
```

The detailed operator runbook is `docs/deployment/tau-tui-arm64.md`.
