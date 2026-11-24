FROM python:3.12-slim
RUN useradd --create-home --uid 10001 aether
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
RUN pip install --no-cache-dir ".[postgres]" && mkdir -p /app/var/objects /app/var/keys && chown -R aether /app/var
# fixtures are read at seed time (REPO_ROOT/fixtures); mount or copy them for demo seeding
USER aether
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
