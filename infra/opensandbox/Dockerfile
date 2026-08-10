# OpenSandbox injects execd when it creates a sandbox. Keep this image focused
# on the generated application's runtime and tooling; do not pre-install execd.
# This official Node 22 Debian image digest resolves to linux/arm64 on Apple
# Silicon Docker Desktop while remaining a multi-architecture image reference.
FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436

ARG PNPM_VERSION=10.17.1
ARG PI_VERSION=0.84.1
ARG OPENCODE_VERSION=1.18.16
ARG CODEX_VERSION=0.147.0

ENV PNPM_HOME=/opt/fomo/pnpm \
    npm_config_store_dir=/opt/fomo/pnpm/store \
    NPM_CONFIG_PREFIX=/opt/fomo/pi \
    COREPACK_HOME=/opt/fomo/corepack \
    COREPACK_DEFAULT_TO_LATEST=0 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/opt/fomo/pi/bin:/opt/fomo/pnpm:${PATH} \
    PI_OFFLINE=1 \
    PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0 \
    CI=true

USER root

# Git is needed for project versioning and curl for health checks. The image
# deliberately installs only the controlled Node/pnpm/Git tooling; compilers,
# SSH, and Python are omitted. Pi may run pnpm commands inside G, but release
# verification still only accepts dependencies already in the prefetched
# offline store. Playwright installs Chromium's exact runtime dependency set
# below.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        git \
        ripgrep \
    && node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (major < 22 || (major === 22 && minor < 19)) process.exit(1)' \
    && mkdir -p /workspace "${PNPM_HOME}" "${COREPACK_HOME}" /opt/fomo/bin /var/lib/fomo-pi /var/lib/fomo-opencode /var/lib/fomo-codex \
    && corepack enable \
    && corepack prepare "pnpm@${PNPM_VERSION}" --activate \
    && npm install --global --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm install --global "opencode-ai@${OPENCODE_VERSION}" "@opencode-ai/sdk@${OPENCODE_VERSION}" \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && /opt/fomo/pi/bin/pi --version \
    && /opt/fomo/pi/bin/opencode --version \
    && /opt/fomo/pi/bin/codex --version \
    && node -e 'import("/opt/fomo/pi/lib/node_modules/@opencode-ai/sdk/dist/v2/index.js").then(() => process.exit(0), () => process.exit(1))' \
    && pnpm dlx playwright@1.55.1 install --with-deps chromium \
    # Fixed toolchain directories are root-owned and read-only: PNPM_HOME
    # (contains the pnpm shim and the store parent), COREPACK_HOME, the
    # Playwright browser closure, the Pi CLI, and the bridge dir. The fixed
    # runner entries are pnpm-generated '#!/bin/sh' wrappers; FOMO invokes
    # their absolute root-owned paths with a PATH that excludes node-writable
    # directories. Only /workspace, /var/lib/fomo-pi, and the pnpm store
    # subtree are node-writable; the store is re-chowned/chmodded after the
    # parent dirs are locked so the parent stays non-writable.
    && chown -R root:root /opt/fomo/pi /opt/fomo/bin "${PNPM_HOME}" "${COREPACK_HOME}" /ms-playwright \
    && chmod -R a-w /opt/fomo/pi /opt/fomo/bin "${PNPM_HOME}" "${COREPACK_HOME}" /ms-playwright \
    && chown -R node:node /workspace /var/lib/fomo-pi /var/lib/fomo-opencode /var/lib/fomo-codex \
    && chmod 0700 /var/lib/fomo-pi /var/lib/fomo-opencode /var/lib/fomo-codex \
    && chown -R node:node "${npm_config_store_dir}" \
    && chmod -R u+rwX "${npm_config_store_dir}" \
    && rm -rf /var/lib/apt/lists/*

# This bridge is infrastructure, not part of the generated application or its
# dependency graph. Keep it root-owned and immutable while the sandbox runs as
# the unprivileged node user.
COPY --chown=root:root fomo-pi-rpc-bridge.mjs /opt/fomo/bin/fomo-pi-rpc-bridge.mjs
COPY --chown=root:root fomo-opencode-rpc-bridge.mjs /opt/fomo/bin/fomo-opencode-rpc-bridge.mjs
COPY --chown=root:root fomo-codex-rpc-bridge.mjs /opt/fomo/bin/fomo-codex-rpc-bridge.mjs
COPY --chown=root:root fomo-codex-models.json /opt/fomo/bin/fomo-codex-models.json
COPY --chown=root:root fomo-runtime-preflight.mjs /opt/fomo/bin/fomo-runtime-preflight.mjs
COPY --chown=root:root fomo-structured-output.ts /opt/fomo/bin/fomo-structured-output.ts
COPY --chown=root:root fomo-request-user-input.ts /opt/fomo/bin/fomo-request-user-input.ts
COPY --chown=root:root fomo-delegate-subtasks.ts /opt/fomo/bin/fomo-delegate-subtasks.ts
COPY --chown=root:root fomo-delegate-subtasks-core.mjs /opt/fomo/bin/fomo-delegate-subtasks-core.mjs
COPY --chown=root:root fomo-delegate-readonly-guard.ts /opt/fomo/bin/fomo-delegate-readonly-guard.ts
RUN chmod 0555 \
    /opt/fomo/bin/fomo-pi-rpc-bridge.mjs \
    /opt/fomo/bin/fomo-opencode-rpc-bridge.mjs \
    /opt/fomo/bin/fomo-codex-rpc-bridge.mjs \
    /opt/fomo/bin/fomo-codex-models.json \
    /opt/fomo/bin/fomo-runtime-preflight.mjs \
    /opt/fomo/bin/fomo-structured-output.ts \
    /opt/fomo/bin/fomo-request-user-input.ts \
    /opt/fomo/bin/fomo-delegate-subtasks.ts \
    /opt/fomo/bin/fomo-delegate-subtasks-core.mjs \
    /opt/fomo/bin/fomo-delegate-readonly-guard.ts

# npm_config_store_dir forces build-time prefetch and runtime installs to use
# the same controlled store. The staging install is COPIED (never moved) into
# the runtime cache: pnpm links installed files into the store, and a plain
# mv would leave the cache sharing hardlink inodes with the node-writable
# store. cp -a --reflink=never gives the cache independent inodes (internal
# relative symlinks and intra-tree hardlinks are preserved), so the cache is
# a self-contained, root-owned, read-only runner closure. Clean workspaces
# reuse this graph instead of rebuilding thousands of links per sandbox.
COPY --from=fomo-control-plane --chown=node:node \
    src/fomo/starter_assets/fomo-next-radix-v2/base/package.json \
    src/fomo/starter_assets/fomo-next-radix-v2/base/pnpm-lock.yaml \
    /opt/fomo/starter-prefetch/
RUN cd /opt/fomo/starter-prefetch \
    && pnpm fetch --frozen-lockfile --store-dir="${npm_config_store_dir}" \
    && pnpm install --offline --frozen-lockfile --store-dir="${npm_config_store_dir}" \
    && mkdir -p /opt/fomo/runtime-cache/fomo-next-radix-v2 \
    && cp -a --reflink=never node_modules /opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules \
    && cp package.json pnpm-lock.yaml /opt/fomo/runtime-cache/fomo-next-radix-v2/ \
    && rm -rf -- /opt/fomo/starter-prefetch \
    # The runtime cache is the FOMO-owned verification runner (tsc/next/
    # playwright at fixed absolute paths): root-owned and read-only so a
    # candidate can never tamper with it. The pnpm store stays node-writable
    # for offline installs.
    && chown -R root:root /opt/fomo/runtime-cache \
    && chmod -R a-w /opt/fomo/runtime-cache \
    # The store stays node-writable for offline installs; re-assert u+rwX so
    # pnpm-generated mode bits cannot leave the runtime store unreadable. The
    # PNPM_HOME parent remains root-owned a-w.
    && chown -R node:node "${npm_config_store_dir}" \
    && chmod -R u+rwX "${npm_config_store_dir}"

# `fomo-control-plane` is a named Compose build context. This seed is read
# only; OpenSandbox copies it into a newly-created writable workspace before
# fomo-pi-ds sees the project.
COPY --from=fomo-control-plane --chown=node:node src/fomo/starter_assets/fomo-next-radix-v2/base /opt/fomo/starters/fomo-next-radix-v2/base
COPY --from=fomo-control-plane --chown=node:node src/fomo/starter_assets/fomo-next-radix-v2/capabilities/crud /opt/fomo/starters/fomo-next-radix-v2/capabilities/crud
COPY --from=fomo-control-plane --chown=node:node src/fomo/starter_assets/fomo-next-radix-v2/capabilities/local-persistence /opt/fomo/starters/fomo-next-radix-v2/capabilities/local-persistence
RUN chown -R root:root /opt/fomo/starters/fomo-next-radix-v2 \
    && chmod -R a-w /opt/fomo/starters/fomo-next-radix-v2 \
    && ln -s /opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules \
        /opt/fomo/starters/fomo-next-radix-v2/base/node_modules

USER node
WORKDIR /workspace

# Generated Next.js applications serve their preview from this port. The
# OpenSandbox lifecycle service maps it separately from execd's control port.
EXPOSE 8080

# OpenSandbox supplies its own long-running sandbox entrypoint. This fallback
# only makes direct `docker run` inspection convenient.
CMD ["tail", "-f", "/dev/null"]
