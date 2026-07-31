# Requirements and verification matrix

This matrix maps the frozen v0.1 invariants and the v0.2 functional-closure
requirements to implementation and tests. A green command alone is not
completion evidence; the named tests must exercise the corresponding invariant.

| Requirement | Implementation | Verification |
|---|---|---|
| Immutable Brief, policy, raw metadata | `models.py`, `db.py` | `test_models.py`, `test_db.py` |
| Separate platform/measured duration | `ReleaseArtifact`, importer | `test_importers.py` |
| Unknown remains unknown | typed optional fields, no inferred defaults | `test_importers.py` |
| Brief → Event → Take → Artifact | models and manifest validation | `test_db.py`, Spring E2E |
| Captured vs inferred source relationship remains explicit | `SourceStateAssessment` | Spring E2E |
| Typed acyclic Artifact DAG | `lineage.py`, SQLite edge guard | `test_lineage.py` |
| Deterministic Crop inheritance only | `verify_deterministic_crop`, `common_craft_regions` | `test_lineage.py`, real-audio test |
| Lossy/unknown acquisition degradation | `AcquisitionPath`, analyzer | `test_audio.py`, Spring E2E |
| Original audio never normalized in place | gain calculation and opaque stimuli | `test_audio.py` |
| Independent feature families | chroma, onset flux, energy envelope | `test_audio.py`, `test_reference.py` |
| Same URL with changed content creates a revision | `AcquisitionSnapshot`, importer | `test_importers.py` |
| Approximate repeats without invented section names | `StructureSegment` | `test_audio.py`, Spring E2E |
| Timestamped hotspots | `compare_feature_series` | `test_audio.py`, Spring E2E |
| Reference risk preflight | `preflight_reference` | `test_reference.py`, Spring E2E |
| Exact/melody needs two families, controls, and comparable acquisition | retention evaluator | `test_reference.py` |
| Structural gesture avoids similarity voting | gesture evaluator | `test_reference.py` |
| Blind metadata removal and probes | `listening.py` | `test_listening.py` |
| Common-mode defects | `promote_common_mode_defects` | `test_evaluation.py` |
| T1/T2/T3 and protected edit feasibility | evaluator | `test_evaluation.py` |
| No total score; lexical policy | recommender | `test_recommendation.py`, source scan |
| Zero survivors does not fall back | recommender | `test_recommendation.py` |
| Missing policy/evidence abstains | recommender | `test_recommendation.py`, Spring E2E |
| Cross-Brief guard | recommender | `test_recommendation.py` |
| Dual cross-Brief Compliance contexts | `CandidateAssessment` | `test_recommendation.py`, Spring E2E |
| User override does not rewrite the policy result | recommender | `test_recommendation.py` |
| B-route keeps v2 parent and forbids a second Sample | `migration.py` | `test_migration.py`, CLI acceptance |
| LLM cannot invent measurements by design | evidence packet and system contract | `test_llm.py` |
| CLI and HTTP API | `cli.py`, `api.py` | `test_cli.py`, `test_api.py` |
| Real 《春》 dataset | `examples/spring` | `test_spring_e2e.py`, CLI acceptance |
| One-step Suno URL/snapshot/local intake and initial report | `importers.py`, `cli.py intake` | `test_importers.py`, `test_cli.py`, Seventeen E2E |
| Suno `metadata.type=gen/edit_crop` mapping | `captured_task` | `test_importers.py`, Seventeen E2E |
| Playlist clips are not URL revisions of one another | acquisition snapshot platform scope | `test_importers.py` |
| Explicit hidden Crop parent and measured verification | `ParentDeclaration`, intake verifier | `test_importers.py` |
| Stable byte-for-byte local cache; no transcoding | intake downloader/cache | `test_importers.py`, `test_cli.py` |
| Active-audio ending boundary remains a listening question | `EndingDiagnostics`, analyzer guard | `test_audio.py`, `test_evaluation.py`, Seventeen E2E |
| JSON and optional local MLX-Whisper lyric locator | `lyrics.py`, `locate-lyrics` | `test_lyrics.py`, `test_cli.py` |
| Persistent opaque listening after service restart | stored bundle and media route | `test_api.py` |
| Probes remain blinded in the public payload | `BlindBundle.public_payload` | `test_listening.py` |
| Semantic Craft reasons become per-candidate evidence | `build_listening_review` | `test_listening.py`, Seventeen E2E |
| Non-blind Compliance and ending confirmation form | review context/page and stored review | `test_api.py`, `test_evaluation.py` |
| Explicit policy confirmation, never inferred | CLI/API policy declaration | `test_cli.py`, `test_api.py` |
| Alternate is ranked rather than input-ordered | recommender alternate selection | `test_recommendation.py`, Seventeen E2E |
| Reference registration never attaches it to generation | `reference_workflow.py` | `test_reference_workflow.py`, API/CLI tests |
| Pro non-Studio structural gesture uses target + Song Editor | `recommend_suno_workflow` | `test_migration.py`, API/CLI tests |
| Exact/melody route abstains without retention proof | `recommend_suno_workflow` | `test_migration.py` |
| Real 《十七》 preference loop | local snapshot/audio replay | `test_seventeen_e2e.py` |
| Production overview uses current project/report evidence | workspace context + `web/app.js` | `test_api.py`, browser desktop/mobile acceptance |
| Real audio waveforms, not demo geometry | Web Audio decode + candidate/stimulus media routes | `test_api.py`, browser playback/seek acceptance |
| Named review keeps human-only boundaries explicit | gated ending control + stored review/policy | `test_api.py`, browser form acceptance |
| Reference target survives registration and refresh | `PreservationDirective.target_artifact_id` + legacy ID fallback | `test_reference_workflow.py`, browser plan acceptance |
| Blind drafts survive navigation/reload | opaque session payload + `sessionStorage` | `test_api.py`, browser draft-recovery acceptance |
| Missing UI evidence remains absent or labelled | workspace aggregation + explicit empty states | `test_api.py`, source scan, browser accessibility snapshot |
| Zero-trial blind rounds cannot unlock recommendation | blind builder + validation + UI empty state | `test_listening.py`, `test_api.py` |
| Policy may make unverified preservation descriptive | compliance evaluation | `test_evaluation.py` |
| Final human choice does not rewrite policy output | release-decision record | `test_api.py`, `test_cli.py` |
| Lyric T1 requires explicit human confirmation | lyric confirmation API/CLI | `test_api.py`, `test_lyrics.py` |
| Shared evidence packages do not expose local paths | redacted export | `test_api.py` |
| Local mode remains loopback-only by default | `serve` remote opt-in guard | `test_cli.py` |
| Remote mode requires exact Host and administrator auth | CLI guard + HTTP Basic middleware | `test_cli.py`, `test_api.py`, container smoke test |
| Server secrets may be file-mounted | auth password and LLM API key file readers | `test_cli.py`, `test_api.py`, Compose validation |
| Reproducible server audio stack | locked uv image + FFmpeg/libsndfile Dockerfile | package build, container smoke test |
| Responsive and reduced-motion presentation | `web/app.css` media queries | mobile browser acceptance, source inspection |
