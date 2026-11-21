# Shared base: Node, git, and T3 Code. No agent harness is installed here.
# Each harness image (docker/pi, docker/claude) adds exactly one harness.
#
# Everything the agent under test can read inside the container uses neutral
# names: user `node`, workspace `/work/project`, T3 base dir `~/.t3`.

ARG NODE_IMAGE=node:24.22.3-bookworm-slim
FROM ${NODE_IMAGE}

ARG T3_VERSION=0.1.12-nightly.20261121.5051

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git procps \
 && rm -rf /var/lib/apt/lists/*

# The npm package is a small launcher. The first run downloads the
# self-contained runtime for this platform from the GitHub release, checks it
# against the release's SHA256SUMS, and stores it under ~/.t3/runtime. Running
# `t3 --version` here bakes that runtime into the image so containers start
# without network access to GitHub.
RUN npm install -g --ignore-scripts "t3@${T3_VERSION}" \
 && npm cache clean --force \
 && rm -rf /root/.npm

COPY --chmod=0755 base/entrypoint.sh /usr/local/bin/workspace-entrypoint

RUN mkdir -p /work/project && chown -R node:node /work

USER node
ENV HOME=/home/node \
    T3_BASE_DIR=/home/node/.t3 \
    T3_PORT=3773 \
    T3CODE_TELEMETRY_ENABLED=false \
    T3CODE_NO_BROWSER=true

RUN t3 --version \
 && git config --global user.name "dev" \
 && git config --global user.email "dev@localhost" \
 && git config --global init.defaultBranch main \
 && cd /work/project \
 && git init -q \
 && printf '# project\n' > README.md \
 && git add README.md \
 && git commit -qm "initial commit"

WORKDIR /work/project
ENTRYPOINT ["/usr/local/bin/workspace-entrypoint"]
