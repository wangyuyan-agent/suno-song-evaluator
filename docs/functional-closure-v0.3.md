# Functional closure v0.3

Version 0.3 closes the gap between the existing analysis API and a page that a
single trusted user can operate without writing a manifest or running a CLI
command.

## User-visible contract

1. `/` is an operational intake workspace, not a static project index.
2. A public Suno song or playlist is previewed before the user explicitly
   selects clips and creates a project. Preview and intake never authenticate to
   Suno, generate, publish, or spend credits.
3. Local WAV, MP3, M4A, FLAC, OGG, and AAC files can be uploaded directly. The
   server validates the decoded audio and caches the original bytes without
   transcoding.
4. Saved Suno snapshots and complete trusted manifests remain available as an
   advanced reproducible path.
5. Intake runs as a persistent job with queued, running, succeeded, failed, and
   canceled states. Jobs recover after process restart and failed analysis can
   be retried without re-uploading or duplicating immutable project records.
6. A successful initial analysis has both its immutable database record and
   atomically materialized JSON/Markdown reports. A file-write failure is
   repaired on retry.
7. Completed projects open the existing overview, blind A/B, named review,
   evidence export, and reference-to-Suno planning workflows.

## Safety and resource contract

- JSON requests are limited to 8 MiB; multipart uploads have count, per-file,
  aggregate, pending-storage, and duration limits.
- Suno URLs and every redirect remain on Suno-controlled hosts. Clip count,
  downloaded file size, and duration use the same bounded intake policy.
- Public API clients cannot submit server filesystem paths. Upload staging uses
  server-generated identifiers under a dedicated root, and responses redact
  local paths.
- State-changing browser requests reject cross-site fetches and mismatched
  origins. Remote mode still requires exact Host validation, HTTPS termination,
  and the single administrator account.
- Failed/canceled upload staging is retained only when retry needs it and can be
  explicitly cleaned. Successful staging is removed after byte-exact media has
  been imported.
- One project ID owns one durable intake lifecycle. Failed or canceled work is
  retried in place or explicitly deleted; abandoning an already imported but
  unanalyzed project requires the explicit `discard_partial_project=true`
  delete option. Succeeded projects remain immutable.
- Media/report directories and generated brief/policy IDs use a readable slug
  plus the full SHA-256 of the original project ID, preventing case-folding or
  punctuation-normalization collisions without changing the public project ID.

## Persistence and deployment

Native mode stores media, staging, and reports beside the selected database.
The container stores all of them under `/data`, so the existing
`song_eval_data` volume is the complete application backup boundary. The
optional `/library` mount remains read-only and outside that volume.
Multipart spooling uses `/data/song-eval-uploads` instead of the bounded `/tmp`
tmpfs, so the declared 512 MiB per-file and 2 GiB per-request limits remain
reachable in the hardened Compose deployment.

## Deliberate non-goals

- no Suno account login or private-library integration;
- no generation, publication, credit use, or external post-production;
- no multi-user registration, RBAC, MFA, brute-force protection, or tenant
  isolation;
- no LLM access to raw audio and no LLM-authored measurements;
- no inferred release winner until the existing policy and human-evidence gates
  are satisfied.
