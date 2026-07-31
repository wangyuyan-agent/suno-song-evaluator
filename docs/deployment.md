# Deployment and security

Suno Song Evaluator is local-only by default. A server deployment is an
explicit mode with four required controls:

1. a non-loopback bind must opt in with `--allow-remote`;
2. every accepted Host header must be listed exactly with `--allowed-host`;
3. one administrator username and a password file are required;
4. the public endpoint must terminate HTTPS before traffic reaches the app.

The provided Compose stack implements those controls with application-level
HTTP Basic authentication and Caddy automatic HTTPS. The app container has no
published port, so Caddy is the only public entry point.

For a private tailnet deployment, `compose.tailscale.yaml` instead publishes the
app only on host loopback, disables Caddy by profile, and relies on Tailscale
Serve for tailnet-only HTTPS. Application Basic Auth remains enabled.

## Account model

Version 0.2.1 supports one trusted administrator account. It does not provide
registration, password recovery, per-project roles, multi-tenant isolation, or
an audit log. Do not expose one deployment to mutually untrusted users. For
teams, put an identity-aware proxy in front and keep the application Basic Auth
enabled as defense in depth.

The password is mounted as a Docker secret file and never passed as a command
argument or environment variable. `/health` is intentionally unauthenticated
for container health checks; it returns only status and version.

## Deployment matrix

The same locked application can be started four ways. These are separate
operational paths, not different feature editions:

| Path | Best fit | HTTPS owner | Persistent state |
| --- | --- | --- | --- |
| native `uv` | local development, host service managers | caller | host paths |
| direct `docker run` | container smoke/integration | caller | named volume |
| Compose + Tailscale | private VPS; recommended here | Tailscale Serve | named volume |
| Compose + Caddy | public DNS endpoint | Caddy/ACME | named volumes |

Native and direct-Docker examples below intentionally bind only to loopback.
They are not public deployment recipes. The verification helper retries startup
health because first-time imports on a small server can take several seconds:

```bash
deploy/verify-deployment.sh BASE_URL USERNAME PASSWORD_FILE
```

It expects `/health` to return 200, `/` without credentials to return 401, and
`/` with credentials to return 200. It does not put the password in a command
argument or environment variable.

## Native `uv`

Native mode requires Python 3.11 or newer and `uv`. Install host audio
libraries first. On Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install --yes ffmpeg libsndfile1
```

Then create a locked environment and a local, authenticated loopback service:

```bash
uv sync --locked --no-dev
install -d -m 0700 runtime runtime/review-media
umask 077
openssl rand -base64 36 | tr -d '\n' > runtime/admin_password.txt

uv run song-eval serve \
  --db runtime/song-eval.sqlite \
  --host 127.0.0.1 \
  --port 8765 \
  --auth-username admin \
  --auth-password-file runtime/admin_password.txt \
  --media-dir runtime/review-media \
  --library-root audio
```

In another shell:

```bash
deploy/verify-deployment.sh \
  http://127.0.0.1:8765 admin runtime/admin_password.txt
```

`runtime/`, audio, databases, and secrets are ignored by Git. For a persistent
native service, use the host's service manager, a dedicated unprivileged user,
an absolute database path, and an HTTPS/VPN proxy; Compose remains the provided
server lifecycle.

## Direct `docker run`

This route exercises the image without Compose. The example remains local-only
and preserves data in an explicit named volume:

```bash
sudo ./deploy/prepare-linux.sh
docker build --tag suno-song-evaluator:0.2.1 .
docker volume create song-eval-docker-run-data

docker run --detach --name song-eval-docker-run \
  --restart unless-stopped \
  --publish 127.0.0.1:8765:8765 \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --mount type=volume,source=song-eval-docker-run-data,target=/data \
  --mount type=bind,source="$(pwd)/audio",target=/library,readonly \
  --mount type=bind,source="$(pwd)/secrets/admin_password.txt",target=/run/secrets/song_eval_admin_password,readonly \
  suno-song-evaluator:0.2.1 serve \
  --db /data/song-eval.sqlite \
  --host 0.0.0.0 --port 8765 --allow-remote \
  --allowed-host localhost \
  --auth-username admin \
  --auth-password-file /run/secrets/song_eval_admin_password \
  --media-dir /data/review-media \
  --library-root /library

sudo deploy/verify-deployment.sh \
  http://127.0.0.1:8765 admin secrets/admin_password.txt
```

Remove the container with `docker rm --force song-eval-docker-run`. Removing
the named volume is a separate, destructive action and deletes its database and
review media.

## Server deployment with Docker Compose

Before either Compose topology, prepare default bind mounts:

```bash
cp .env.example .env
# Edit APP_DOMAIN and SONG_EVAL_USERNAME first.
sudo ./deploy/prepare-linux.sh
```

The script refuses symbolic-link deployment paths, preserves an existing
password, generates a random one only when absent, sets the secret to numeric
UID/GID 10001 with mode `0400`, and makes existing regular files under `audio/`
group-readable by GID 10001. It never prints the password.

### Tailscale-only server

Requirements:

- a Linux server with current Docker Engine and Docker Compose;
- the server and client joined to the same tailnet;
- Tailscale HTTPS enabled for the tailnet;
- no inbound public application port.

Set `APP_DOMAIN` in `.env` to the server's exact MagicDNS name without the
trailing dot. Start only the private topology:

```bash
docker compose -f compose.yaml -f compose.tailscale.yaml config --quiet
docker compose -f compose.yaml -f compose.tailscale.yaml up --build -d

# Use 443 when it is free. This example preserves an existing service on 443.
sudo tailscale serve --bg --https=8444 http://127.0.0.1:8765
tailscale serve status
```

Verify that the host listener is loopback-only and that Serve says `tailnet
only`:

```bash
ss -lnt | grep ':8765'
curl --fail -H "Host: server-name.tailnet.ts.net" \
  http://127.0.0.1:8765/health
curl --fail "https://server-name.tailnet.ts.net:8444/health"
sudo env SONG_EVAL_VERIFY_HOST=server-name.tailnet.ts.net \
  deploy/verify-deployment.sh \
  http://127.0.0.1:8765 admin secrets/admin_password.txt
```

The expected host listener is `127.0.0.1:8765`, never `0.0.0.0:8765` or
`[::]:8765`. Do not use Tailscale Funnel, and do not run `tailscale serve
reset` on a node that serves other applications.

To remove only this listener while preserving the existing Serve configuration:

```bash
sudo tailscale serve --https=8444 off
```

### Internet-facing server

Requirements:

- a Linux server with current Docker Engine and Docker Compose;
- a DNS A/AAAA record for the server;
- inbound TCP 80/443 and UDP 443;
- an outbound path for Caddy certificate issuance and, if enabled, the LLM
  provider.

Start the deployment after running the shared preparation step:

```bash
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

Use a unique random password of at least 16 characters. For an Internet-facing
service, also restrict source IPs at the firewall or place the stack behind a
VPN/identity-aware proxy. Basic Auth does not provide registration, MFA, or
brute-force rate limiting.

The application state lives in the `song_eval_data` named volume. The host
`audio/` directory is mounted read-only at `/library`. Copy candidate audio
there before importing it, and keep copied files group-readable by GID 10001
(for example mode `0640`). Original files are read byte-for-byte and are never
normalized in place.

Inspect health and logs:

```bash
curl --fail https://evaluator.example.com/health
docker compose logs --tail=100 app caddy
sudo deploy/verify-deployment.sh \
  https://evaluator.example.com admin secrets/admin_password.txt
```

Stop or upgrade:

```bash
docker compose down
git pull --ff-only
docker compose up --build -d
```

`docker compose down` preserves named volumes. Do not add `--volumes` unless
you intend to delete the database and review media.

### Isolated Caddy smoke test

`compose.smoke.yaml` replaces public mappings with high, loopback-only ports.
It is for validating the Caddy reverse proxy and headers without opening the
firewall or requesting a public certificate. Docker Compose 2.24.4 or newer is
required for the `!override` port reset.

```bash
APP_DOMAIN=localhost docker compose \
  --project-name song-eval-caddy-smoke \
  --file compose.yaml --file compose.smoke.yaml \
  up --build --detach

sudo env SONG_EVAL_VERIFY_INSECURE=1 \
  deploy/verify-deployment.sh \
  https://localhost:18443 admin secrets/admin_password.txt

curl --insecure --dump-header - --output /dev/null \
  https://localhost:18443/health
ss -lnt | grep -E '127\.0\.0\.1:(18080|18443)'

APP_DOMAIN=localhost docker compose \
  --project-name song-eval-caddy-smoke \
  --file compose.yaml --file compose.smoke.yaml \
  down --volumes
```

The local-CA smoke can log that Caddy could not install its root certificate
inside the read-only container. That is expected for this isolated test. A
successful smoke does not prove public DNS reachability or ACME issuance; those
must be verified on the real domain without `--insecure`.

## Optional LLM narration

The deterministic narrator requires no network or key. To enable an
OpenAI-compatible provider:

```bash
read -r -s SONG_EVAL_LLM_API_KEY
printf '%s' "$SONG_EVAL_LLM_API_KEY" > secrets/llm_api_key.txt
unset SONG_EVAL_LLM_API_KEY
sudo chown 10001:10001 secrets/llm_api_key.txt
sudo chmod 0400 secrets/llm_api_key.txt

# Configure SONG_EVAL_LLM_BASE_URL and SONG_EVAL_LLM_MODEL in .env.
docker compose -f compose.yaml -f compose.llm.yaml up --build -d
```

For the private topology, include both overlays:

```bash
docker compose \
  -f compose.yaml -f compose.tailscale.yaml -f compose.llm.yaml \
  config --quiet
docker compose \
  -f compose.yaml -f compose.tailscale.yaml -f compose.llm.yaml \
  up --build -d
```

Only structured evidence is sent. The provider URL is fixed by server
configuration; an API request cannot redirect the configured key to another
URL. LLM output cannot create measurements or alter the stored recommendation.

## Audio tooling in the image

The image installs FFmpeg (including ffprobe) and libsndfile during `docker
build`. Python dependencies are reproduced from `uv.lock` with `uv sync
--locked`. Analysis uses:

- SoundFile/libsndfile for decoding;
- NumPy and SciPy for feature extraction and comparisons;
- pyloudnorm for loudness measurements and listening-match gain;
- FFmpeg/ffprobe as the compressed-format inspection and decoding fallback.

No speech model is downloaded at runtime. MLX Whisper is an optional
Apple-Silicon-only host extra; a Linux container should ingest an existing
Whisper-compatible JSON transcript instead.

## Backup boundary

The SQLite database and generated review media are the durable state. Back up
the `song_eval_data` volume while the app is stopped, or use a filesystem
snapshot with equivalent consistency guarantees. The original audio library is
outside that volume and must be backed up separately.

Never:

- publish port 8765 directly;
- use `--allowed-host '*'`;
- commit `.env`, `secrets/`, databases, or audio;
- store an LLM key in Compose YAML;
- treat Basic Auth without HTTPS as secure.
