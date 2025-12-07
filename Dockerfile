FROM ghcr.io/astral-sh/uv:0.6.17-python3.13-bookworm

RUN useradd -m devuser
RUN mkdir -p /app
RUN chown devuser:devuser /app

WORKDIR /app
USER devuser

# Set up the python environment with uv
RUN uv init
RUN uv add uiprotect requests pytz tzdata openai
RUN uv sync

# Copy only the source code and environment file
COPY --chown=devuser:devuser src/ src/
COPY --chown=devuser:devuser instructions.txt* . 

# Create necessary directories
RUN mkdir -p images log events

CMD ["uv", "run", "src/main.py", "--notify", "--scheduled-exit"]
 