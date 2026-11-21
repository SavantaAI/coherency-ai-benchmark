# T3 Code plus the Pi coding agent, and no other harness.
ARG BASE_IMAGE=t3-harness-base:dev
FROM ${BASE_IMAGE}

ARG PI_VERSION=1.6.0

USER root
RUN npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm cache clean --force \
 && rm -rf /root/.npm
COPY pi/settings.json /etc/t3-settings.json
USER node
# Pi's own network activity: no version check, no install telemetry, no
# model catalog refresh. Model calls are unaffected.
ENV PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0 \
    PI_OFFLINE=1
RUN pi --version
