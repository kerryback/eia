FROM python:3.12-slim

# The Agent SDK drives the Claude Code CLI as a subprocess, so the image needs
# Node alongside Python.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The CLI refuses to skip permission prompts when running as root, and the
# agent runs unattended, so give it an unprivileged user to work as.
RUN useradd --create-home --home-dir /home/app --uid 10001 app \
    && mkdir -p /app/workspaces \
    && chown -R app:app /app /home/app

USER app
ENV HOME=/home/app

# Warm the matplotlib font cache as the runtime user, so the first chart of the
# first session isn't paying for it.
RUN python -c "import matplotlib.pyplot"

EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
