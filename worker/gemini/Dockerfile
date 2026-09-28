FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates git && rm -rf /var/lib/apt/lists/* \
    && npm install --global @google/gemini-cli@0.61.0 \
    && groupadd --gid 10001 gemini && useradd --uid 10001 --gid 10001 --create-home gemini \
    && mkdir -p /workspace /home/gemini/.gemini && chown -R gemini:gemini /workspace /home/gemini
COPY --chown=gemini:gemini probe.py mcp_probe.py /opt/gemini-probe/
COPY --chown=gemini:gemini settings.json /home/gemini/.gemini/settings.json
USER gemini
WORKDIR /workspace
ENV HOME=/home/gemini NO_BROWSER=true
CMD ["sleep", "infinity"]
