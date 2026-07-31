from __future__ import annotations

import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyloudnorm as pyln
import soundfile as sf

from .audio import load_audio
from .enums import ComparisonOutcome, CraftAttribute
from .models import (
    ArtifactReview,
    DifferenceHotspot,
    ListeningResponse,
    ListeningSession,
    ListeningStimulus,
    ListeningStimulusSecret,
    ListeningTrial,
    ListeningTrialSecret,
    ListeningValidation,
    OrdinalObservation,
    PairwiseComparison,
    ProjectReviewPacket,
    StoredListeningBundle,
)


@dataclass(frozen=True)
class BlindBundle:
    session: ListeningSession
    stimuli: tuple[ListeningStimulusSecret, ...]
    trials: tuple[ListeningTrialSecret, ...]

    def public_payload(self) -> dict:
        """Return only opaque references; no title, duration, batch or lineage."""
        return {
            "session_id": self.session.id,
            "blinded": True,
            "trials": [
                {
                    "trial_id": trial.id,
                    "left": {
                        "sample_id": trial.left.sample_id,
                        "media_url": trial.left.media_path,
                        "clip_start_s": 0.0,
                        "clip_end_s": trial.left.end_s - trial.left.start_s,
                    },
                    "right": {
                        "sample_id": trial.right.sample_id,
                        "media_url": trial.right.media_path,
                        "clip_start_s": 0.0,
                        "clip_end_s": trial.right.end_s - trial.right.start_s,
                    },
                }
                for trial in self.session.trials
            ],
        }

    def to_record(
        self,
        *,
        run_id: str,
        media_files: dict[str, Path],
    ) -> StoredListeningBundle:
        return StoredListeningBundle(
            id=self.session.id,
            project_id=self.session.project_id,
            run_id=run_id,
            session=self.session,
            stimuli=self.stimuli,
            trial_secrets=self.trials,
            media_files={
                sample_id: str(path.resolve())
                for sample_id, path in media_files.items()
            },
        )

    @classmethod
    def from_record(cls, record: StoredListeningBundle) -> BlindBundle:
        return cls(
            session=record.session,
            stimuli=record.stimuli,
            trials=record.trial_secrets,
        )


def _token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _stimulus(
    *,
    artifact_id: str,
    source_path: str,
    start_s: float,
    end_s: float,
    gain_variant_db: float = 0.0,
) -> tuple[ListeningStimulus, ListeningStimulusSecret]:
    sample_id = _token("sample")
    public = ListeningStimulus(
        sample_id=sample_id,
        media_path=f"/media/{sample_id}",
        start_s=start_s,
        end_s=end_s,
    )
    secret = ListeningStimulusSecret(
        sample_id=sample_id,
        artifact_id=artifact_id,
        source_path=source_path,
        start_s=start_s,
        end_s=end_s,
        gain_variant_db=gain_variant_db,
    )
    return public, secret


def build_blind_session(
    *,
    project_id: str,
    comparisons: Iterable[PairwiseComparison],
    artifact_paths: dict[str, str],
    include_probes: bool = True,
    max_hotspots_per_pair: int = 2,
) -> BlindBundle:
    if max_hotspots_per_pair < 1:
        raise ValueError("max_hotspots_per_pair must be at least 1")
    trials: list[ListeningTrial] = []
    stimuli: list[ListeningStimulusSecret] = []
    secrets_for_trials: list[ListeningTrialSecret] = []
    first_real: tuple[str, str, str, DifferenceHotspot] | None = None
    for comparison in comparisons:
        path_a = artifact_paths[comparison.artifact_a_id]
        path_b = artifact_paths[comparison.artifact_b_id]
        for hotspot in comparison.hotspots[:max_hotspots_per_pair]:
            pair_key = _token("pair")
            first_real = first_real or (
                comparison.artifact_a_id,
                comparison.artifact_b_id,
                path_a,
                hotspot,
            )
            for order in ("ab", "ba"):
                public_a, secret_a = _stimulus(
                    artifact_id=comparison.artifact_a_id,
                    source_path=path_a,
                    start_s=hotspot.a_start_s,
                    end_s=hotspot.a_end_s,
                )
                public_b, secret_b = _stimulus(
                    artifact_id=comparison.artifact_b_id,
                    source_path=path_b,
                    start_s=hotspot.b_start_s,
                    end_s=hotspot.b_end_s,
                )
                stimuli.extend((secret_a, secret_b))
                left, right = (
                    (public_a, public_b) if order == "ab" else (public_b, public_a)
                )
                trial = ListeningTrial(
                    left=left,
                    right=right,
                    pair_key=pair_key,
                    order=order,
                    probe_type="real",
                )
                trials.append(trial)
                secrets_for_trials.append(
                    ListeningTrialSecret(
                        trial_id=trial.id,
                        canonical_a_artifact_id=comparison.artifact_a_id,
                        canonical_b_artifact_id=comparison.artifact_b_id,
                        left_artifact_id=(
                            comparison.artifact_a_id
                            if order == "ab"
                            else comparison.artifact_b_id
                        ),
                        right_artifact_id=(
                            comparison.artifact_b_id
                            if order == "ab"
                            else comparison.artifact_a_id
                        ),
                    )
                )
    if include_probes and first_real:
        artifact_a, _, path_a, hotspot = first_real
        for probe_type, gain_right in (
            ("a_vs_a", 0.0),
            ("loudness_variant", 6.0),
        ):
            left, left_secret = _stimulus(
                artifact_id=artifact_a,
                source_path=path_a,
                start_s=hotspot.a_start_s,
                end_s=hotspot.a_end_s,
            )
            right, right_secret = _stimulus(
                artifact_id=artifact_a,
                source_path=path_a,
                start_s=hotspot.a_start_s,
                end_s=hotspot.a_end_s,
                gain_variant_db=gain_right,
            )
            stimuli.extend((left_secret, right_secret))
            trial = ListeningTrial(
                left=left,
                right=right,
                pair_key=f"probe:{probe_type}",
                order="ab",
                probe_type=probe_type,
            )
            trials.append(trial)
            secrets_for_trials.append(
                ListeningTrialSecret(
                    trial_id=trial.id,
                    canonical_a_artifact_id=artifact_a,
                    canonical_b_artifact_id=artifact_a,
                    left_artifact_id=artifact_a,
                    right_artifact_id=artifact_a,
                )
            )
    if not any(trial.probe_type == "real" for trial in trials):
        raise ValueError(
            "blind listening requires at least two candidates with a comparable "
            "audio hotspot"
        )
    session = ListeningSession(project_id=project_id, trials=tuple(trials))
    return BlindBundle(
        session=session,
        stimuli=tuple(stimuli),
        trials=tuple(secrets_for_trials),
    )


def _canonical_outcome(
    response: ListeningResponse,
    trial: ListeningTrial,
) -> ComparisonOutcome:
    if response.outcome in {ComparisonOutcome.TIE, ComparisonOutcome.NA}:
        return response.outcome
    chose_left = response.outcome == ComparisonOutcome.A
    if trial.order == "ab":
        return ComparisonOutcome.A if chose_left else ComparisonOutcome.B
    return ComparisonOutcome.B if chose_left else ComparisonOutcome.A


def validate_listening_session(
    bundle: BlindBundle,
    responses: Iterable[ListeningResponse],
) -> ListeningValidation:
    responses = tuple(responses)
    response_map = {response.trial_id: response for response in responses}
    failures: list[str] = []
    if not any(trial.probe_type == "real" for trial in bundle.session.trials):
        failures.append("blind-listening session contains no real comparison trials")
    if len(response_map) != len(responses):
        failures.append("duplicate trial responses are not allowed")
    expected_ids = {trial.id for trial in bundle.session.trials}
    unknown_ids = sorted(set(response_map) - expected_ids)
    if unknown_ids:
        failures.append(f"responses reference unknown trials: {unknown_ids}")
    pair_values: dict[str, list[ComparisonOutcome]] = {}
    for trial in bundle.session.trials:
        response = response_map.get(trial.id)
        if response is None:
            failures.append(f"missing response for trial {trial.id}")
            continue
        canonical = _canonical_outcome(response, trial)
        if trial.probe_type == "a_vs_a" and canonical != ComparisonOutcome.TIE:
            failures.append("A-vs-A probe was not rated tie")
        if (
            trial.probe_type == "loudness_variant"
            and canonical != ComparisonOutcome.TIE
        ):
            failures.append("loudness-variant calibration probe was not rated tie")
        if trial.probe_type == "real":
            pair_values.setdefault(trial.pair_key, []).append(canonical)
    outcomes: dict[str, ComparisonOutcome] = {}
    for pair_key, values in pair_values.items():
        actual = [value for value in values if value != ComparisonOutcome.NA]
        if not actual:
            outcomes[pair_key] = ComparisonOutcome.NA
        elif all(value == ComparisonOutcome.TIE for value in actual):
            outcomes[pair_key] = ComparisonOutcome.TIE
        else:
            directional = {
                value
                for value in actual
                if value in {ComparisonOutcome.A, ComparisonOutcome.B}
            }
            if len(directional) > 1:
                # Position-order reversal: downgrade instead of averaging.
                outcomes[pair_key] = ComparisonOutcome.TIE
            elif ComparisonOutcome.TIE in actual:
                outcomes[pair_key] = ComparisonOutcome.TIE
            else:
                outcomes[pair_key] = next(iter(directional))
    return ListeningValidation(
        valid=not failures,
        failures=tuple(failures),
        pair_outcomes=outcomes,
    )


def build_listening_review(
    bundle: BlindBundle,
    responses: Iterable[ListeningResponse],
) -> tuple[ListeningValidation, ProjectReviewPacket]:
    """Turn a valid opaque comparison round into timestamped Craft evidence."""
    responses = tuple(responses)
    validation = validate_listening_session(bundle, responses)
    if not validation.valid:
        return validation, ProjectReviewPacket(
            project_id=bundle.session.project_id,
            listening_round_valid=False,
        )
    responses_by_trial = {response.trial_id: response for response in responses}
    trial_secrets = {trial.trial_id: trial for trial in bundle.trials}
    stimulus_secrets = {item.sample_id: item for item in bundle.stimuli}
    values: dict[
        tuple[str, CraftAttribute],
        list[tuple[int, float, float, str, str | None]],
    ] = {}
    for pair_key, pair_outcome in validation.pair_outcomes.items():
        if pair_outcome == ComparisonOutcome.NA:
            continue
        trials = [
            trial
            for trial in bundle.session.trials
            if trial.probe_type == "real" and trial.pair_key == pair_key
        ]
        if not trials:
            continue
        first_trial = trials[0]
        secret = trial_secrets[first_trial.id]
        response_group = [
            responses_by_trial[trial.id]
            for trial in trials
            if trial.id in responses_by_trial
        ]
        tags = {tag for response in response_group for tag in response.reason_tags} or {
            CraftAttribute.OVERALL_PREFERENCE
        }
        comments = " | ".join(
            response.comment.strip()
            for response in response_group
            if response.comment and response.comment.strip()
        )
        artifact_windows: dict[str, tuple[float, float]] = {}
        for sample in (first_trial.left, first_trial.right):
            stimulus = stimulus_secrets[sample.sample_id]
            artifact_windows[stimulus.artifact_id] = (
                stimulus.start_s,
                stimulus.end_s,
            )
        if pair_outcome == ComparisonOutcome.A:
            scores = {
                secret.canonical_a_artifact_id: 3,
                secret.canonical_b_artifact_id: 1,
            }
        elif pair_outcome == ComparisonOutcome.B:
            scores = {
                secret.canonical_a_artifact_id: 1,
                secret.canonical_b_artifact_id: 3,
            }
        else:
            scores = {
                secret.canonical_a_artifact_id: 2,
                secret.canonical_b_artifact_id: 2,
            }
        for artifact_id, score in scores.items():
            start_s, end_s = artifact_windows[artifact_id]
            for tag in tags:
                values.setdefault((artifact_id, tag), []).append(
                    (score, start_s, end_s, pair_key, comments or None)
                )
    reviews: dict[str, list[OrdinalObservation]] = {}
    for (artifact_id, tag), evidence_values in values.items():
        wins = sum(item[0] == 3 for item in evidence_values)
        ties = sum(item[0] == 2 for item in evidence_values)
        losses = sum(item[0] == 1 for item in evidence_values)
        preference_ratio = (wins + 0.5 * ties) / len(evidence_values)
        score = int(round(1 + 2 * preference_ratio))
        windows = ", ".join(f"{item[1]:.2f}-{item[2]:.2f}s" for item in evidence_values)
        comments = sorted({item[4] for item in evidence_values if item[4] is not None})
        evidence = (
            f"valid blinded comparisons={len(evidence_values)}; "
            f"wins/ties/losses={wins}/{ties}/{losses}; "
            f"audition windows={windows}"
        )
        if comments:
            evidence += f"; listener notes={' | '.join(comments)}"
        reviews.setdefault(artifact_id, []).append(
            OrdinalObservation(
                criterion=tag.value,
                value=score,
                evidence=evidence,
            )
        )
    return validation, ProjectReviewPacket(
        project_id=bundle.session.project_id,
        artifact_reviews=[
            ArtifactReview(
                artifact_id=artifact_id,
                craft_observations=tuple(
                    sorted(observations, key=lambda item: item.criterion)
                ),
            )
            for artifact_id, observations in sorted(reviews.items())
        ],
        listening_round_valid=True,
    )


def merge_project_reviews(
    base: ProjectReviewPacket,
    evidence: ProjectReviewPacket,
) -> ProjectReviewPacket:
    if base.project_id != evidence.project_id:
        raise ValueError("cannot merge reviews from different projects")
    base_by_artifact = {item.artifact_id: item for item in base.artifact_reviews}
    evidence_by_artifact = {
        item.artifact_id: item for item in evidence.artifact_reviews
    }
    merged_reviews: list[ArtifactReview] = []
    for artifact_id in sorted(set(base_by_artifact) | set(evidence_by_artifact)):
        manual = base_by_artifact.get(artifact_id)
        automatic = evidence_by_artifact.get(artifact_id)
        requirement_observations = {
            **(automatic.requirement_observations if automatic is not None else {}),
            **(manual.requirement_observations if manual is not None else {}),
        }
        craft_by_criterion = {
            item.criterion: item
            for item in (automatic.craft_observations if automatic is not None else ())
        }
        if manual is not None:
            craft_by_criterion.update(
                {item.criterion: item for item in manual.craft_observations}
            )
        merged_reviews.append(
            ArtifactReview(
                artifact_id=artifact_id,
                requirement_observations=requirement_observations,
                craft_observations=tuple(
                    craft_by_criterion[key] for key in sorted(craft_by_criterion)
                ),
                release_actions=(
                    manual.release_actions
                    if manual is not None and manual.release_actions
                    else automatic.release_actions
                    if automatic is not None
                    else ()
                ),
                target_windows={
                    **(automatic.target_windows if automatic is not None else {}),
                    **(manual.target_windows if manual is not None else {}),
                },
                technical_confirmations={
                    **(
                        automatic.technical_confirmations
                        if automatic is not None
                        else {}
                    ),
                    **(manual.technical_confirmations if manual is not None else {}),
                },
            )
        )
    lyric_by_artifact = {
        item.artifact_id: item
        for item in [*evidence.lyric_analyses, *base.lyric_analyses]
    }
    target_observations = {
        artifact_id: {
            **evidence.target_requirement_observations.get(artifact_id, {}),
            **base.target_requirement_observations.get(artifact_id, {}),
        }
        for artifact_id in {
            *evidence.target_requirement_observations,
            *base.target_requirement_observations,
        }
    }
    null_baselines = {
        directive_id: {
            **evidence.null_baselines.get(directive_id, {}),
            **base.null_baselines.get(directive_id, {}),
        }
        for directive_id in {*evidence.null_baselines, *base.null_baselines}
    }
    batch_variance_floors = {
        directive_id: {
            **evidence.batch_variance_floors.get(directive_id, {}),
            **base.batch_variance_floors.get(directive_id, {}),
        }
        for directive_id in {
            *evidence.batch_variance_floors,
            *base.batch_variance_floors,
        }
    }
    return base.model_copy(
        update={
            "artifact_reviews": merged_reviews,
            "target_brief_id": base.target_brief_id or evidence.target_brief_id,
            "target_requirement_observations": target_observations,
            "listening_round_valid": (
                base.listening_round_valid or evidence.listening_round_valid
            ),
            "cross_brief_target_compliance_complete": (
                base.cross_brief_target_compliance_complete
                or evidence.cross_brief_target_compliance_complete
            ),
            "null_baselines": null_baselines,
            "batch_variance_floors": batch_variance_floors,
            "lyric_analyses": list(lyric_by_artifact.values()),
        }
    )


def materialize_blind_media(
    bundle: BlindBundle,
    output_dir: str | Path,
) -> dict[str, Path]:
    """Create temporary blind-listening clips.

    The original files are read-only. Real trials are pairwise loudness-matched
    by attenuating only the louder stimulus. Loudness-variant calibration trials
    preserve their relative level difference and apply only shared attenuation
    to avoid clipping. No path uses a limiter or segment-wise dynamics.
    """
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    by_id = {item.sample_id: item for item in bundle.stimuli}
    outputs: dict[str, Path] = {}
    for trial in bundle.session.trials:
        secrets_for_pair = (by_id[trial.left.sample_id], by_id[trial.right.sample_id])
        decoded = []
        for secret in secrets_for_pair:
            audio = load_audio(secret.source_path)
            start = max(0, int(round(secret.start_s * audio.sample_rate_hz)))
            end = min(
                len(audio.samples),
                int(round(secret.end_s * audio.sample_rate_hz)),
            )
            if end <= start:
                raise ValueError(
                    f"stimulus {secret.sample_id} has no decodable frames "
                    "inside its requested window"
                )
            values = np.asarray(audio.samples[start:end], dtype=np.float64)
            values = values * (10 ** (secret.gain_variant_db / 20.0))
            decoded.append((values, audio.sample_rate_hz))
        if decoded[0][1] != decoded[1][1]:
            raise ValueError(
                "stimuli sample rates must match for blind materialization"
            )
        sample_rate = decoded[0][1]
        levels: list[float | None] = []
        meter = pyln.Meter(sample_rate)
        for values, _ in decoded:
            try:
                level = float(meter.integrated_loudness(values))
                levels.append(level if np.isfinite(level) else None)
            except (ValueError, ZeroDivisionError):
                levels.append(None)
        if trial.probe_type == "loudness_variant":
            common_attenuation_db = -max(
                0.0,
                *(secret.gain_variant_db for secret in secrets_for_pair),
            )
            gains = [common_attenuation_db, common_attenuation_db]
        elif all(level is not None for level in levels):
            target = min(float(level) for level in levels if level is not None)
            gains = [min(0.0, target - float(level)) for level in levels]
        else:
            rms = [float(np.sqrt(np.mean(values**2) + 1e-12)) for values, _ in decoded]
            target = min(rms)
            gains = [
                min(0.0, 20.0 * np.log10(target / max(level, 1e-12))) for level in rms
            ]
        for secret, (values, _), gain_db in zip(
            secrets_for_pair,
            decoded,
            gains,
            strict=True,
        ):
            output = destination / f"{secret.sample_id}.wav"
            matched = values * (10 ** (gain_db / 20.0))
            sf.write(output, matched.astype(np.float32), sample_rate, subtype="PCM_16")
            outputs[secret.sample_id] = output
    return outputs
