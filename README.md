# Tinfoil: private AI for your Wizard

Install from the Wizard App Store (`wam install wizard-tinfoil`). Open Tinfoil, paste an API key from https://dash.tinfoil.sh and tap Connect. Your agent gets a private Tinfoil model in its model picker; your default model does not change. Disconnect removes the key and the model.

Image source and CI: `humanitylabs-org/wizard-app-store` `images/wizard-tinfoil` (published only after its tests pass, amd64 and arm64).

## Agent registration
On Connect (and on start, if a key is saved) the app adds itself to the agent through the stock Hermes dashboard API (`POST /api/providers/custom-endpoints`, `make_default: false`, one chat model: Kimi K3 when available). Hermes receives only this box's local gateway token, never the Tinfoil key. The dashboard login is filled in by the App Manager from the Hermes Agent app (fresh installs) or the host dashboard (alongside installs). If the agent can't be reached, the key is still saved and status shows `chatIntegration: unavailable`; it retries on the next Test or restart.

The gateway accepts the request fields agents send (reasoning effort, thinking, cache key) and maps Kimi K3's unsupported `medium` effort to `high`.

## Owner workflow
Open Tinfoil in the Wizard launcher, create an inference API key at https://dash.tinfoil.sh, paste it in the masked field, and Connect. The field clears immediately. Test rechecks the enclave and model catalog without inference. Disconnect deletes the provider key; the independent box-only gateway bearer remains stable so reconnecting does not break the agent's registration. No page, GET or MCP returns either credential.

**Important tested limitation:** Tinfoil `/v1/models` accepts a synthetic invalid key. Connect and Test therefore **cannot authenticate the key**. They save it only after successful attestation and mark it `credentialStored: true`, `credentialVerified: false`, `status: not_connected`. Only a successful separately authorized inference request marks it connected. No paid request is made for key validation. Usage/billing stay null; no admin key is requested.

## Transport and security boundary
The sole upstream implementation is official Apache-2.0 `tinfoil==0.14.0` (tag commit `9f2fe2756b910e55ae895d3a4cf64e6d76600ef0`). Its verified transport authenticates the enclave before requests; no alternate OpenAI endpoint, measurement bypass or unverified fallback exists. Current default is attested EHBP. Dependencies have exact versions and hashes in requirements.lock; Python base is digest-pinned.

Provider key and local bearer use atomic replace, fsync and 0600 files beneath a 0700 app-owned `/data/private` directory. HOME and XDG caches also live there, allowing Sigstore verification with a read-only root filesystem. This is private app storage, not encryption at rest against host root. Upstream keys reach the SDK worker over stdin only, never argv, URLs, logs, exceptions, MCP, HTML, GET output or browser storage; worker stderr is discarded.

Host/Origin allowlists prevent DNS rebinding and cross-site writes. Browser mutations require an approved Origin and `X-Wizard-Request: 1`. `/api/status` is the sanitized browser route. MCP, `/api/connection` and inference are box-only and refuse forwarding/Cloudflare/Tailscale identity headers. Same-origin shell paths support framing and the shipped appearance handshake. The production listener binds only host loopback at 18804; staging at 18803 is independent.

## Same-agent integration
`/v1/models` and `/v1/chat/completions` are local OpenAI-compatible routes requiring `/data/private/gateway-token.json`. Provision this independent bearer process-to-process, without printing it or giving Hermes the provider key. A bearer can be registered before any provider key exists: inference then returns 409 without an upstream call. Model IDs must come from the attestation-verified catalog, never invented names. Caller-supplied URLs, credentials and headers are forbidden.

Streaming is currently **buffered SSE**, not real-time token streaming. Tool-call fields and non-streaming completions pass through the official SDK. No Responses, embedding or audio gateway is implemented. Disconnect blocks new requests; already admitted requests may finish. The `chatIntegration` status field is app-local and does not inspect external Hermes registration.

## Private-provider relay (0.3.0)

An independent listener on container port **8090** relays EHBP ciphertext; the
existing SDK-backed, decrypting `/v1` gateway on the main port is unchanged.
Nothing publishes 8090 on the host. The Agent Service attaches an internal
`10.252.x.0/24` network with alias `wizard-tinfoil-relay`. Admission uses the
actual peer socket address and a matching **current interface subnet**, not the
whole reserved /16 or a forwarded header. Even health checks require that subnet.

On the existing owner/box port, `POST /api/private-clients` with
`{"agent":"agent-id"}` returns a fresh opaque `token` and
`relay_url: "http://wizard-tinfoil-relay:8090"`. Repeating the request revokes
the previous token. `DELETE /api/private-clients/agent-id` revokes it (204,
idempotent). Both routes reuse the key-save route's Host/Origin and
`X-Wizard-Request: 1` write guards, plus the box-only API's forwarding refusal.
Agent IDs contain 1–128 letters, digits,
underscores or hyphens and start with a letter/digit. Only SHA-256 token digests
are persisted, atomically at mode 0600, in `private-clients.json`.
`GET /api/connection` returns chat models as `[{id,label}]`; `/api/status`, MCP,
and the legacy gateway retain their existing representations.

The relay requires that per-agent bearer for `GET /attestation` (forwarded to
ATC) and `POST /v1/*`. POST additionally requires `Ehbp-Encapsulated-Key` and
`X-Tinfoil-Enclave-Url`: an HTTPS origin ending `.tinfoil.sh`, no userinfo,
nonstandard port, path, query or fragment. It strips client credentials, adds
the saved provider key, forwards encrypted framing and `Ehbp-Response-Nonce`,
and flushes bounded chunks in both directions without parsing payloads. It
never follows redirects, logs request bodies/headers or uses an SDK/decrypts.
Missing key, transport failure and invalid admission fail closed. Revocation
and Disconnect block new admissions; already admitted requests may finish.

**SDK integration caveat:** official Python 0.14.0 and JavaScript 1.2.2 reject
an HTTP attestation bundle URL and do not send the configured bearer during
bundle fetching. The literal shared HTTP-relay client configuration therefore
needs an explicit client-side transport adapter; it does not work unchanged.
Python also tries online Sigstore TUF refreshes even with a complete bundle.
The live probe below uses the official JS SDK with a test-only loopback HTTPS
attestation adapter and an ephemeral trusted CA, adding the token on the hop
to the relay. All SDK attestation verification and EHBP remain enabled. This
proves the relay, not compatibility with an unmodified HTTP-bundle client.

Reproduce the opt-in, paid live proof in isolated disposable networks:

```sh
docker build -t pp-test-tinfoil-relay -f app/Dockerfile .
docker run --rm --network none -v "$PWD/tests:/app/tests:ro" pp-test-tinfoil-relay python -m unittest discover -s tests -v
docker build -t pp-test-relay-client -f tests/relay-client.Dockerfile tests
python3 tests/live_relay.py --key-env /path/to/owner/tinfoil.env
docker image rm pp-test-relay-client pp-test-tinfoil-relay
```

The env file must contain `TINFOIL_API_KEY`; it is read by the test process,
never mounted, copied, printed or passed to the SDK client. The app saves it
through its own route into disposable tmpfs. The client has only the internal
network, no internet, and no provider key. The capture helper alone has NET_RAW;
the provider and client run non-root, read-only and cap-drop ALL. The test checks
verified streaming/nonstreaming `OK`, actual off-subnet refusal, token rotation
and revocation, both EHBP headers on captured relay traffic, and absence of the
random `PRIVPROBE-…` codeword from captured wire, app logs (stdout/stderr) and all
app `/data` files. Only capture summary booleans/counts are emitted; packet
payloads and tokens are not saved. Containers/networks are removed in `finally`;
remove the two test image tags afterwards. No live installation is touched.

The compose digest deliberately remains the previously published image:
Miguel must publish/re-pin 0.3.0 using the existing release workflow after review;
this branch does not invent or overwrite a registry digest.

## Packaging and verification
The recipe uses uid 1000, a read-only root filesystem, dropped capabilities, no-new-privileges, and only its own named private data volume. Runtipi recursively chmods bind-mounted app-data during lifecycle actions; the named volume keeps credential permissions outside that operation. Images are published for amd64 and arm64.

`wam check . --proposal` passes. Run the six unit tests in the actual image with the entire source mounted at `/qa`, then `python -m unittest discover -s /qa/tests -v`; these cover local auth, safe state, CSRF/DNS/forwarding guards, persistence, SDK use, and real SDK attestation rejection before HTTP. Doubles are application control-flow tests, not cryptographic proof.

Production checks exercised the actual Wizard launcher iframe at phone and desktop widths on all three owner shell ports: masked input, successful real free attestation/catalog read with an explicitly synthetic invalid key, pending authentication, Test, Disconnect, no page errors or horizontal overflow, and live hosted theme handshake. The synthetic key was removed. Restart preserved the independent bearer and left provider credentials absent. No paid inference was performed.

Official docs: https://docs.tinfoil.sh/sdk/python-sdk , https://docs.tinfoil.sh/sdk/overview , https://docs.tinfoil.sh/tutorials/coding-agents . Wizard contract: `humanitylabs-org/wizard-os` `spec/AI_CONNECTIONS.md`.
