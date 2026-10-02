FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl python3 python3-venv git && rm -rf /var/lib/apt/lists/*
# Standalone Claude Code build; version and checksum are pinned like the Antigravity image.
ARG CLAUDE_VERSION=2.1.287
ARG CLAUDE_SHA256=3920489a5109cff5786a1a392c25277408ff22bc796d5edb9c16a60e5a1718f0
ADD https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-x64/claude /usr/local/bin/claude
RUN echo "${CLAUDE_SHA256}  /usr/local/bin/claude" | sha256sum -c - \
    && chmod 755 /usr/local/bin/claude \
    && groupadd -g 10001 claude && useradd -m -u 10001 -g claude claude \
    && mkdir -p /workspace /home/claude/.claude \
    && chown -R claude:claude /workspace /home/claude
RUN python3 -m venv /opt/runtime && /opt/runtime/bin/pip install --no-cache-dir fastapi==0.115.14 uvicorn==0.34.3
COPY service.py client_bridge.py /opt/
USER claude
# The whole home is a per-worker volume: OAuth credentials, ~/.claude.json state and session transcripts live there.
ENV HOME=/home/claude DISABLE_AUTOUPDATER=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
WORKDIR /workspace
CMD ["/opt/runtime/bin/uvicorn", "service:app", "--app-dir", "/opt", "--host", "0.0.0.0", "--port", "4500", "--no-access-log"]
