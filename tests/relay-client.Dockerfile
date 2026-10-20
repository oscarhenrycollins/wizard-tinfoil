# Test-only official JavaScript SDK client; not the provider runtime image.
FROM node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
WORKDIR /probe
RUN npm init -y >/dev/null && npm install --save-exact tinfoil@1.2.2
COPY relay-client.cjs /probe/relay-client.cjs
USER node
ENV HOME=/work
CMD ["node"]
