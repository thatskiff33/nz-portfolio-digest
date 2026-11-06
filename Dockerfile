# python:3.12-slim
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

# ghcr.io/astral-sh/uv:0.13.7
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1 \
    uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python \
    && rm -f /usr/local/bin/uv \
    && useradd --system --uid 10001 --no-create-home --home-dir /tmp app \
    && mkdir /app/state \
    && chown app /app/state

ENV PATH="/app/.venv/bin:$PATH"
USER app
ENTRYPOINT ["portfolio-digest"]
CMD ["health"]
