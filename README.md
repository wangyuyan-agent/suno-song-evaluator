# Suno Song Evaluator

[![CI](https://github.com/wangyuyan-agent/suno-song-evaluator/actions/workflows/ci.yml/badge.svg)](https://github.com/wangyuyan-agent/suno-song-evaluator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An evidence-first local tool for comparing Suno song candidates. It keeps
creative requirements, generation lineage, objective audio evidence, blind
listening, and release decisions separate.

The tool does **not** calculate a total score, infer hidden Suno sliders, claim
that an LLM heard audio, generate music, publish songs, or perform external
post-production.

## Current scope

- immutable, versioned Briefs and raw platform payloads;
- Source → GenerationEvent → Take → ReleaseArtifact data model;
- typed Artifact DAG with deterministic Crop verification;
- WAV/MP3/M4A/FLAC probing and content hashes;
- technical diagnostics, explicit active-audio ending checks, and
  loudness-matching inputs;
- structure boundaries, repeat groups, and timestamped pairwise hotspots;
- independent pitch/harmony, rhythm/onset, and energy/structure features;
- reference preflight and preservation directives;
- restart-safe blind A/B sessions with A-vs-A, loudness, and position-order
  probes;
- a functional local review page that records warmth/fullness, catchiness,
  vocal identity, arrangement/harmony development, lyric delivery, ending
  completeness, timestamps, and listener notes;
- a production local workspace with project overview, real decoded waveforms,
  blind A/B listening, named review, evidence export, and reference-to-plan
  screens;
- optional local MLX-Whisper transcription or existing Whisper JSON ingestion;
- separate Compliance, Craft, Release Readiness, and Distinctiveness views;
- T1/T2/T3 and common-mode failure handling;
- user-declared, lexical release recommendation with explicit abstention;
- CLI, FastAPI, SQLite, deterministic reports, and optional
  OpenAI-compatible LLM narration;
- an authenticated Docker Compose server deployment with exact Host validation,
  Caddy automatic HTTPS, secret files, and an unexposed application port;
- a Tailscale-only Compose overlay that binds the app to host loopback and uses
  Tailscale Serve for tailnet-scoped HTTPS without starting Caddy;
- a capability-aware Suno Pro, non-Studio structural-gesture plan that keeps
  the selected target as the edit parent and never silently reattaches the
  reference as a Sample.

The accepted Claude Design prototype is preserved as design evidence, while
the production UI is now wired to the FastAPI runtime and local SQLite data.
See [`docs/visual-design-v0.3.md`](docs/visual-design-v0.3.md) for the design
contract, fidelity ledger, and implementation boundary.

## Development

Python dependencies are locked in `uv.lock` and installed by `uv`; the project
does not maintain a parallel `requirements.txt`. `ffmpeg` and `ffprobe` are
also required when SoundFile cannot decode or inspect a compressed input such
as M4A. On macOS, install them with `brew install ffmpeg`.

```bash
uv sync --extra dev
uv run ruff format --check src tests
uv run ruff check src tests
uv run pytest
uv run song-eval --help
```

No API key is required for analysis. Optional LLM narration reads only the
structured evidence packet and can target any OpenAI-compatible endpoint.

## Basic workflow: no hand-written manifest

```bash
# This downloads the captured public audio byte-for-byte, builds the project,
# and writes the initial JSON and Markdown analysis.
uv run song-eval intake seventeen \
  --title "《十七》候选评估" \
  --suno-url "https://suno.com/playlist/..." \
  --db seventeen.sqlite

# Offline/reproducible intake can use fetch-suno JSON and an existing media dir.
uv run song-eval intake seventeen-replay \
  --snapshot playlist-snapshot.json \
  --media-dir /path/to/cached/public-audio \
  --db seventeen-replay.sqlite

# Start the local workspace. The overview can create the opaque blind session;
# it also exposes named review, evidence export, and reference planning.
uv run song-eval serve --db seventeen.sqlite

# If an imported manifest points to audio outside the default song-eval-media
# cache, explicitly trust only that audio library root (repeat as needed).
uv run song-eval serve --db seventeen.sqlite \
  --library-root /path/to/audio-library

# Open:
# http://127.0.0.1:8765/projects/seventeen
```

`serve` is local-only by default and accepts only `127.0.0.1`, `localhost`, or
`::1`. Remote binding is rejected unless the operator explicitly enables it,
declares exact allowed Host values, and supplies a password-file-backed
administrator account. Artifact and reference paths must resolve inside the
review-media cache, the default `song-eval-media` library, or an explicit
`--library-root`; symlink escapes are rejected. Evidence export redacts
filesystem paths while preserving hashes, provenance, and measured metadata.

`blind-session RUN_ID` remains available for a CLI-first workflow. It exports a
complete offline package to `--output-dir` while persisting the database-backed
copy under the default trusted review-media root. Both UI- and CLI-created
blind-review URLs therefore survive a service restart without widening the
trusted library roots. If `serve` uses a custom `--media-dir`, pass that same
value to `blind-session`:

```bash
uv run song-eval blind-session RUN_ID \
  --db project.sqlite \
  --output-dir blind-export \
  --media-dir /path/to/review-media

uv run song-eval serve \
  --db project.sqlite \
  --media-dir /path/to/review-media
```

`intake` also accepts repeated `--audio` options for local candidates. Audio is
copied byte-for-byte into a stable cache and never normalized or post-processed.
For a Crop whose full parent is absent from the playlist, repeat
`--parent CHILD_CLIP_ID=PARENT_CLIP_ID_OR_PATH_OR_URL`. The relationship remains
unknown unless captured by the platform or explicitly declared.

## Deployment

The repository supports four execution paths:

| Path | Intended use | Network boundary |
| --- | --- | --- |
| native `uv` | local development or a host-managed service | loopback by default |
| direct `docker run` | low-level container integration | loopback-only example |
| Compose + Tailscale Serve | recommended private VPS deployment | tailnet-only HTTPS |
| Compose + Caddy | Internet-facing server with real DNS | public HTTPS on 80/443 |

For a private Linux server already joined to your tailnet:

```bash
cp .env.example .env
# Set APP_DOMAIN and SONG_EVAL_USERNAME in .env. The helper prepares the
# default ./audio and ./secrets paths.
sudo ./deploy/prepare-linux.sh
docker compose -f compose.yaml -f compose.tailscale.yaml config --quiet
docker compose -f compose.yaml -f compose.tailscale.yaml up --build -d
sudo tailscale serve --bg --https=8444 http://127.0.0.1:8765
```

The application is published only on `127.0.0.1`; Tailscale Serve provides
tailnet-only HTTPS. Use a different Serve HTTPS port when the server already
uses 443, and never enable Funnel for this topology. The base Compose stack is
the supported Caddy/Internet topology; it keeps the app port internal and
publishes only Caddy.

The Linux preparation script creates a random administrator password when one
does not exist, never prints it, and sets the numeric UID/GID permissions needed
by the non-root container. `deploy/verify-deployment.sh` checks health, the
unauthenticated boundary, and authenticated access without placing the password
in process arguments or environment variables.

The image downloads FFmpeg/ffprobe and libsndfile during build, then reproduces
Python dependencies from `uv.lock`. Authentication is deliberately a single
administrator account—there is no registration, RBAC, password recovery, MFA,
brute-force rate limiting, or multi-tenant isolation in v0.2.1.

See [`docs/deployment.md`](docs/deployment.md) for commands for all four paths,
Caddy isolation smoke tests, DNS, secrets, LLM, backup, upgrade, and threat
boundaries. See [`SECURITY.md`](SECURITY.md) before placing the service on a
network.

## Audio analysis stack

- SoundFile/libsndfile decodes supported containers;
- NumPy and SciPy calculate independent pitch/harmony, onset/rhythm, energy,
  structure, and correlation evidence;
- pyloudnorm measures loudness and calculates listening-match gain without
  modifying source files;
- FFmpeg/ffprobe inspect and decode compressed formats when SoundFile cannot.

The container downloads these system and Python dependencies at image-build
time. It does not download an ASR model at startup. Optional MLX Whisper is
Apple-Silicon-only; Linux deployments can ingest existing Whisper-compatible
JSON instead.

## LLM integration

Deterministic evidence narration is always available without a network or API
key. An optional OpenAI-compatible endpoint can be configured with
`SONG_EVAL_LLM_BASE_URL`, `SONG_EVAL_LLM_MODEL`, and either
`SONG_EVAL_LLM_API_KEY` or the safer `SONG_EVAL_LLM_API_KEY_FILE`.

For Compose, use the secret-file overlay:

```bash
docker compose -f compose.yaml -f compose.llm.yaml up --build -d
```

Only the structured evidence packet is sent. The configured key can be used
only with the server-configured base URL; callers cannot redirect it. The LLM
does not receive raw audio, cannot claim it listened, cannot create
measurements, and cannot overwrite the deterministic recommendation.

## Lyrics locator

```bash
# Reuse an existing Whisper-compatible JSON:
uv run song-eval locate-lyrics PROJECT ARTIFACT \
  --db project.sqlite \
  --transcript transcript.json

# Or install the optional local Apple Silicon backend:
uv sync --extra asr
uv run song-eval locate-lyrics PROJECT ARTIFACT \
  --db project.sqlite \
  --provider mlx-whisper \
  --language zh
```

ASR only locates lines and always requires human confirmation before a
burden-bearing lyric defect can be created:

```bash
uv run song-eval confirm-lyric-defect PROJECT ARTIFACT \
  --db project.sqlite \
  --line-index 0 \
  --description "human heard the burden-bearing lyric change" \
  --confirm
```

## Reference segment to Suno action

```bash
uv run song-eval register-reference PROJECT TARGET_ARTIFACT reference-16s.wav \
  --db project.sqlite \
  --intent structural_gesture

uv run song-eval plan-suno PROJECT \
  --db project.sqlite \
  --directive-id DIRECTIVE_ID \
  --target-artifact-id TARGET_ARTIFACT \
  --prompt "restrained bridge close, one-beat breath, immediate chorus" \
  --subscription-tier pro \
  --no-studio
```

For structural gesture intent, the Pro plan uses Song Editor → Replace Section,
keeps the target as the edit parent, generates two takes per batch, and stops
after two batches. For exact-audio or melody-rhythm intent without sufficient
retention evidence, the planner abstains instead of promising that a Sample can
be placed at an exact location. The tool never opens Suno, spends credits, or
publishes.

## Final human choice

The policy result and the user's release choice remain separate. The web
overview can record a final choice, or the CLI can persist one explicitly:

```bash
uv run song-eval record-final-choice PROJECT ARTIFACT \
  --db project.sqlite \
  --reason "preferred warmth and completed harmony ending" \
  --confirm
```

This record never rewrites the original recommendation or its evidence gaps.

## Legacy manifest and real-audio acceptance

The frozen Spring manifest is still supported. Local paths may contain
environment variables such as `${SPRING_AUDIO_DIR}`. Missing required audio
fails before records are written; `--allow-missing-audio` is only for an
intentionally incomplete lineage import.

Private/lossless audio remains outside Git. The real suite can use explicit
environment paths; a cached public-CDN Spring replay and the local Seventeen
playlist replay are detected automatically:

```bash
uv run pytest -m real_audio
```

Set `SPRING_AUDIO_DIR`, `SPRING_REFERENCE_AUDIO`, `SEVENTEEN_SNAPSHOT`, and
`SEVENTEEN_AUDIO_DIR` to run the corresponding private/local acceptance cases.

## Recommendation policy

The engine will analyze without a policy, but it will not emit a formal
recommendation until the user has declared:

- the lexical axis priority;
- the concrete Compliance floor;
- the N/A ceiling and abstention rule;
- the evidence source for axis noise thresholds.

T1 gates first. The remaining axes are compared lexically, not added.
Distinctiveness stays descriptive because “more different” is not inherently
better. Cross-Brief comparison requires both `compliance_as_generated` and
`compliance_vs_target`.

## LLM boundary

The deterministic report needs no model. Optional OpenAI-compatible narration
receives only a structured evidence packet. Configure it through the HTTP API
with:

```bash
export SONG_EVAL_LLM_API_KEY="..."
export SONG_EVAL_LLM_BASE_URL="https://provider.example/v1"
export SONG_EVAL_LLM_MODEL="model-name"
```

The model is instructed to withhold claims when evidence is absent; its prose
is never treated as an audio measurement or a hidden score.

## Interpretation boundary

Automatic analysis locates differences and checks explicit, testable
conditions. Active audio at the decoded file boundary is reported as requiring
listening, not automatically called a hard cut. Craft and burden-bearing lyric
decisions require valid human evidence. Low ASR confidence never becomes an
automatic wrong-lyric verdict.
