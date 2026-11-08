# Changelog

## 0.4.0 - 2026-11-08

- Added explicit passwordless Tailscale Serve mode with an exact HTTPS origin,
  Host validation, and cross-site write protection.
- Added standalone `compose.tailnet.yaml`, publishing only to host loopback and
  retaining the existing project name and data volume. It does not mount a Web
  password. Existing authenticated Compose deployments remain supported.
- Kept Basic Auth as the default requirement for remote binding; passwordless
  mode requires explicit configuration and cannot be mixed with Basic Auth.

## 0.3.0 - 2026-07-31

- Added first-class Web intake for Suno public links, local audio uploads, saved
  Suno snapshots, and trusted manifests.
- Added persistent background jobs with progress, cancellation, retry, restart
  recovery, and bounded cleanup.
- Added byte-exact upload staging, actual audio validation, clip/file/duration
  quotas, same-origin write protection, and request-body limits.
- Made initial report materialization atomic and retryable without re-uploading
  audio or duplicating immutable project records.
- Added collision-resistant internal project keys, orphan-staging recovery, and
  an explicit two-step path for abandoning an imported but unanalyzed project.
- Added a Claude Design-guided responsive intake workspace for desktop and
  mobile while preserving the no-generation and no-fabricated-evidence rules.

## 0.2.1 - 2026-07-31

- Added a loopback-only Docker Compose overlay for Tailscale Serve.
- Added isolated Caddy smoke testing without public port exposure.
- Added Linux state preparation and credential-safe deployment verification.
- Expanded ignore rules for local audio, databases, environment files, and
  private key material.

## 0.2.0 - 2026-07-31

Initial public release.

- Evidence-first Suno candidate intake, analysis, lineage, and reporting.
- Restart-safe blind A/B sessions and named human review.
- Premium Minimalism project, waveform, review, and reference-planning UI.
- Capability-aware Suno Pro non-Studio Replace Section planning.
- Optional evidence-only OpenAI-compatible narration.
- Loopback-safe local mode and authenticated Docker Compose server deployment.
- Locked `uv` dependencies, FFmpeg-enabled image, CI, and security guidance.
