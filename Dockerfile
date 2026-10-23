FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
WORKDIR /app
FROM base AS build
RUN pip install --no-cache-dir uv==0.13.5
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
FROM base AS runtime
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001
CMD ["python", "-m", "uvicorn", "faultline.orders:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
