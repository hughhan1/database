FROM ghcr.io/astral-sh/uv:0.13.9 AS uv
FROM python:3.15-slim
LABEL io.modelcontextprotocol.server.name="io.github.hughhan1/database" \
      org.opencontainers.image.source="https://github.com/hughhan1/database"
WORKDIR /app
ENV HOST=0.0.0.0 PORT=3000 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv uv sync --locked --no-dev
COPY server.py ./
USER 10001:10001
EXPOSE 3000
CMD ["/app/.venv/bin/holon-serve", "server.py"]
