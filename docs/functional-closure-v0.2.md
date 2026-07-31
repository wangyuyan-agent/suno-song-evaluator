# Functional closure v0.2

This document closes the functional gaps found while evaluating the real
《春》 and 《十七》 projects. It does not specify visual product design.

## End-to-end contract

1. Intake accepts a Suno share/playlist URL, a saved public snapshot, or
   repeated local audio files.
2. Captured audio is cached byte-for-byte, hashed, probed, and imported before
   the initial report is written.
3. Unknown lineage remains unknown. A hidden Crop parent can be explicitly
   declared and is promoted to deterministic only after sample verification.
4. Automatic analysis reports structure, three independent feature families,
   timestamped hotspots, acquisition limitations, and ending-boundary evidence.
5. ASR is a locator only. Existing Whisper JSON works without an optional
   dependency; Apple Silicon users may install the local MLX-Whisper extra.
6. Blind listening persists across service restarts. Candidate identity,
   calibration type, title, duration, and lineage are absent from the public
   payload.
7. A valid round records criterion-specific Craft evidence. The named review
   then captures Compliance and technical confirmations without contaminating
   the blind round.
8. A formal recommendation requires an explicitly confirmed lexical policy.
   No total score is introduced.
9. A reference segment is registered as evidence, not silently attached to a
   generation. The action planner either emits a bounded Suno workflow or
   abstains.
10. A zero-comparison round is invalid and cannot satisfy the subjective gate.
11. A final human choice is stored separately from the immutable policy result.
12. A burden-bearing lyric T1 requires an explicit human-confirmation command
    or API request after transcript localization.

## Current Suno capability snapshot

Verified on 2026-07-31 against Suno's official help:

- Pro and Premier subscribers have Replace Section:
  <https://help.suno.com/en/articles/3271873>
- Song Editor exposes Replace Section outside Studio:
  <https://help.suno.com/en/articles/6141505>
- Studio is Premier-only:
  <https://help.suno.com/en/articles/7940161>

Therefore the default personal workflow for a structural gesture on the
user's Pro plan is:

- keep the selected target song as the edit parent;
- do not attach the 16-second reference as a Sample;
- use Library/Create → More Actions → Edit → Replace Section;
- select clean phrase/bar boundaries around the local transition;
- keep frozen lyrics unchanged;
- generate two versions per batch and at most two batches;
- rerun the evaluator on every Whole Song result;
- retain the target fallback if all versions fail.

The capability snapshot is evidence, not a permanent platform guarantee. A
future release must re-verify it before changing the workflow.

## Real acceptance

### 《春》

- Lossless private files remain outside Git.
- Public CDN copies are cached outside Git for replay.
- Full v1.6 → Crop sample verification must pass the `0.995` correlation
  threshold. The measured lag is the source offset, so exact middle-region
  crops are valid.
- Missing policy or blind evidence must still abstain.
- Structural-gesture planning must target v2.0, require no Studio, attach no
  Sample, and preserve the fallback.

### 《十七》

- The saved playlist snapshot and five cached public MP3s are replayed without
  an ad hoc analysis script.
- Crop must import as `edit_crop`, not `unknown`.
- The shortened active ending must be surfaced for human confirmation.
- Valid preference evidence must be capable of explaining:
  - Crop first for warm, full sound and melody;
  - the user-described 4:33 v9 take (measured/displayed as 4:34) second for
    catchiness and final choral harmony;
  - the 4:17 v9.3 take third.
- The expected winner and alternate come from the declared evidence, never
  from hard-coded artifact IDs or input order.

## Non-goals and safety boundaries

- no Suno generation, upload, publication, or credit consumption;
- no external post-production;
- no hidden-slider inference;
- no LLM-authored measurements;
- no automatic wrong-lyric or hard-cut verdict from weak evidence;
- no global preference model trained from one user's project;
- no visual Design phase before functional acceptance.
