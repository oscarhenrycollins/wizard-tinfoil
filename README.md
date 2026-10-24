# Tinfoil: private AI for your Wizard

Install from the Wizard App Store (`wam install wizard-tinfoil`). Open Tinfoil, paste an API key from https://dash.tinfoil.sh and tap Connect. Your agent gets a private Tinfoil model in its model picker; your default model does not change. Disconnect removes the key and the model.

Image source and CI: `humanitylabs-org/wizard-app-store` `images/wizard-tinfoil` (published only after its tests pass, amd64 and arm64).

## Agent registration
On Connect (and on start, if a key is saved) the app adds itself to the agent through the stock Hermes dashboard API (`POST /api/providers/custom-endpoints`, `make_default: false`, one chat model: Kimi K3 when available). Hermes receives only this box's local gateway token, never the Tinfoil key. The dashboard login is filled in by the App Manager from the Hermes Agent app (fresh installs) or the host dashboard (alongside installs). If the agent can't be reached, the key is still saved and status shows `chatIntegration: unavailable`; it retries on the next Test or restart.

The gateway accepts the request fields agents send (reasoning effort, thinking, cache key) and maps Kimi K3's unsupported `medium` effort to `high`.

## Owner workflow
Open Tinfoil in the Wizard launcher, create an inference API key at https://dash.tinfoil.sh, paste it in the masked field, and Connect. The field clears immediately. Test rechecks the enclave and model catalog without inference. Disconnect deletes the provider key; the independent box-only gateway bearer remains stable so reconnecting does not break the agent's registration. No page, GET or MCP returns either credential.

**Important tested limitation:** Tinfoil `/v1/models` accepts a synthetic invalid key. Connect and Test therefore **cannot authenticate the key**. They save it only after successful attestation and mark it `credentialStored: true`, `credentialVerified: false`, `status: not_connected`. Only a successful separately authorized inference request marks it connected. No paid request is made for key validation. Usage stays null unless the owner adds the optional billing key below.

## Billing (optional admin key)
The Tinfoil page has two sections, API key and Admin key, each with Save, Replace and Remove (showing only the last 4 characters). Under Admin key the owner can paste a Tinfoil **admin key** (Dashboard → Admin Keys; unscoped, because Tinfoil refuses billing to scoped keys). It is checked against Tinfoil before it is saved, kept 0600 in this app's private volume, and only ever used for `GET /api/auth/context` and `GET /api/billing/{usage,transactions}`: never to create, change or delete keys. No route, MCP tool or log returns it. `/api/connection` then reports `usage.paid = {type:'credits', balanceMinor, limitMinor (last top-up), usedMinor (this period)}`, which Home draws as a fuel gauge. Balance = Tinfoil's $1 starting credit plus credit top-ups, minus all-time spend; before any top-up the full tank is the $1 starting credit. Remove billing key deletes it.

## Transport and security boundary
The inference upstream implementation is official Apache-2.0 `tinfoil==0.14.0` (tag commit `9f2fe2756b910e55ae895d3a4cf64e6d76600ef0`). Its verified transport authenticates the enclave before requests; no alternate OpenAI endpoint, measurement bypass or unverified fallback exists. Current default is attested EHBP. Dependencies have exact versions and hashes in requirements.lock; Python base is digest-pinned.

Provider key and local bearer use atomic replace, fsync and 0600 files beneath a 0700 app-owned `/data/private` directory. HOME and XDG caches also live there, allowing Sigstore verification with a read-only root filesystem. This is private app storage, not encryption at rest against host root. Upstream keys reach the SDK worker over stdin only, never argv, URLs, logs, exceptions, MCP, HTML, GET output or browser storage; worker stderr is discarded.

Host/Origin allowlists prevent DNS rebinding and cross-site writes. Browser mutations require an approved Origin and `X-Wizard-Request: 1`. `/api/status` is the sanitized browser route. MCP, `/api/connection` and inference are box-only and refuse forwarding/Cloudflare/Tailscale identity headers. Same-origin shell paths support framing and the shipped appearance handshake. The production listener binds only host loopback at 18804; staging at 18803 is independent.

## Same-agent integration
`/v1/models` and `/v1/chat/completions` are local OpenAI-compatible routes requiring `/data/private/gateway-token.json`. Provision this independent bearer process-to-process, without printing it or giving Hermes the provider key. A bearer can be registered before any provider key exists: inference then returns 409 without an upstream call. Model IDs must come from the attestation-verified catalog, never invented names. Caller-supplied URLs, credentials and headers are forbidden.

The legacy `/v1/chat/completions` route retains **buffered SSE** for compatibility; the Router's new `/api/infer` route streams tokens immediately. Tool-call fields and non-streaming completions pass through the official SDK. No Responses or embedding gateway is implemented. Disconnect blocks new requests; already admitted requests may finish. The `chatIntegration` status field is app-local and does not inspect external Hermes registration.

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
`GET /api/connection` preserves PR #4's `wizard.ai-connection/2` catalog
descriptors (`id,label,tier,abilities,context`); `/api/status`, MCP and the
legacy gateway retain their existing representations.

The relay requires that per-agent bearer for `GET /attestation` (forwarded to
ATC) and `POST /v1/*`. POST additionally requires `Ehbp-Encapsulated-Key` and
`X-Tinfoil-Enclave-Url`: an HTTPS origin ending `.tinfoil.sh`, no userinfo,
nonstandard port, path, query or fragment. It strips client credentials, adds
the saved provider key, forwards encrypted framing and `Ehbp-Response-Nonce`,
and flushes bounded chunks in both directions without parsing payloads. It
never follows redirects, logs request bodies/headers or uses an SDK/decrypts.
Missing key, transport failure and invalid admission fail closed. Revocation
and Disconnect block new admissions; already admitted requests may finish.

**SDK integration:** high-level official Python 0.14.0 and JavaScript 1.2.2
clients reject an HTTP attestation bundle URL and do not send the relay bearer
during bundle fetching. The published Private Client supplies the supported
adapter: authenticated HTTP bundle fetch, official `Verifier.verifyBundle`,
then official EHBP using only the verified HPKE key. It needs no HTTP-attestation
bypass flag, custom CA or HTTPS loopback in production.
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

PR #4 advances the provider recipe to the future `0.4.0` image tag. Miguel
must publish and digest-pin the combined provider source before deployment;
this rebase does not merge, deploy or invent a provider image digest.

## Runtime-owned declared-host egress

The provider recipe now starts on an internal/no-host-address bootstrap network,
with `restart: no`, external DNS directed to its own loopback, and
`HTTPS_PROXY=http://wizard-egress-proxy:3128`. The Agent Service egress lane
creates/owns its two internal relay/egress networks and one system edge, removes
bootstrap/other interfaces and preserves only fixed incoming owner routes. This
requires its reviewed system helper and loopback-only effective port bindings;
App Manager installation/update must invoke reconciliation before exposing the app.
The old published image digest is intentionally not presented as this new image.

Relay `http.client` now explicitly uses CONNECT; it never silently ignores the
proxy. The decrypting gateway's pinned official Python SDK gets a narrowly scoped
transport adapter because its custom `httpx.HTTPTransport` otherwise ignores proxy
env. All attestation/certificate/provenance/HPKE verification remains enabled.
SDK bundle fetch and Sigstore/TUF's requests fetches honor the proxy directly.

Default ATC GET bundles sometimes select `router-0.tinfoil.sh`. To keep an exact
allowlist, both paths request an attestation bundle specifically for
`inference.tinfoil.sh` / `tinfoilsh/confidential-model-router`. The client still uses
**GET /attestation** with its relay bearer; the relay internally makes a fixed
**POST** to ATC. Only this constant public attestation request is generated; client
ciphertext is still forwarded unchanged, unparsed and undecrypted. A bundle for a
new/undeclared enclave fails closed rather than enabling a wildcard.

`private_inference.egress` adds just **`tuf-repo-cdn.sigstore.dev`** alongside ATC
and inference. A fresh Python SDK's live Sigstore TUF refresh requires that host
(including on Connect/Test); blocking it produced `TUFError`/CONNECT 403. Complete
bundles avoid AMD/GitHub lookups; no Rekor/Fulcio/dashboard browser hosts are added.
Legacy internal Hermes-dashboard registration may report unavailable under this
seal, and is not a reason to grant provider internet or host access.

The actual PR #16 `wizard-private-client` image was exercised through this PR's
relay behind the declared-host proxy, with a real saved key and completion. The
older relay-only probe above is not the egress proof; the reproducible final proof
is `wizard-agent-service` `ops/agent-runtime/prove_provider_egress.py` and its
`PRIVATE_PROVIDER_EGRESS.md`. Local providers get no CONNECT listener/proxy.

## Packaging and verification
The recipe uses uid 1000, a read-only root filesystem, dropped capabilities, no-new-privileges, and only its own named private data volume. Runtipi recursively chmods bind-mounted app-data during lifecycle actions; the named volume keeps credential permissions outside that operation. Images are published for amd64 and arm64.

`wam check . --proposal` is the app-manifest check. Run Python tests with `python -m unittest discover -s tests -v` after installing `requirements.lock`, and realtime tests with `npm --prefix app ci --ignore-scripts && npm --prefix app test`. The tests cover local auth, safe state, CSRF/DNS/forwarding guards, persistence, Router grant refusal, v2 catalog/SSE, live frame mapping, multiple queued utterances, stop/rotation, buffer bounds, sanitized errors, and real SDK attestation rejection before upstream requests. Doubles are application control-flow tests, not cryptographic proof. To test the image, mount `tests/` at `/app/tests` and run `python -m unittest discover -s /app/tests -v`.

Production checks exercised the actual Wizard launcher iframe at phone and desktop widths on all three owner shell ports: masked input, successful real free attestation/catalog read with an explicitly synthetic invalid key, pending authentication, Test, Disconnect, no page errors or horizontal overflow, and live hosted theme handshake. The synthetic key was removed. Restart preserved the independent bearer and left provider credentials absent. No paid inference was performed.

## Router contracts (0.4.0)

The app declares `private: true` and provides `ai-connection`, `tinfoil-api-key`, `private-inference`, and `live-transcription`. No frontend or owner key flow changed.

- **`GET /api/connection` — `wizard.ai-connection/2`:** preserves the /1 status, gateway, usage and credential-verification fields; `models` becomes descriptors `{id,label,tier,abilities,context}` from the verified catalog. DeepSeek V4.1 Flash is `fast` with `[text,streaming]`; other registered chat models are `standard` (none is labeled frontier). `context: null` means the catalog does not supply a verified context limit. Non-chat/audio/embedding endpoints are omitted. `/api/status`, MCP and `/v1/models` retain their existing shapes.
- **`POST /api/infer`:** body `{model,system?,messages,max_tokens?,stream?,abilities?}`. Messages are user/assistant text; `system` is prepended separately. Uses the existing attested Python SDK subprocess, now emitting unbuffered private-pipe events. Always returns `text/event-stream` (even for `stream:false`): `event: delta`, `data: {"type":"delta","text":"..."}`, followed by `event: done`, `data: {"type":"done","model":"<effective model>","usage":{...}}`. A failed stream ends with `event: error`, `data: {"type":"error","code":"signed_out|limit|unreachable|provider_error"}` instead. No automatic substitution; mismatched effective models and incomplete upstream streams fail closed. A successful call marks the stored credential verified.
- **`GET /v1/live` — `wizard.live-transcription/1`:** WebSocket. First text frame is `{"type":"start","format":"pcm_s16le","sample_rate":16000,"channels":1,"language":null}`; then binary PCM frames <=64 KiB and text `{"type":"commit"}` or `{"type":"stop"}`. Returns `ready` with provider/model, provisional `delta`, one `final` per committed utterance containing audio, `error` with code/message, and `closed` followed by normal close. Stop finalizes all queued audio. Invalid formats return `unsupported_format` and close. Start is required before audio, but audio after start and before ready is buffered.

### Router authentication and deployment

Both new inference endpoints **require** `X-Wizard-Grant`. `WIZARD_GRANTS_FILE` points to the same grants-file format used by the Router: `{"enforce":true,"incoming":[{"from":"wizard-connection-router","sha256":"<SHA256 of the Router's outgoing grant for wizard-tinfoil>"}]}`. Provision it privately process-to-process through App Manager (the recipe defaults to `/data/private/router-grants.json`), or inject an appropriately mounted read-only grants path. The Router's `outgoing["wizard-tinfoil"]` must hold that matching token. This PR does not provision a real grant or change a live installation. Missing/malformed files, absent/unknown grants, grants belonging to other apps and `enforce:false` with unknown grants all fail closed. Grant files are re-read per request for rotation. Browser origins, cookies, Authorization and forwarded identity headers are refused; identity hints and the legacy local gateway bearer never authorize these endpoints. Applications use the Router's service/model routes, not direct provider access.

### Realtime attestation route

Inspection of the pinned Python SDK 0.14.0 shows `chat`, `embeddings` and `audio`, but **no realtime API**. Realtime therefore uses a small private Node subprocess in the same image: official npm **`tinfoil@1.2.1`**, exact npm lockfile, `new TinfoilAI({apiKey}).realtime({model:'voxtral-mini-4b-realtime'})`. That SDK verifies the enclave and pins the upstream WebSocket's TLS fingerprint. There is no raw upstream WebSocket, custom host, measurement override or insecure fallback. The app's `wsproto` dependency only frames the downstream Router connection. Python's existing key store passes the key to Node once via a private stdin pipe; stderr is discarded, and audio/transcripts are never logged by the provider.

On `session.created`, send `session.update`, then a **non-final** `input_audio_buffer.commit`, followed by queued base64 PCM appends. Commit sends `{type:'input_audio_buffer.commit',final:true}`. On `transcription.done`, close that native session and rotate to a fresh SDK-attested connection for the next utterance. Queuing preserves every commit boundary even across startup/rotation. Queued PCM is limited to 4 MiB / 128 utterances, startup/finalization to 30 seconds and downstream idle to 180 seconds. Failure/disconnect reaps the child.

### Opt-in real smoke (paid)

Generate **synthetic speech only**, e.g. with ffmpeg's `flite` filter, at 16 kHz mono PCM16. Then explicitly authorize and run `python tools/smoke_router.py --key-file <0600-file> --wav <synthetic-wav> --output <private-log-directory>`. It starts a disposable loopback server and provisions a synthetic Router grant, performs a real attested catalog read and DeepSeek V4.1 Flash inference, streams the WAV in ~250 ms frames to the real attested Voxtral realtime service, commits/stops, and asserts the spoken keywords in the final transcript. Only this opt-in harness records its synthetic prompt/output/transcript in `tinfoil-*.log`; the provider itself never logs prompts/audio/text. Keys/grants are not included in the logs. This smoke never changes a live container or owner state.

The 0.4.0 recipe points to the next store-built image tag. Release CI must build/publish both architectures from this source and pin its resulting digest before deployment; this source PR does not publish or deploy that image.

Official docs: https://docs.tinfoil.sh/sdk/python-sdk , https://docs.tinfoil.sh/sdk/overview , https://docs.tinfoil.sh/tutorials/coding-agents . Wizard contract: `humanitylabs-org/wizard-os` `spec/AI_CONNECTIONS.md`.

### Per-agent relay topology

The runtime attaches one internal 10.252.x.0/24 relay bridge per linked agent.
The relay admits peers only from the actual attached /24s (not the entire pool)
and still requires a per-agent bearer on every relay request. Management listens
only on the runtime-owned 10.253.x.x egress address; it never binds the relay
interfaces. HTTP Host/Origin spoofing cannot expose /api/private-clients to a
relay client. A missing/ambiguous management interface fails closed. No owner
sudo, br_netfilter module or bridged private-client firewall is required.

### Rebase and client release checkpoint

This branch is stacked on PR #4 head `9a32d0869618b7855949aebd7aebff87ed215a14`,
which descends from current main `cbfa896fdd1d484f9c96c44d8b5ad7c20dc3474e`.
Review/land **#4 before #3**; #3 contains #4's commits until then. Keep both
server imports, both sets of owner/Router handlers, live-transcription metadata
and all OpenAPI paths. The v2 Router infer worker now selects the same proxy-aware
verified SDK factory as the existing gateway when HTTPS_PROXY is present;
without a proxy its original SDK selection is unchanged.

The per-agent client is publicly available for linux/amd64 and linux/arm64:
`ghcr.io/humanitylabs-org/wizard-private-client@sha256:c562bef2166e6e1054be8eed34229cf0ead4574002da28f8fe5d4f09ad0e6631`
(source `c3a4345ff68363dfff7435f80344ca1a235624be`, store PR #16,
[publication + anonymous pulls](https://github.com/humanitylabs-org/wizard-app-store/actions/runs/40115387940)).
Agent Service must pin `ops/agent-runtime/private_providers.py:CLIENT_IMAGE`
to that digest. Its separate integration lane owns that edit; this repo does not.

**Deployment boundary:** gateway route contracts and the v2/Realtime unit suites
are preserved, but the sealed recipe is an explicit network migration, not a
safe stand-alone update for an existing gateway installation. Coordinate the
runtime/installer reconciliation before installing it; existing Airlock and
private-worker users must retain gateway reachability. No live app was changed.
PR #4's official JS realtime SDK opens a direct pinned WebSocket and strips
custom proxy agents. Live realtime behind this PR's DNS-disabled CONNECT-only
seal is **not supported/proven by this rebase**; Miguel/Oscar must supply a
verified SDK proxy route (without weakening its pin) or separate that capability
before deploying the combined app. Do not widen the egress allowlist or silently
restore direct internet to make that test pass.
