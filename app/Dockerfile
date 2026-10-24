FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS realtime
WORKDIR /sdk
COPY app/package.json app/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
LABEL org.opencontainers.image.source="https://github.com/oscarhenrycollins/wizard-tinfoil" \
      org.opencontainers.image.licenses="GPL-3.0-only"
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock
COPY app /app/app
COPY --from=realtime /usr/local/bin/node /usr/local/bin/node
COPY --from=realtime /sdk/node_modules /app/app/node_modules
COPY web /app/web
COPY openapi.json /app/openapi.json
RUN mkdir /data && chown 1000:1000 /data && chmod 700 /data
USER 1000:1000
ENV DATA_DIR=/data/private PORT=18804 HOME=/data/private XDG_CACHE_HOME=/data/private/cache XDG_DATA_HOME=/data/private/share PYTHONDONTWRITEBYTECODE=1
EXPOSE 18804 8090
CMD ["python", "/app/app/server.py"]
