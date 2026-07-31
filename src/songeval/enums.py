from __future__ import annotations

from enum import StrEnum


class Provenance(StrEnum):
    CAPTURED = "captured"
    DECLARED = "declared"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INDETERMINATE = "indeterminate"


class SourceKind(StrEnum):
    FULL_SONG = "full_song"
    AUDIO_UPLOAD = "audio_upload"
    SAMPLE = "sample"
    COVER_SOURCE = "cover_source"
    HUMAN_RECORDING = "human_recording"
    OTHER = "other"


class AcquisitionPath(StrEnum):
    LOSSLESS_PLATFORM_DOWNLOAD = "lossless_platform_download"
    LOSSY_PLATFORM_DOWNLOAD = "lossy_platform_download"
    USER_PROVIDED_UNKNOWN = "user_provided_unknown"
    CDN_LOSSY = "cdn_lossy"
    LOCAL_RECORDING = "local_recording"
    UNKNOWN = "unknown"

    @property
    def format_sensitive_allowed(self) -> bool:
        return self in {
            AcquisitionPath.LOSSLESS_PLATFORM_DOWNLOAD,
            AcquisitionPath.LOCAL_RECORDING,
        }


class TaskType(StrEnum):
    CREATE = "create"
    COVER = "cover"
    EDIT_CROP = "edit_crop"
    EXTEND = "extend"
    REPLACE_SECTION = "replace_section"
    REMASTER = "remaster"
    AUDIO_UPLOAD = "audio_upload"
    UNKNOWN = "unknown"


class OperationType(StrEnum):
    RAW = "raw"
    CROP = "crop"
    COVER = "cover"
    EXTEND = "extend"
    REPLACE_SECTION = "replace_section"
    REMASTER = "remaster"
    DOWNLOAD_TRANSCODE = "download_transcode"


class BoundaryQuality(StrEnum):
    CLEAN_BAR = "clean_bar"
    CLEAN_PHRASE = "clean_phrase"
    CLEAN_LYRIC_LINE = "clean_lyric_line"
    MID_PHRASE = "mid_phrase"
    MID_WORD = "mid_word"
    MID_TRANSIENT = "mid_transient"
    UNKNOWN = "unknown"


class PreservationIntent(StrEnum):
    EXACT_AUDIO = "exact_audio"
    MELODY_RHYTHM = "melody_rhythm"
    STRUCTURAL_GESTURE = "structural_gesture"


class ProtectedDimension(StrEnum):
    MELODY = "melody"
    RHYTHM = "rhythm"
    HARMONY = "harmony"
    TIMBRE = "timbre"
    VOCAL_IDENTITY = "vocal_identity"
    ARRANGEMENT = "arrangement"
    LYRICS = "lyrics"
    STRUCTURE_SHAPE = "structure_shape"
    ENERGY_ENVELOPE = "energy_envelope"


class TargetPlacement(StrEnum):
    GLOBAL_CONDITIONER = "global_conditioner"
    ABSOLUTE_TIME = "absolute_time"
    SECTION_RELATIVE = "section_relative"
    UNSPECIFIED = "unspecified"


class EvaluationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"
    NOT_EVALUATED = "not_evaluated"


class DefectTier(StrEnum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


class Axis(StrEnum):
    COMPLIANCE = "compliance"
    CRAFT = "craft"
    RELEASE_READINESS = "release_readiness"
    DISTINCTIVENESS = "distinctiveness"


class ReadinessStatus(StrEnum):
    READY = "ready"
    NEEDS_SUNO_EDIT = "needs_suno_edit"
    BLOCKED = "blocked"
    INDETERMINATE = "indeterminate"
    NOT_ELIGIBLE = "not_eligible"


class RecommendationStatus(StrEnum):
    RECOMMENDED = "recommended"
    UNIQUE_SURVIVOR = "unique_survivor"
    ABSTAIN = "abstain"
    NO_RELEASE_CANDIDATE = "no_release_candidate"


class EvidenceInheritance(StrEnum):
    INHERIT_PRESERVED_REGION = "inherit_preserved_region"
    RECOMPUTE = "recompute"
    NEVER_INHERIT = "never_inherit"


class ComparisonOutcome(StrEnum):
    A = "a"
    B = "b"
    TIE = "tie"
    NA = "n/a"


class CraftAttribute(StrEnum):
    WARMTH_FULLNESS = "warmth_fullness"
    HOOK_CATCHINESS = "hook_catchiness"
    VOCAL_TIMBRE_IDENTITY = "vocal_timbre_identity"
    ARRANGEMENT_HARMONY_DEVELOPMENT = "arrangement_harmony_development"
    LYRIC_DELIVERY = "lyric_delivery"
    ENDING_COMPLETENESS = "ending_completeness"
    OVERALL_PREFERENCE = "overall_preference"
