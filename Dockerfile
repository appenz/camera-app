FROM ghcr.io/astral-sh/uv:0.6.17-python3.13-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN useradd -m devuser
RUN mkdir -p /app
RUN chown devuser:devuser /app

WORKDIR /app
USER devuser

# Install the exact dependency versions from uv.lock; uiprotect releases have broken the app before
COPY --chown=devuser:devuser pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen

# Copy only the source code and environment file
COPY --chown=devuser:devuser src/ src/
COPY --chown=devuser:devuser animals.prompt person.prompt ./

# Create necessary directories
RUN mkdir -p images log events

CMD ["uv", "run", "--frozen", "src/main.py", "--notify", "--scheduled-exit"]
 