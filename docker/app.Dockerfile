FROM node:24.11.1-bookworm-slim AS web-builder
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13.7-slim-bookworm AS app
ARG APP_UID=10001
ARG APP_GID=10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install --no-install-recommends -y postgresql-client tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid ${APP_GID} drydock \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --no-create-home --shell /usr/sbin/nologin drydock
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY warden_drydock/ ./warden_drydock/
RUN pip install --no-cache-dir '.[postgres]'
COPY --from=web-builder /build/web/dist ./web/dist/
COPY docker/entrypoint.sh /usr/local/bin/drydock-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/drydock-entrypoint \
    && chmod 0555 /usr/local/bin/drydock-entrypoint \
    && mkdir -p /var/lib/drydock/snapshots /var/lib/drydock/secrets \
    && chown -R ${APP_UID}:${APP_GID} /var/lib/drydock \
    && chmod 0700 /var/lib/drydock/secrets
USER ${APP_UID}:${APP_GID}
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/drydock-entrypoint"]
CMD ["python", "-m", "warden_drydock.hosted.operations.server"]
