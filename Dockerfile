FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

ENV DATABASE_URL=sqlite:////data/drugshortage.db \
    SYNC_INTERVAL_HOURS=6 \
    PATH="/srv/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
