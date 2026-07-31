from __future__ import annotations

import json
import math
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy import signal

from .enums import EvaluationStatus
from .models import (
    AudioMetrics,
    CropVerification,
    DifferenceHotspot,
    EncodingFingerprint,
    EndingDiagnostics,
    PairwiseComparison,
    StructureAnalysis,
    StructureBoundary,
    StructureSegment,
)
from .util import dbfs, file_sha256

AUDIO_TOOL_TIMEOUT_S = 120


@dataclass(frozen=True)
class DecodedAudio:
    samples: np.ndarray
    sample_rate_hz: int

    @property
    def channels(self) -> int:
        return 1 if self.samples.ndim == 1 else int(self.samples.shape[1])

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate_hz

    @property
    def mono(self) -> np.ndarray:
        if self.samples.ndim == 1:
            return self.samples
        return np.mean(self.samples, axis=1)


@dataclass(frozen=True)
class FeatureSeries:
    times_s: np.ndarray
    chroma: np.ndarray
    onset: np.ndarray
    energy_db: np.ndarray
    centroid_hz: np.ndarray
    duration_s: float
    hop_s: float


def _ffprobe(path: Path) -> dict:
    media_path = path.resolve()
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name:stream=codec_name,sample_rate,channels,"
                "channel_layout,bits_per_raw_sample,bits_per_sample",
                "-of",
                "json",
                "--",
                str(media_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=AUDIO_TOOL_TIMEOUT_S,
        )
        return json.loads(completed.stdout)
    except FileNotFoundError as error:
        raise ValueError(
            "ffprobe is required to inspect this audio format; install ffmpeg"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ValueError(
            f"audio inspection timed out after {AUDIO_TOOL_TIMEOUT_S} seconds: {path}"
        ) from error
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        KeyError,
    ) as error:
        raise ValueError(f"not a decodable audio file: {path}") from error


def probe_encoding(path: str | Path) -> tuple[EncodingFingerprint, float]:
    media_path = Path(path)
    try:
        info = sf.info(media_path)
        subtype = info.subtype or ""
        digits = "".join(character for character in subtype if character.isdigit())
        bit_depth = int(digits) if digits else None
        encoding = EncodingFingerprint(
            container=info.format,
            codec=info.subtype,
            sample_rate_hz=info.samplerate,
            bit_depth=bit_depth,
            channels=info.channels,
            channel_layout="mono"
            if info.channels == 1
            else "stereo"
            if info.channels == 2
            else None,
        )
        return encoding, float(info.frames / info.samplerate)
    except (RuntimeError, TypeError):
        payload = _ffprobe(media_path)
        stream = next(
            (
                item
                for item in payload.get("streams", [])
                if item.get("sample_rate") is not None
            ),
            {},
        )
        bits = stream.get("bits_per_raw_sample") or stream.get("bits_per_sample")
        encoding = EncodingFingerprint(
            container=payload.get("format", {}).get("format_name"),
            codec=stream.get("codec_name"),
            sample_rate_hz=int(stream["sample_rate"])
            if stream.get("sample_rate")
            else None,
            bit_depth=int(bits) if bits and str(bits).isdigit() else None,
            channels=stream.get("channels"),
            channel_layout=stream.get("channel_layout"),
        )
        try:
            return encoding, float(payload["format"]["duration"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"not a decodable audio file: {media_path}") from error


def load_audio(path: str | Path, always_2d: bool = True) -> DecodedAudio:
    media_path = Path(path).resolve()
    try:
        samples, sample_rate = sf.read(
            media_path,
            dtype="float32",
            always_2d=always_2d,
        )
        if len(samples) == 0:
            raise ValueError(f"audio contains no decodable frames: {media_path}")
        return DecodedAudio(samples=samples, sample_rate_hz=int(sample_rate))
    except (RuntimeError, TypeError):
        encoding, _ = probe_encoding(media_path)
        sample_rate = encoding.sample_rate_hz or 48_000
        channels = encoding.channels or 2
        if channels <= 0:
            raise ValueError(f"not a decodable audio file: {media_path}") from None
        try:
            with tempfile.TemporaryFile() as decoded:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-i",
                        str(media_path),
                        "-f",
                        "f32le",
                        "-acodec",
                        "pcm_f32le",
                        "-ac",
                        str(channels),
                        "-ar",
                        str(sample_rate),
                        "-",
                    ],
                    check=True,
                    stdout=decoded,
                    stderr=subprocess.PIPE,
                    timeout=AUDIO_TOOL_TIMEOUT_S,
                )
                decoded_size = decoded.tell()
                frame_size = channels * np.dtype("<f4").itemsize
                if decoded_size == 0:
                    raise ValueError(
                        f"audio contains no decodable frames: {media_path}"
                    )
                if decoded_size % frame_size:
                    raise ValueError(f"not a decodable audio file: {media_path}")
                decoded.seek(0)
                flat = np.fromfile(decoded, dtype="<f4", count=decoded_size // 4)
        except FileNotFoundError as error:
            raise ValueError(
                "ffmpeg is required to decode this audio format; install ffmpeg"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise ValueError(
                f"audio decoding timed out after {AUDIO_TOOL_TIMEOUT_S} seconds: "
                f"{media_path}"
            ) from error
        except subprocess.CalledProcessError as error:
            raise ValueError(f"not a decodable audio file: {media_path}") from error
        samples = flat.reshape((-1, channels))
        if not always_2d and channels == 1:
            samples = samples[:, 0]
        if len(samples) == 0:
            raise ValueError(
                f"audio contains no decodable frames: {media_path}"
            ) from None
        return DecodedAudio(samples=samples, sample_rate_hz=sample_rate)


def _resample_mono(audio: DecodedAudio, target_rate: int) -> np.ndarray:
    mono = np.asarray(audio.mono, dtype=np.float64)
    if audio.sample_rate_hz == target_rate:
        return mono
    gcd = math.gcd(audio.sample_rate_hz, target_rate)
    return signal.resample_poly(
        mono,
        target_rate // gcd,
        audio.sample_rate_hz // gcd,
    )


def _silence_edges(
    mono: np.ndarray, sample_rate: int, threshold_dbfs: float = -55.0
) -> tuple[float, float]:
    threshold = 10 ** (threshold_dbfs / 20.0)
    active = np.flatnonzero(np.abs(mono) >= threshold)
    if len(active) == 0:
        duration = len(mono) / sample_rate
        return duration, duration
    initial = active[0] / sample_rate
    trailing = (len(mono) - 1 - active[-1]) / sample_rate
    return float(initial), float(trailing)


def _approximate_lra(
    mono: np.ndarray, sample_rate: int, block_s: float = 3.0
) -> float | None:
    block = max(1, int(block_s * sample_rate))
    if len(mono) < block:
        return None
    values: list[float] = []
    for start in range(0, len(mono) - block + 1, max(1, block // 2)):
        rms = float(np.sqrt(np.mean(np.square(mono[start : start + block]))))
        level = dbfs(rms)
        if level > -70:
            values.append(level)
    if len(values) < 4:
        return None
    return float(np.percentile(values, 95) - np.percentile(values, 10))


def _streaming_true_peak(
    samples: np.ndarray,
    sample_rate: int,
    oversample: int = 4,
    block_s: float = 2.0,
) -> float:
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, None]
    if not len(values):
        return 0.0
    block = max(1, int(block_s * sample_rate))
    peak = 0.0
    # Small overlap avoids losing interpolation peaks at block boundaries.
    overlap = min(64, block // 4)
    for channel_index in range(values.shape[1]):
        channel = values[:, channel_index]
        start = 0
        while start < len(channel):
            end = min(len(channel), start + block)
            chunk_start = max(0, start - overlap)
            chunk_end = min(len(channel), end + overlap)
            oversampled = signal.resample_poly(
                channel[chunk_start:chunk_end],
                oversample,
                1,
            )
            peak = max(peak, float(np.max(np.abs(oversampled))))
            start = end
    return peak


def _rms_db(values: np.ndarray) -> float:
    if not len(values):
        return -120.0
    return dbfs(float(np.sqrt(np.mean(np.square(values)))))


def diagnose_ending(
    mono: np.ndarray,
    sample_rate: int,
    *,
    trailing_silence_s: float,
) -> EndingDiagnostics:
    """Describe the decoded file boundary without claiming an audible defect."""
    values = np.asarray(mono, dtype=np.float64)
    if sample_rate <= 0 or len(values) < max(2, int(0.25 * sample_rate)):
        return EndingDiagnostics(
            classification="indeterminate",
            status=EvaluationStatus.INDETERMINATE,
            trailing_silence_s=trailing_silence_s,
            final_100ms_rms_dbfs=-120.0,
            preceding_1s_rms_dbfs=-120.0,
            final_to_preceding_db=0.0,
            boundary_sample_peak_dbfs=-120.0,
            evidence=("audio is too short for a stable ending diagnostic",),
        )
    final_frames = max(1, int(round(0.100 * sample_rate)))
    preceding_frames = max(1, int(round(1.000 * sample_rate)))
    final = values[-final_frames:]
    final_10ms_frames = max(1, int(round(0.010 * sample_rate)))
    final_10ms = values[-final_10ms_frames:]
    preceding_90ms = values[
        max(0, len(values) - final_frames) : max(0, len(values) - final_10ms_frames)
    ]
    preceding_end = max(0, len(values) - final_frames)
    preceding_start = max(0, preceding_end - preceding_frames)
    preceding = values[preceding_start:preceding_end]
    final_db = _rms_db(final)
    final_10ms_db = _rms_db(final_10ms)
    preceding_90ms_db = _rms_db(preceding_90ms)
    preceding_db = _rms_db(preceding)
    delta_db = final_db - preceding_db
    boundary_db = dbfs(float(abs(values[-1])))
    evidence = (
        f"trailing silence={trailing_silence_s:.4f}s",
        f"final 100ms RMS={final_db:.2f} dBFS",
        f"final 10ms RMS={final_10ms_db:.2f} dBFS",
        f"preceding 90ms RMS={preceding_90ms_db:.2f} dBFS",
        f"preceding window RMS={preceding_db:.2f} dBFS",
        f"final/preceding={delta_db:+.2f} dB",
        f"last decoded sample={boundary_db:.2f} dBFS",
    )
    if trailing_silence_s >= 0.250:
        classification = "silence_tail"
        status = EvaluationStatus.PASS
    elif (
        final_db <= -45.0
        or delta_db <= -18.0
        or final_10ms_db <= -45.0
        or final_10ms_db - preceding_90ms_db <= -12.0
    ):
        classification = "natural_decay"
        status = EvaluationStatus.PASS
    elif final_db > -35.0 and delta_db > -8.0:
        classification = (
            "likely_abrupt_boundary"
            if boundary_db > -24.0
            else "active_audio_at_boundary"
        )
        status = EvaluationStatus.INDETERMINATE
    else:
        classification = "indeterminate"
        status = EvaluationStatus.INDETERMINATE
    return EndingDiagnostics(
        classification=classification,
        status=status,
        trailing_silence_s=trailing_silence_s,
        final_100ms_rms_dbfs=final_db,
        preceding_1s_rms_dbfs=preceding_db,
        final_to_preceding_db=delta_db,
        boundary_sample_peak_dbfs=boundary_db,
        evidence=evidence,
    )


def measure_audio(
    path: str | Path,
    artifact_id: str,
    analysis_run_id: str,
    acquisition_degraded: bool,
) -> AudioMetrics:
    audio = load_audio(path)
    mono = np.asarray(audio.mono, dtype=np.float64)
    peak = float(np.max(np.abs(audio.samples))) if len(audio.samples) else 0.0
    true_peak = _streaming_true_peak(audio.samples, audio.sample_rate_hz)
    clipped = float(np.mean(np.abs(audio.samples) >= 0.9999))
    initial, trailing = _silence_edges(mono, audio.sample_rate_hz)
    ending = diagnose_ending(
        mono,
        audio.sample_rate_hz,
        trailing_silence_s=trailing,
    )

    diff = np.abs(np.diff(mono))
    if len(diff):
        median = float(np.median(diff))
        mad = float(np.median(np.abs(diff - median))) + 1e-12
        click_threshold = max(0.25, median + 25.0 * mad)
        macro_click_count = int(np.count_nonzero(diff > click_threshold))
    else:
        macro_click_count = 0

    warnings: list[str] = []
    if initial > 2.0:
        warnings.append("long_initial_silence")
    if trailing > 3.0:
        warnings.append("long_trailing_silence")
    if clipped > 1e-5:
        warnings.append("decoded_clipping_hint")
    if macro_click_count:
        warnings.append("macro_click_candidates_require_listening")
    if ending.classification in {
        "active_audio_at_boundary",
        "likely_abrupt_boundary",
    }:
        warnings.append("active_audio_at_file_boundary_requires_listening")
    if acquisition_degraded:
        warnings.append("format_sensitive_absolute_claims_disabled")

    integrated_lufs: float | None
    try:
        meter = pyln.Meter(audio.sample_rate_hz)
        integrated_lufs = float(meter.integrated_loudness(audio.samples))
        if not np.isfinite(integrated_lufs):
            integrated_lufs = None
    except (ValueError, ZeroDivisionError):
        integrated_lufs = None

    stereo_correlation: float | None = None
    side_to_mid_db: float | None = None
    if audio.samples.ndim == 2 and audio.samples.shape[1] == 2:
        left = audio.samples[:, 0].astype(np.float64)
        right = audio.samples[:, 1].astype(np.float64)
        if np.std(left) > 1e-12 and np.std(right) > 1e-12:
            stereo_correlation = float(np.corrcoef(left, right)[0, 1])
        mid = (left + right) / 2.0
        side = (left - right) / 2.0
        mid_rms = float(np.sqrt(np.mean(mid**2)))
        side_rms = float(np.sqrt(np.mean(side**2)))
        side_to_mid_db = dbfs(side_rms / max(mid_rms, 1e-12))

    return AudioMetrics(
        artifact_id=artifact_id,
        analysis_run_id=analysis_run_id,
        measured_file_duration_s=float(audio.duration_s),
        sample_rate_hz=audio.sample_rate_hz,
        channels=audio.channels,
        peak_dbfs=dbfs(peak),
        true_peak_dbfs=dbfs(true_peak),
        integrated_lufs=integrated_lufs,
        approximate_lra_lu=_approximate_lra(mono, audio.sample_rate_hz),
        clipped_sample_ratio=clipped,
        dc_offset=float(np.mean(mono)) if len(mono) else 0.0,
        initial_silence_s=initial,
        trailing_silence_s=trailing,
        macro_click_count=macro_click_count,
        stereo_correlation=stereo_correlation,
        side_to_mid_db=side_to_mid_db,
        acquisition_degraded=acquisition_degraded,
        warnings=tuple(warnings),
        technical_checks={
            "decode": EvaluationStatus.PASS,
            "silence_edges": (
                EvaluationStatus.FAIL
                if initial > 2.0 or trailing > 3.0
                else EvaluationStatus.PASS
            ),
            "hard_cut_or_macro_glitch": (
                EvaluationStatus.INDETERMINATE
                if macro_click_count or ending.status == EvaluationStatus.INDETERMINATE
                else EvaluationStatus.PASS
            ),
            "ending_boundary": ending.status,
            "decoded_clipping": (
                EvaluationStatus.INDETERMINATE
                if clipped > 1e-5
                else EvaluationStatus.PASS
            ),
            "vocal_timbre_change": EvaluationStatus.NOT_EVALUATED,
        },
        evidence_gaps=(
            "vocal timbre change requires separated-vocal or human evidence",
        ),
        ending=ending,
    )


def extract_features(
    path: str | Path,
    target_rate: int = 11_025,
    n_fft: int = 1024,
    hop_length: int = 512,
) -> FeatureSeries:
    audio = load_audio(path)
    mono = _resample_mono(audio, target_rate)
    if len(mono) < n_fft:
        mono = np.pad(mono, (0, n_fft - len(mono)))
    frequencies, times, stft = signal.stft(
        mono,
        fs=target_rate,
        window="hann",
        nperseg=n_fft,
        noverlap=n_fft - hop_length,
        boundary=None,
        padded=False,
    )
    magnitude = np.abs(stft).T.astype(np.float64)
    power = magnitude**2
    energy = np.sqrt(np.mean(power, axis=1) + 1e-16)
    energy_db = 20 * np.log10(energy + 1e-12)
    centroid = np.sum(magnitude * frequencies[None, :], axis=1) / (
        np.sum(magnitude, axis=1) + 1e-12
    )
    flux = np.zeros(len(magnitude), dtype=np.float64)
    if len(magnitude) > 1:
        positive = np.maximum(0.0, np.diff(magnitude, axis=0))
        flux[1:] = np.sqrt(np.mean(positive**2, axis=1))
    flux_scale = np.percentile(flux, 95) if len(flux) else 0.0
    onset = flux / max(float(flux_scale), 1e-12)

    chroma = np.zeros((len(magnitude), 12), dtype=np.float64)
    valid = (frequencies >= 55.0) & (frequencies <= 5000.0)
    valid_bins = np.flatnonzero(valid)
    if len(valid_bins):
        midi = np.rint(69 + 12 * np.log2(frequencies[valid_bins] / 440.0)).astype(int)
        pitch_classes = np.mod(midi, 12)
        for pitch_class in range(12):
            bins = valid_bins[pitch_classes == pitch_class]
            if len(bins):
                chroma[:, pitch_class] = np.sum(power[:, bins], axis=1)
    norms = np.linalg.norm(chroma, axis=1, keepdims=True)
    chroma = chroma / np.maximum(norms, 1e-12)
    return FeatureSeries(
        times_s=times.astype(np.float64),
        chroma=chroma,
        onset=onset,
        energy_db=energy_db,
        centroid_hz=centroid,
        duration_s=float(audio.duration_s),
        hop_s=hop_length / target_rate,
    )


def analyze_structure(
    features: FeatureSeries,
    artifact_id: str,
    analysis_run_id: str,
    min_segment_s: float = 8.0,
    max_boundaries: int = 24,
) -> StructureAnalysis:
    if len(features.times_s) < 4:
        return StructureAnalysis(
            artifact_id=artifact_id,
            analysis_run_id=analysis_run_id,
            boundaries=(
                StructureBoundary(time_s=0.0, confidence=1.0),
                StructureBoundary(time_s=features.duration_s, confidence=1.0),
            ),
            segments=(
                StructureSegment(
                    start_s=0.0,
                    end_s=features.duration_s,
                    repeat_group="A",
                    similarity_to_group=1.0,
                ),
            ),
            feature_hop_s=features.hop_s,
            warnings=("insufficient_frames_for_structure",),
        )
    energy = features.energy_db
    energy_z = (energy - np.median(energy)) / (np.std(energy) + 1e-9)
    onset_z = (features.onset - np.median(features.onset)) / (
        np.std(features.onset) + 1e-9
    )
    combined = np.column_stack((features.chroma, energy_z, onset_z))
    novelty = np.linalg.norm(np.diff(combined, axis=0), axis=1)
    smooth_frames = max(1, int(round(1.0 / features.hop_s)))
    if smooth_frames > 1:
        novelty = np.convolve(
            novelty,
            np.ones(smooth_frames) / smooth_frames,
            mode="same",
        )
    minimum_distance = max(1, int(round(min_segment_s / features.hop_s)))
    prominence = max(float(np.percentile(novelty, 65)) * 0.25, 1e-6)
    peaks, properties = signal.find_peaks(
        novelty,
        distance=minimum_distance,
        prominence=prominence,
    )
    if len(peaks) > max_boundaries - 2:
        order = np.argsort(properties["prominences"])[-(max_boundaries - 2) :]
        peaks = np.sort(peaks[order])
    scale = max(float(np.percentile(novelty, 95)), 1e-9)
    boundaries = [StructureBoundary(time_s=0.0, confidence=1.0)]
    for peak in peaks:
        boundaries.append(
            StructureBoundary(
                time_s=float(
                    features.times_s[min(peak + 1, len(features.times_s) - 1)]
                ),
                confidence=float(min(1.0, novelty[peak] / scale)),
            )
        )
    boundaries.append(
        StructureBoundary(time_s=float(features.duration_s), confidence=1.0)
    )
    summaries: list[np.ndarray] = []
    segments: list[StructureSegment] = []
    group_centroids: list[np.ndarray] = []
    group_members: list[int] = []
    boundary_times = [item.time_s for item in boundaries]
    for start_s, end_s in zip(boundary_times, boundary_times[1:], strict=False):
        mask = (features.times_s >= start_s) & (features.times_s < end_s)
        if not np.any(mask):
            summary = np.zeros(14, dtype=np.float64)
        else:
            summary = np.concatenate(
                (
                    np.mean(features.chroma[mask], axis=0),
                    np.array(
                        [
                            np.mean(onset_z[mask]),
                            np.mean(energy_z[mask]),
                        ]
                    ),
                )
            )
        summaries.append(summary)
        similarities = [
            float(
                np.dot(summary, centroid)
                / (
                    max(np.linalg.norm(summary), 1e-12)
                    * max(np.linalg.norm(centroid), 1e-12)
                )
            )
            for centroid in group_centroids
        ]
        if similarities and max(similarities) >= 0.82:
            group_index = int(np.argmax(similarities))
            similarity = similarities[group_index]
            count = group_members[group_index]
            group_centroids[group_index] = (
                group_centroids[group_index] * count + summary
            ) / (count + 1)
            group_members[group_index] += 1
        else:
            group_index = len(group_centroids)
            group_centroids.append(summary.copy())
            group_members.append(1)
            similarity = 1.0
        repeat_group = (
            chr(ord("A") + group_index) if group_index < 26 else f"G{group_index + 1}"
        )
        segments.append(
            StructureSegment(
                start_s=float(start_s),
                end_s=float(end_s),
                repeat_group=repeat_group,
                similarity_to_group=similarity,
            )
        )
    return StructureAnalysis(
        artifact_id=artifact_id,
        analysis_run_id=analysis_run_id,
        boundaries=tuple(boundaries),
        segments=tuple(segments),
        feature_hop_s=features.hop_s,
    )


def _interpolate_features(
    series: FeatureSeries, points: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    source = np.linspace(0.0, 1.0, len(series.times_s))
    target = np.linspace(0.0, 1.0, points)
    chroma = np.column_stack(
        [np.interp(target, source, series.chroma[:, index]) for index in range(12)]
    )
    chroma /= np.maximum(np.linalg.norm(chroma, axis=1, keepdims=True), 1e-12)
    onset = np.interp(target, source, series.onset)
    energy = np.interp(target, source, series.energy_db)
    onset = (onset - np.median(onset)) / (np.std(onset) + 1e-9)
    energy = (energy - np.median(energy)) / (np.std(energy) + 1e-9)
    return chroma, onset, energy


def _top_hotspots(
    pitch: np.ndarray,
    rhythm: np.ndarray,
    energy: np.ndarray,
    duration_a: float,
    duration_b: float,
    offset_a_s: float = 0.0,
    offset_b_s: float = 0.0,
    count: int = 6,
) -> tuple[DifferenceHotspot, ...]:
    families = (
        ("pitch_harmony", pitch),
        ("rhythm_onset", rhythm),
        ("energy_structure", energy),
    )
    candidates: list[tuple[float, str, int, int]] = []
    window = max(4, len(pitch) // 32)
    for family, values in families:
        for start in range(0, len(values), window):
            end = min(len(values), start + window)
            magnitude = float(np.mean(values[start:end]))
            candidates.append((magnitude, family, start, end))
    candidates.sort(reverse=True)
    chosen: list[DifferenceHotspot] = []
    used: list[tuple[int, int]] = []
    for magnitude, family, start, end in candidates:
        if any(
            not (end <= used_start or start >= used_end)
            for used_start, used_end in used
        ):
            continue
        a_start = offset_a_s + duration_a * start / len(pitch)
        a_end = offset_a_s + duration_a * end / len(pitch)
        b_start = offset_b_s + duration_b * start / len(pitch)
        b_end = offset_b_s + duration_b * end / len(pitch)
        chosen.append(
            DifferenceHotspot(
                a_start_s=float(a_start),
                a_end_s=float(a_end),
                b_start_s=float(b_start),
                b_end_s=float(b_end),
                feature_family=family,
                magnitude=magnitude,
                evidence=f"time-normalized {family} divergence",
            )
        )
        used.append((start, end))
        if len(chosen) == count:
            break
    return tuple(chosen)


def compare_feature_series(
    features_a: FeatureSeries,
    features_b: FeatureSeries,
    *,
    project_id: str,
    artifact_a_id: str,
    artifact_b_id: str,
    analysis_run_id: str,
    same_generation_event: bool,
    acquisition_warning: str | None = None,
    deterministic_relation: bool = False,
    excluded_difference_regions: Iterable[tuple[float, float]] = (),
    comparable_region_a_s: tuple[float, float] | None = None,
    comparable_region_b_s: tuple[float, float] | None = None,
    points: int = 384,
) -> PairwiseComparison:
    region_a = comparable_region_a_s or (0.0, features_a.duration_s)
    region_b = comparable_region_b_s or (0.0, features_b.duration_s)
    duration_a = region_a[1] - region_a[0]
    duration_b = region_b[1] - region_b[0]
    if duration_a <= 0 or duration_b <= 0:
        raise ValueError("comparable audio regions must have positive duration")
    comparable_a = _crop_feature_series(features_a, region_a)
    comparable_b = _crop_feature_series(features_b, region_b)
    a_chroma, a_onset, a_energy = _interpolate_features(comparable_a, points)
    b_chroma, b_onset, b_energy = _interpolate_features(comparable_b, points)
    pitch = 1.0 - np.sum(a_chroma * b_chroma, axis=1)
    rhythm = np.minimum(4.0, np.abs(a_onset - b_onset)) / 4.0
    energy = np.minimum(4.0, np.abs(a_energy - b_energy)) / 4.0
    return PairwiseComparison(
        project_id=project_id,
        artifact_a_id=artifact_a_id,
        artifact_b_id=artifact_b_id,
        analysis_run_id=analysis_run_id,
        comparable_region_a_s=region_a,
        comparable_region_b_s=region_b,
        pitch_harmony_distance=float(np.mean(pitch)),
        rhythm_onset_distance=float(np.mean(rhythm)),
        energy_structure_distance=float(np.mean(energy)),
        hotspots=_top_hotspots(
            pitch,
            rhythm,
            energy,
            duration_a,
            duration_b,
            offset_a_s=region_a[0],
            offset_b_s=region_b[0],
        ),
        same_generation_event=same_generation_event,
        parameter_attribution_allowed=not same_generation_event,
        acquisition_warning=acquisition_warning,
        deterministic_relation=deterministic_relation,
        excluded_difference_regions=tuple(excluded_difference_regions),
    )


def _crop_feature_series(
    features: FeatureSeries,
    interval_s: tuple[float, float],
) -> FeatureSeries:
    start, end = interval_s
    mask = (features.times_s >= start) & (features.times_s <= end)
    if not mask.any():
        raise ValueError(f"no feature frames in interval {interval_s}")
    return FeatureSeries(
        times_s=features.times_s[mask] - start,
        chroma=features.chroma[mask],
        onset=features.onset[mask],
        energy_db=features.energy_db[mask],
        centroid_hz=features.centroid_hz[mask],
        duration_s=end - start,
        hop_s=features.hop_s,
    )


def compare_audio_files(
    path_a: str | Path,
    path_b: str | Path,
    **kwargs,
) -> PairwiseComparison:
    return compare_feature_series(
        extract_features(path_a),
        extract_features(path_b),
        **kwargs,
    )


def loudness_match_gain_db(
    lufs_a: float | None, lufs_b: float | None
) -> tuple[float, float]:
    """Return non-positive gains; the louder item is attenuated to the quieter."""
    if lufs_a is None or lufs_b is None:
        return 0.0, 0.0
    target = min(lufs_a, lufs_b)
    return min(0.0, target - lufs_a), min(0.0, target - lufs_b)


def verify_deterministic_crop(
    parent_path: str | Path,
    child_path: str | Path,
    *,
    parent_artifact_id: str,
    child_artifact_id: str,
    analysis_run_id: str,
    correlation_threshold: float = 0.995,
    analysis_rate: int = 4_000,
    envelope_rate: int = 200,
) -> CropVerification:
    parent_audio = load_audio(parent_path)
    child_audio = load_audio(child_path)
    parent = _resample_mono(parent_audio, analysis_rate)
    child = _resample_mono(child_audio, analysis_rate)
    if len(child) > len(parent):
        raise ValueError("crop child cannot be longer than parent")

    coarse_gcd = math.gcd(analysis_rate, envelope_rate)
    parent_envelope = signal.resample_poly(
        parent,
        envelope_rate // coarse_gcd,
        analysis_rate // coarse_gcd,
    )
    child_envelope = signal.resample_poly(
        child,
        envelope_rate // coarse_gcd,
        analysis_rate // coarse_gcd,
    )
    parent_envelope = (parent_envelope - np.mean(parent_envelope)) / (
        np.std(parent_envelope) + 1e-12
    )
    child_envelope = (child_envelope - np.mean(child_envelope)) / (
        np.std(child_envelope) + 1e-12
    )
    correlation = signal.correlate(
        parent_envelope,
        child_envelope,
        mode="valid",
        method="fft",
    )
    lag_frames = int(np.argmax(correlation))
    coarse_lag_s = lag_frames / envelope_rate
    coarse_lag_samples = int(round(coarse_lag_s * analysis_rate))

    search_radius = int(round(0.025 * analysis_rate))
    max_lag = len(parent) - len(child)
    coarse_lag_samples = min(max(0, coarse_lag_samples), max_lag)
    best: tuple[float, int] = (-1.0, coarse_lag_samples)
    child_centered = child - np.mean(child)
    child_norm = np.linalg.norm(child_centered)
    for lag in range(
        max(0, coarse_lag_samples - search_radius),
        min(max_lag, coarse_lag_samples + search_radius) + 1,
    ):
        region = parent[lag : lag + len(child)]
        region_centered = region - np.mean(region)
        denominator = np.linalg.norm(region_centered) * child_norm
        value = (
            float(np.dot(region_centered, child_centered) / denominator)
            if denominator > 1e-12
            else -1.0
        )
        if value > best[0]:
            best = (value, lag)
    coefficient, lag_samples = best
    lag_s = lag_samples / analysis_rate
    return CropVerification(
        parent_artifact_id=parent_artifact_id,
        child_artifact_id=child_artifact_id,
        analysis_run_id=analysis_run_id,
        lag_s=float(lag_s),
        retained_region_correlation=float(coefficient),
        correlation_threshold=correlation_threshold,
        verified=coefficient >= correlation_threshold,
    )


def audio_identity(path: str | Path) -> tuple[str, EncodingFingerprint, float]:
    encoding, duration = probe_encoding(path)
    return file_sha256(path), encoding, duration
