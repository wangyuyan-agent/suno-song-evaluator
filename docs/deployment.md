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

## Account model

Version 0.2.0 supports one trusted administrator account. It does not provide
registration, password recovery, per-project roles, multi-tenant isolation, or
an audit log. Do not expose one deployment to mutually untrusted users. For
teams, put an identity-aware proxy in front and keep the application Basic Auth
enabled as defense in depth.

The password is mounted as a Docker secret file and never passed as a command
argument or environment variable. `/health` is intentionally unauthenticated
for container health checks; it returns only status and version.

## Server deployment with Docker Compose

Requirements:

- a Linux server with current Docker Engine and Docker Compose;
- a DNS A/AAAA record for the server;
- inbound TCP 80/443 and UDP 443;
- an outbound path for Caddy certificate issuance and, if enabled, the LLM
  provider.

Prepare the deployment:

```bash
cp .env.example .env
mkdir -p audio secrets
chmod 700 secrets

# Edit APP_DOMAIN and SONG_EVAL_USERNAME in .env.
read -r -s SONG_EVAL_PASSWORD
printf '%s' "$SONG_EVAL_PASSWORD" > secrets/admin_password.txt
unset SONG_EVAL_PASSWORD
chmod 600 secrets/admin_password.txt

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
there before importing it. Original files are read byte-for-byte and are never
normalized in place.

Inspect health and logs:

```bash
curl --fail https://evaluator.example.com/health
docker compose logs --tail=100 app caddy
```

Stop or upgrade:

```bash
docker compose down
git pull --ff-only
docker compose up --build -d
```

`docker compose down` preserves named volumes. Do not add `--volumes` unless
you intend to delete the database and review media.

## Optional LLM narration

The deterministic narrator requires no network or key. To enable an
OpenAI-compatible provider:

```bash
read -r -s SONG_EVAL_LLM_API_KEY
printf '%s' "$SONG_EVAL_LLM_API_KEY" > secrets/llm_api_key.txt
unset SONG_EVAL_LLM_API_KEY
chmod 600 secrets/llm_api_key.txt

# Configure SONG_EVAL_LLM_BASE_URL and SONG_EVAL_LLM_MODEL in .env.
docker compose -f compose.yaml -f compose.llm.yaml up --build -d
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
