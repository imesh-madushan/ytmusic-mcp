FROM python:3.12-slim

RUN useradd --create-home --uid 10001 app \
    && mkdir /data && chown app /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY ytmusic_mcp ./ytmusic_mcp
RUN pip install --no-cache-dir .

USER app
# Inside a container the app must listen on all interfaces; docker-compose.yml
# publishes the port on 127.0.0.1 only.
ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8787 \
    YTMUSIC_DB_PATH=/data/ytmusic-mcp.db
VOLUME /data
EXPOSE 8787

CMD ["python", "-m", "ytmusic_mcp"]
