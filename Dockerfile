FROM ghcr.io/astral-sh/uv:0.11.33-python3.12-trixie-slim@sha256:7b5a3c8f80fb965911cf8f0c6690b30b39f10f691fc1e52cd6cc44bc509c3aeb

LABEL org.opencontainers.image.title="Suno Song Evaluator" \
      org.opencontainers.image.description="Evidence-first comparison and release support for Suno song candidates" \
      org.opencontainers.image.source="https://github.com/wangyuyan-agent/suno-song-evaluator" \
      org.opencontainers.image.version="0.2.1" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

RUN groupadd --gid 10001 song-eval \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin song-eval \
    && mkdir -p /data /library \
    && chown -R 10001:10001 /data /library

ENV PATH="/app/.venv/bin:${PATH}"

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).read()"

ENTRYPOINT ["song-eval"]
CMD ["--help"]
