FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim AS base
WORKDIR /app
COPY pyproject.toml ./
COPY recon/ recon/
COPY migrations/ migrations/
RUN pip install --no-cache-dir -e .

FROM base AS test
RUN pip install --no-cache-dir -e ".[dev]"
COPY tests/ tests/
CMD ["pytest"]

FROM base
COPY --from=web /web/dist web/dist
