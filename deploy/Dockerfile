# syntax=docker/dockerfile:1

# Stage 1: Build Vite + Preact frontends (`web/dist/dashboard`, `web/dist/cocreation`, and `web/dist/survey`)
FROM node:22-alpine AS web-builder

WORKDIR /src/web

COPY web/package.json web/package-lock.json ./
COPY web/packages/ui-core/package.json ./packages/ui-core/package.json
COPY web/apps/dashboard/package.json ./apps/dashboard/package.json
COPY web/apps/cocreation/package.json ./apps/cocreation/package.json
COPY web/apps/survey/package.json ./apps/survey/package.json
COPY web/apps/webgl-bridge/package.json ./apps/webgl-bridge/package.json

RUN npm ci

COPY web/ ./
RUN npm run build --workspace=@sokoban/dashboard --workspace=@sokoban/cocreation --workspace=@sokoban/survey

# Stage 2: Minimal Python 3.12 runtime for unified FastAPI server
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1

WORKDIR /app

COPY pyproject.toml ./
RUN python -c "import subprocess, sys, tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', *deps])"

COPY server ./server
COPY --from=web-builder /src/web/dist ./web/dist

RUN groupadd --system appuser \
    && useradd --system --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/server/data \
    && chown -R appuser:appuser /app/server/data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; resp = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4); sys.exit(0 if resp.status == 200 else 1)"]

CMD ["sh", "-c", "exec uvicorn server.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
