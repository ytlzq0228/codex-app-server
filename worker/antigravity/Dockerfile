FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl python3 python3-venv git libsecret-1-0 && rm -rf /var/lib/apt/lists/*
ADD https://storage.googleapis.com/antigravity-public/antigravity-cli/1.2.12-5784551402897408/linux-x64/cli_linux_x64.tar.gz /tmp/agy.tar.gz
RUN echo 'd5f0fe7433cb7c43ea878c07627a4fdb82d218f3bef5e6436266f5d9fdd2df145523453b9be0c4250391a64a007f5f42f7faff797bc2b2d502e7efb4874e383a  /tmp/agy.tar.gz' | sha512sum -c - \
    && tar -xzf /tmp/agy.tar.gz -C /usr/local/bin antigravity \
    && mv /usr/local/bin/antigravity /usr/local/bin/agy && chmod 755 /usr/local/bin/agy \
    && rm /tmp/agy.tar.gz \
    && groupadd -g 10001 agy && useradd -m -u 10001 -g agy agy \
    && mkdir -p /workspace /home/agy/.gemini/antigravity-cli \
    && chown -R agy:agy /workspace /home/agy
COPY --chown=agy:agy settings.json /home/agy/.gemini/antigravity-cli/settings.json
RUN python3 -m venv /opt/runtime && /opt/runtime/bin/pip install --no-cache-dir fastapi==0.115.14 uvicorn==0.34.3 pyte==0.8.2
COPY service.py /opt/service.py
USER agy
ENV HOME=/home/agy
WORKDIR /workspace
CMD ["/opt/runtime/bin/uvicorn", "service:app", "--app-dir", "/opt", "--host", "0.0.0.0", "--port", "4500", "--no-access-log"]
