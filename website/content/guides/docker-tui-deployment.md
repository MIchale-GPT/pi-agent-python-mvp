---
title: "Deploy the Tau TUI with Docker"
description: "Build and deploy Tau's TUI and print mode as an offline ARM64 image."
---

Tau can be delivered as a single `linux/arm64` Docker image for terminal-only
environments. The image contains the Textual TUI, print mode, and the optional
data-query dependencies; it does not contain Tau Web and does not publish a
port.

Build and verify the offline artifact:

```bash
DRY_RUN=1 ./build-tau-arm64.sh
./build-tau-arm64.sh tau:arm64-tui-latest
sha256sum -c dist/tau-arm64-tui.tar.sha256
```

On the ARM64 host, load it and start the persistent exec target:

```bash
docker load --input dist/tau-arm64-tui.tar
cp tau.env.production.example tau.env.production
# Fill in the production allowlist, SAG ids, provider settings, and secrets.
./tau-start.sh
docker exec -it tau tau
```

The default launcher joins `sag_default`, so Agent requests use
`http://api:8000` without the host Nginx `/sag` prefix. It adds
`host.docker.internal` for a DWS port published by the host and publishes no Tau
ports. Docker does not allow a host-networked container to join this SAG bridge,
so do not combine `--network host` with a later `docker network connect`.

Configuration files and credentials under `~/.tau` are mounted individually as
read-only files. Sessions and logs remain writable under `tau-runtime/.tau`.
Keep `TAU_DATA_AUTO_APPROVE_EXECUTE=0` for interactive production use and set an
explicit DBA-approved `TAU_DWS_ALLOWED_OBJECTS` allowlist.

See the repository runbook at `docs/deployment/tau-tui-arm64.md` for transfer,
verification, rollback, and troubleshooting steps.
