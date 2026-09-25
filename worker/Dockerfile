FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.157.0
RUN apt-get update \
    && apt-get install --yes --no-install-recommends bubblewrap ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && useradd --create-home --uid 10001 codex \
    && mkdir -p /workspace /run/codex /home/codex/.codex \
    && chown -R codex:codex /workspace /run/codex /home/codex/.codex

COPY --chown=codex:codex worker/entrypoint.sh /usr/local/bin/codex-worker
RUN chmod 0555 /usr/local/bin/codex-worker

USER codex
ENV HOME=/home/codex CODEX_HOME=/home/codex/.codex
WORKDIR /workspace
EXPOSE 4500
ENTRYPOINT ["/usr/local/bin/codex-worker"]
