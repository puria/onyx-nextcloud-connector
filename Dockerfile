FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_PATH=/data/state.db

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bridge ./bridge
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 bridge \
    && mkdir -p /data \
    && chown -R bridge:bridge /data /app
USER bridge

VOLUME ["/data"]
ENTRYPOINT ["onyx-nextcloud-connector"]
CMD ["sync", "--daemon"]
