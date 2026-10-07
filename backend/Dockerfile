FROM python:3.13-slim

# Install OpenCV dependencies and the cached-PDF page renderer.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libgthread-2.0-0 \
    curl \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Install uv.
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /uvx /bin/

# Copy the application into the container.
COPY pyproject.toml uv.lock /app/
COPY backend /app/backend

# Install the application dependencies.
WORKDIR /app
# Since we're using a non-standard project structure with the backend in a subdirectory
# but pyproject.toml in the root, we need to set PYTHONPATH
ENV PYTHONPATH=/app

RUN uv sync --locked --no-cache

# Add the virtual environment to PATH
ENV PATH="/app/.venv/bin:${PATH}"

# Create necessary directories
RUN mkdir -p /app/static/uploads /app/static/images /app/static/videos
RUN mkdir -p /app/backend/static/uploads /app/backend/static/images /app/backend/static/videos

# Verify the fastapi command is available
RUN ls -la /app/.venv/bin/ && /app/.venv/bin/fastapi --version

RUN groupadd --system --gid 10001 docintel \
    && useradd --system --uid 10001 --gid 10001 --no-create-home docintel \
    && chmod -R a+rX /app \
    && chown -R docintel:docintel /app/static /app/backend/static

USER 10001:10001

# Expose port
EXPOSE 80

# Run the application using fastapi CLI with a shell wrapper to ensure PATH is set
CMD ["/app/.venv/bin/fastapi", "run", "backend/main.py", "--port", "80", "--host", "0.0.0.0"]