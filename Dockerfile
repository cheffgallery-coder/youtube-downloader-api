FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy
ENV DENO_INSTALL=/root/.deno
ENV PATH="${DENO_INSTALL}/bin:${PATH}"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN curl -fsSL https://deno.land/install.sh | sh

COPY . .

RUN uv sync --no-dev

CMD ["sh", "-c", "if [ -n \"$YOUTUBE_COOKIES_B64\" ]; then echo \"$YOUTUBE_COOKIES_B64\" | base64 -d > /app/cookies.txt && chmod 600 /app/cookies.txt && echo 'YouTube cookies loaded'; else echo 'WARNING: YOUTUBE_COOKIES_B64 is empty'; fi; uv run fastapi run app --host 0.0.0.0 --port ${PORT:-8000}"]
