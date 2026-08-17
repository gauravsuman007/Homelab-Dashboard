# Two stages so the image builds on every target architecture.
#
# Some dependencies (PyYAML's libyaml binding) ship no wheel for linux/386 or
# linux/arm/v7, so pip falls back to compiling from source -- which needs a
# toolchain the slim runtime image does not have. The builder stage carries gcc
# and produces a virtualenv; the runtime stage copies just the venv, so the
# compiler never reaches the published image.

FROM python:3.12-slim AS builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    PORT=8500

COPY --from=builder /venv /venv

WORKDIR /app
COPY app/ /app/

# Runs as root deliberately: it must read the host's docker socket, which is
# root:docker owned. The socket is the container's only privileged input, and the
# page it serves is reachable from the LAN only.
EXPOSE 8500

# One worker, several threads. Single worker on purpose: the discovery loop and
# its cached snapshot live in the process, so a second worker would double the
# Docker polling and let /api/apps answer from two slightly different states.
# Threads cover the only blocking work -- an icon fetch warming the cache.
CMD ["gunicorn", "--bind", "0.0.0.0:8500", "--workers", "1", "--threads", "8", \
     "--timeout", "30", "--access-logfile", "-", "server:app"]
