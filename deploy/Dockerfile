FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.11.32

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY glasscodex ./glasscodex
RUN uv sync --frozen --no-dev --no-editable

USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8792
CMD ["/app/.venv/bin/glasscodex", "serve", "--host", "0.0.0.0"]
