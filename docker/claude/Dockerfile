# T3 Code plus Claude Code, and no other harness.
# T3's Claude provider drives the `claude` executable through the Claude Agent
# SDK bundled in T3, so the CLI itself must be installed.
ARG BASE_IMAGE=t3-harness-base:dev
FROM ${BASE_IMAGE}

ARG CLAUDE_CODE_VERSION=2.1.330

USER root
# The package's postinstall links the platform binary from its optional
# dependency, so scripts are allowed here.
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && rm -rf /root/.npm
COPY claude/settings.json /etc/t3-settings.json
USER node
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
RUN claude --version
