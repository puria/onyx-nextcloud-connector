# Local stdio notmuch MCP server wrapped as Streamable HTTP for Onyx.
# No host port is published; the container joins only the dedicated
# onyx-notmuch-mcp Docker network shared with onyx-api_server.
#
# Mail/config directories are bind-mounted read-only at runtime. No draft/tag/
# export flags are passed, so the upstream server exposes only its read tier.

FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/mcp-venv/bin:${PATH}" \
    NOTMUCH_CONFIG=/home/node/.notmuch-config

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        notmuch \
        poppler-utils \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/mcp-venv \
    && /opt/mcp-venv/bin/pip install --no-cache-dir --pre \
        "mcp-server-notmuch @ git+https://github.com/hgn/mcp-server-notmuch.git@ec75db63582bcc5a8ff88e97bd7a3cbeb65aad71" \
    && npm install --global supergateway@4.1.0

USER node

CMD ["supergateway", "--stdio", "mcp-server-notmuch", "--outputTransport", "streamableHttp", "--stateful", "--port", "8765"]
