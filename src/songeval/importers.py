from __future__ import annotations

import contextlib
import html
import json
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .audio import audio_identity, verify_deterministic_crop
from .enums import (
    AcquisitionPath,
    Confidence,
    EvidenceInheritance,
    OperationType,
    Provenance,
    TaskType,
)
from .models import (
    AcquisitionSnapshot,
    ArtifactEdge,
    BriefRequirement,
    CreativeBriefVersion,
    GenerationEvent,
    ParameterValue,
    ProjectManifest,
    ProvenanceRecord,
    ReleaseArtifact,
    SourceMaterial,
    Take,
)
from .util import content_hash, expand_path, slugify


class SunoImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParentDeclaration:
    """A user-declared parent for a child clip.

    ``parent`` may be another clip ID in the intake set, a local audio path, or
    a Suno share URL resolving to exactly one clip. The relationship is never
    inferred from titles or durations.
    """

    child_clip_id: str
    parent: str


@dataclass(frozen=True)
class IntakeResult:
    manifest: ProjectManifest
    warnings: tuple[str, ...]


class SunoClipSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    title: str | None = None
    audio_url: str | None = None
    duration: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw_payload: dict[str, Any]
    source_url: str


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _looks_like_clip(value: dict[str, Any]) -> bool:
    candidate_id = value.get("id") or value.get("clip_id")
    if not isinstance(candidate_id, str):
        return False
    audio = value.get("audio_url") or value.get("audioUrl")
    metadata = value.get("metadata")
    return bool(audio or isinstance(metadata, dict)) and len(candidate_id) >= 8


class SunoPublicClient:
    """Best-effort public page resolver.

    Suno has no stable public import API contract. Raw payloads are retained, and
    failure is explicit instead of fabricating defaults.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout_s: float = 20.0,
    ):
        self._owns_client = client is None
        self.client = client or httpx.Client(
            follow_redirects=False,
            timeout=timeout_s,
            headers={"User-Agent": "suno-song-evaluator/0.1"},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> SunoPublicClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @staticmethod
    def validate_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise SunoImportError("Suno URL must be http(s)")
        host = (parsed.hostname or "").lower()
        if host != "suno.com" and not host.endswith(".suno.com"):
            raise SunoImportError("URL is not on suno.com")

    @staticmethod
    def validate_audio_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise SunoImportError("Suno audio URL must be http(s)")
        host = (parsed.hostname or "").lower()
        if not (
            host == "suno.ai"
            or host.endswith(".suno.ai")
            or host == "suno.com"
            or host.endswith(".suno.com")
        ):
            raise SunoImportError("audio URL is not on a Suno-controlled host")

    def _get_validated(
        self,
        url: str,
        validator,
        *,
        stream: bool = False,
        max_redirects: int = 5,
    ) -> httpx.Response:
        current_url = url
        for _ in range(max_redirects + 1):
            validator(current_url)
            request = self.client.build_request("GET", current_url)
            response = self.client.send(
                request,
                follow_redirects=False,
                stream=stream,
            )
            if not response.is_redirect:
                validator(str(response.url))
                return response
            next_request = response.next_request
            response.close()
            if next_request is None:
                raise SunoImportError("redirect response has no target")
            current_url = str(next_request.url)
            validator(current_url)
        raise SunoImportError(f"too many redirects (limit {max_redirects})")

    def fetch(self, url: str) -> list[SunoClipSnapshot]:
        self.validate_url(url)
        response = self._get_validated(url, self.validate_url)
        try:
            response.raise_for_status()
            payloads = self._extract_payloads(response)
        finally:
            response.close()
        clips: dict[str, SunoClipSnapshot] = {}
        for payload in payloads:
            for candidate in _walk_json(payload):
                if not _looks_like_clip(candidate):
                    continue
                clip_id = str(candidate.get("id") or candidate.get("clip_id"))
                metadata = candidate.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                duration = candidate.get("duration")
                if duration is None:
                    duration = metadata.get("duration")
                clips[clip_id] = SunoClipSnapshot(
                    id=clip_id,
                    title=candidate.get("title"),
                    audio_url=candidate.get("audio_url") or candidate.get("audioUrl"),
                    duration=float(duration) if duration is not None else None,
                    metadata=metadata,
                    raw_payload=candidate,
                    source_url=str(response.url),
                )
        if not clips:
            raise SunoImportError(
                "No clip metadata found; save the page/API payload and use a manifest"
            )
        return list(clips.values())

    @staticmethod
    def _extract_payloads(response: httpx.Response) -> list[Any]:
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            return [response.json()]
        text = html.unescape(response.text)
        payloads: list[Any] = []
        script_matches = re.findall(
            r"<script[^>]*type=[\"']application/(?:ld\+)?json[\"'][^>]*>(.*?)</script>",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        next_data = re.findall(
            r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        for candidate in script_matches + next_data:
            try:
                payloads.append(json.loads(candidate))
            except json.JSONDecodeError:
                continue
        # Next.js React Server Component payloads are JSON arrays whose second
        # item is a string containing further JSON values.
        flight_matches = re.findall(
            r"self\.__next_f\.push\((.*?)\)\s*</script>",
            response.text,
            flags=re.DOTALL,
        )
        decoded_strings: list[str] = []
        for candidate in flight_matches:
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            payloads.append(decoded)
            if (
                isinstance(decoded, list)
                and len(decoded) > 1
                and isinstance(decoded[1], str)
            ):
                decoded_strings.append(decoded[1])
        decoder = json.JSONDecoder()
        for decoded_text in decoded_strings:
            with contextlib.suppress(json.JSONDecodeError):
                payloads.append(json.loads(decoded_text))
            for start in (
                match.start()
                for match in re.finditer(
                    r'\{(?=[^{}]{0,120}"(?:type|entity_type|id)"\s*:)',
                    decoded_text,
                )
            ):
                try:
                    value, _ = decoder.raw_decode(decoded_text[start:])
                except json.JSONDecodeError:
                    continue
                payloads.append(value)
        # React server-component streams often contain JSON objects escaped in text.
        for match in re.finditer(
            r"\{[^{}]{0,2000}\"(?:audio_url|clip_id)\"[^{}]*\}", text
        ):
            candidate = match.group(0).replace('\\"', '"')
            try:
                payloads.append(json.loads(candidate))
            except json.JSONDecodeError:
                continue
        return payloads

    def download_audio(
        self,
        clip: SunoClipSnapshot,
        destination: str | Path,
        *,
        max_bytes: int = 512 * 1024 * 1024,
    ) -> Path:
        """Download captured public audio without transcoding it.

        Existing files are decoded and reused. New downloads are written to a
        sibling temporary file and atomically moved into place only after the
        audio can be probed.
        """
        if not clip.audio_url:
            raise SunoImportError(f"{clip.id}: public payload has no audio URL")
        try:
            self.validate_audio_url(clip.audio_url)
        except SunoImportError as error:
            raise SunoImportError(f"{clip.id}: {error}") from error
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            audio_identity(target)
            return target
        temporary = target.with_name(f".{target.name}.part")
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        try:
            response = self._get_validated(
                clip.audio_url,
                self.validate_audio_url,
                stream=True,
            )
            try:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if content_type.startswith("text/") or "json" in content_type:
                    raise SunoImportError(
                        f"{clip.id}: audio URL returned {content_type or 'text'}"
                    )
                declared_size = response.headers.get("content-length")
                if declared_size:
                    try:
                        declared_bytes = int(declared_size)
                    except ValueError:
                        declared_bytes = None
                    if declared_bytes is not None and declared_bytes > max_bytes:
                        raise SunoImportError(f"{clip.id}: audio exceeds size limit")
                written = 0
                with temporary.open("xb") as output:
                    for chunk in response.iter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise SunoImportError(
                                f"{clip.id}: audio exceeds size limit"
                            )
                        output.write(chunk)
            finally:
                response.close()
            if written == 0:
                raise SunoImportError(f"{clip.id}: downloaded audio is empty")
            audio_identity(temporary)
            temporary.replace(target)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            raise
        return target


def make_acquisition_snapshot(
    *,
    project_id: str,
    url: str,
    raw_payload: dict[str, Any],
    platform_id: str | None = None,
    file_sha256: str | None = None,
    existing: Iterable[AcquisitionSnapshot] = (),
) -> AcquisitionSnapshot:
    payload_sha256 = content_hash(raw_payload)
    identity_hash = file_sha256 or payload_sha256
    existing_for_url = sorted(
        (
            item
            for item in existing
            if item.url == url and item.platform_id == platform_id
        ),
        key=lambda item: item.created_at,
    )
    same = next(
        (
            item
            for item in existing_for_url
            if (item.file_sha256 or item.payload_sha256) == identity_hash
        ),
        None,
    )
    if same is not None:
        return same
    previous = existing_for_url[-1] if existing_for_url else None
    record_id = f"snapshot_{slugify(platform_id or 'url')}_{identity_hash[:16]}"
    changed = previous is not None
    return AcquisitionSnapshot(
        id=record_id,
        project_id=project_id,
        url=url,
        platform_id=platform_id,
        file_sha256=file_sha256,
        payload_sha256=payload_sha256,
        raw_payload=raw_payload,
        revision_of=previous.id if previous else None,
        content_changed_from_previous=changed,
        warning=(
            "The same URL now resolves to different content; a new immutable "
            "revision was created."
            if changed
            else None
        ),
    )


def hydrate_local_artifacts(
    manifest: ProjectManifest,
    *,
    manifest_path: str | Path | None = None,
    require_files: bool = True,
) -> ProjectManifest:
    base_dir = Path(manifest_path).resolve().parent if manifest_path else None
    artifacts: list[ReleaseArtifact] = []
    for artifact in manifest.artifacts:
        if not artifact.local_path:
            artifacts.append(artifact)
            continue
        path = expand_path(artifact.local_path, base_dir)
        if not path.exists():
            if require_files:
                raise FileNotFoundError(path)
            payload = artifact.model_dump(mode="python")
            payload["local_path"] = str(path)
            artifacts.append(ReleaseArtifact.model_validate(payload))
            continue
        digest, encoding, measured_duration = audio_identity(path)
        payload = artifact.model_dump(mode="python")
        payload.update(
            {
                "local_path": str(path),
                "file_sha256": digest,
                "encoding": encoding,
                "measured_file_duration_s": measured_duration,
                "duration_mismatch_s": None,
            }
        )
        artifacts.append(ReleaseArtifact.model_validate(payload))
    sources: list[SourceMaterial] = []
    for source in manifest.sources:
        if not source.local_path:
            sources.append(source)
            continue
        path = expand_path(source.local_path, base_dir)
        if not path.exists():
            if require_files:
                raise FileNotFoundError(path)
            payload = source.model_dump(mode="python")
            payload["local_path"] = str(path)
            sources.append(SourceMaterial.model_validate(payload))
            continue
        digest, _, _ = audio_identity(path)
        payload = source.model_dump(mode="python")
        payload.update({"local_path": str(path), "file_sha256": digest})
        sources.append(SourceMaterial.model_validate(payload))
    return manifest.model_copy(update={"artifacts": artifacts, "sources": sources})


def load_manifest(
    path: str | Path,
    *,
    hydrate_audio: bool = True,
    require_files: bool = True,
) -> ProjectManifest:
    manifest_path = Path(path)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = ProjectManifest.model_validate(raw)
    if hydrate_audio:
        manifest = hydrate_local_artifacts(
            manifest,
            manifest_path=manifest_path,
            require_files=require_files,
        )
    return manifest


_TASK_ALIASES: dict[str, TaskType] = {
    "gen": TaskType.CREATE,
    "generate": TaskType.CREATE,
    "create": TaskType.CREATE,
    "cover": TaskType.COVER,
    "edit_crop": TaskType.EDIT_CROP,
    "crop": TaskType.EDIT_CROP,
    "extend": TaskType.EXTEND,
    "replace_section": TaskType.REPLACE_SECTION,
    "remaster": TaskType.REMASTER,
    "audio_upload": TaskType.AUDIO_UPLOAD,
    "upload": TaskType.AUDIO_UPLOAD,
}


def captured_task(clip: SunoClipSnapshot) -> TaskType:
    """Resolve only explicit platform task/type fields, including Suno's `gen`."""
    values = (
        clip.metadata.get("task"),
        clip.metadata.get("type"),
        clip.raw_payload.get("task"),
        clip.raw_payload.get("type"),
    )
    for value in values:
        if isinstance(value, str):
            resolved = _TASK_ALIASES.get(value.strip().lower())
            if resolved is not None:
                return resolved
    return TaskType.UNKNOWN


def clip_to_generation_records(
    clip: SunoClipSnapshot,
    *,
    project_id: str,
    brief_id: str,
    source_material_ids: tuple[str, ...] = (),
    event_id: str | None = None,
    batch_id: str | None = None,
    batch_index: int | None = None,
    acquisition_path: AcquisitionPath = AcquisitionPath.UNKNOWN,
) -> tuple[GenerationEvent, Take, ReleaseArtifact]:
    metadata = clip.metadata
    task = captured_task(clip)
    raw_parameters: dict[str, ParameterValue] = {}
    for key in (
        "weirdness_constraint",
        "style_weight",
        "audio_weight",
        "duration",
        "model_name",
    ):
        if key in metadata:
            raw_parameters[key] = ParameterValue(
                value=metadata[key],
                provenance=ProvenanceRecord(
                    provenance=Provenance.CAPTURED,
                    source="Suno public payload",
                ),
            )
    control_sliders = metadata.get("control_sliders")
    if isinstance(control_sliders, dict):
        for key, value in control_sliders.items():
            raw_parameters[key] = ParameterValue(
                value=value,
                provenance=ProvenanceRecord(
                    provenance=Provenance.CAPTURED,
                    source=f"Suno public payload metadata.control_sliders.{key}",
                ),
            )
    event = GenerationEvent(
        id=event_id or f"event_{clip.id}",
        project_id=project_id,
        brief_id=brief_id,
        source_material_ids=source_material_ids,
        task=task,
        batch_id=batch_id,
        raw_metadata=metadata,
        parameters=raw_parameters,
    )
    take = Take(
        id=f"take_{clip.id}",
        project_id=project_id,
        generation_event_id=event.id,
        batch_index=(
            batch_index
            if batch_index is not None
            else clip.raw_payload.get("batch_index")
            if isinstance(clip.raw_payload.get("batch_index"), int)
            else None
        ),
        platform_id=clip.id,
    )
    operation = {
        TaskType.EDIT_CROP: OperationType.CROP,
        TaskType.COVER: OperationType.COVER,
        TaskType.EXTEND: OperationType.EXTEND,
        TaskType.REPLACE_SECTION: OperationType.REPLACE_SECTION,
        TaskType.REMASTER: OperationType.REMASTER,
    }.get(task, OperationType.RAW)
    artifact = ReleaseArtifact(
        id=f"artifact_{clip.id}",
        project_id=project_id,
        take_id=take.id,
        title=clip.title,
        platform_id=clip.id,
        url=clip.source_url,
        operation=operation,
        acquisition_path=acquisition_path,
        platform_reported_duration_s=clip.duration,
        raw_payload=clip.raw_payload,
    )
    return event, take, artifact


def load_suno_snapshots(path: str | Path) -> list[SunoClipSnapshot]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SunoImportError("snapshot file must contain a list of clips")
    clips = [SunoClipSnapshot.model_validate(item) for item in payload]
    if not clips:
        raise SunoImportError("snapshot file contains no clips")
    return clips


def _consensus_text(
    clips: Iterable[SunoClipSnapshot],
    key: str,
) -> tuple[str | None, int, int]:
    clips = tuple(clips)
    values = [
        value.strip()
        for clip in clips
        if isinstance((value := clip.metadata.get(key)), str) and value.strip()
    ]
    if not values:
        return None, 0, len(clips)
    counts = {value: values.count(value) for value in set(values)}
    selected, count = max(counts.items(), key=lambda item: item[1])
    return (
        selected if count > len(clips) / 2 else None,
        count,
        len(clips),
    )


def _audio_suffix(clip: SunoClipSnapshot) -> str:
    if clip.audio_url:
        suffix = Path(urlparse(clip.audio_url).path).suffix.lower()
        if suffix in {".wav", ".mp3", ".m4a", ".flac", ".ogg"}:
            return suffix
    return ".mp3"


def _acquisition_path(path: Path) -> AcquisitionPath:
    return (
        AcquisitionPath.LOSSLESS_PLATFORM_DOWNLOAD
        if path.suffix.lower() in {".wav", ".flac"}
        else AcquisitionPath.CDN_LOSSY
    )


def cache_local_audio(source: Path, destination: Path) -> Path:
    source = source.expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(source)
    audio_identity(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if audio_identity(destination)[0] != audio_identity(source)[0]:
            raise SunoImportError(
                f"cached parent path already contains different audio: {destination}"
            )
        return destination
    temporary = destination.with_name(f".{destination.name}.part")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    try:
        shutil.copy2(source, temporary)
        audio_identity(temporary)
        temporary.replace(destination)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise
    return destination


def build_suno_project(
    clips: Iterable[SunoClipSnapshot],
    *,
    project_id: str,
    title: str,
    media_dir: str | Path,
    client: SunoPublicClient | None = None,
    download_audio: bool = True,
    lyrics: str | None = None,
    style: str | None = None,
    exclude: str | None = None,
    parent_declarations: Iterable[ParentDeclaration] = (),
) -> IntakeResult:
    """Build a complete, hydrated project from public snapshots.

    The intake action is the user's declaration that the supplied clips are
    candidates for one comparison project. It does not assert that they came
    from one hidden GenerationEvent.
    """
    clips = tuple(clips)
    if not clips:
        raise SunoImportError("at least one Suno clip is required")
    if len({clip.id for clip in clips}) != len(clips):
        raise SunoImportError("duplicate clip IDs in intake set")
    destination = Path(media_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    common_lyrics, lyrics_count, clip_count = _consensus_text(clips, "prompt")
    common_style, style_count, _ = _consensus_text(clips, "tags")
    common_exclude, exclude_count, _ = _consensus_text(clips, "negative_tags")
    effective_lyrics = lyrics if lyrics is not None else common_lyrics or ""
    effective_style = style if style is not None else common_style
    effective_exclude = exclude if exclude is not None else common_exclude
    field_provenance: dict[str, ProvenanceRecord] = {}
    if lyrics is not None:
        field_provenance["lyrics"] = ProvenanceRecord(
            provenance=Provenance.DECLARED,
            source="intake option",
        )
    elif common_lyrics is not None:
        field_provenance["lyrics"] = ProvenanceRecord(
            provenance=Provenance.CAPTURED,
            confidence=(
                Confidence.HIGH if lyrics_count == clip_count else Confidence.MEDIUM
            ),
            source=(f"modal Suno metadata.prompt in {lyrics_count}/{clip_count} clips"),
        )
        if lyrics_count != clip_count:
            warnings.append(
                f"Brief lyrics use the modal captured prompt "
                f"({lyrics_count}/{clip_count}); outlier prompts remain in raw metadata"
            )
    else:
        field_provenance["lyrics"] = ProvenanceRecord(
            provenance=Provenance.UNKNOWN,
            note="candidate prompts differ or are unavailable",
        )
        warnings.append(
            "candidate prompts differ or are unavailable; Brief lyrics remain unknown"
        )
    if style is not None:
        field_provenance["style"] = ProvenanceRecord(
            provenance=Provenance.DECLARED,
            source="intake option",
        )
    elif common_style is not None:
        field_provenance["style"] = ProvenanceRecord(
            provenance=Provenance.CAPTURED,
            confidence=(
                Confidence.HIGH if style_count == clip_count else Confidence.MEDIUM
            ),
            source=f"modal Suno metadata.tags in {style_count}/{clip_count} clips",
        )
    if exclude is not None:
        field_provenance["exclude"] = ProvenanceRecord(
            provenance=Provenance.DECLARED,
            source="intake option",
        )
    elif common_exclude is not None:
        field_provenance["exclude"] = ProvenanceRecord(
            provenance=Provenance.CAPTURED,
            confidence=(
                Confidence.HIGH if exclude_count == clip_count else Confidence.MEDIUM
            ),
            source=(
                "modal Suno metadata.negative_tags in "
                f"{exclude_count}/{clip_count} clips"
            ),
        )
    requirements: list[BriefRequirement] = []
    if lyrics is not None and lyrics.strip():
        requirements.append(
            BriefRequirement(
                id="frozen_lyrics",
                label="Frozen lyrics must remain correct and intelligible",
                value=lyrics,
                hard=True,
                burden_bearing=True,
                provenance=field_provenance["lyrics"],
            )
        )
    elif common_lyrics is not None:
        requirements.append(
            BriefRequirement(
                id="captured_lyrics",
                label="Captured generation lyrics and section order",
                value=common_lyrics,
                provenance=field_provenance["lyrics"],
            )
        )
    if style is not None and style.strip():
        requirements.append(
            BriefRequirement(
                id="declared_style",
                label="Declared style intent",
                value=style,
                provenance=field_provenance["style"],
            )
        )
    elif common_style is not None:
        requirements.append(
            BriefRequirement(
                id="captured_style",
                label="Captured generation style intent",
                value=common_style,
                provenance=field_provenance["style"],
            )
        )
    brief = CreativeBriefVersion(
        id=f"brief_{slugify(project_id)}_v1",
        project_id=project_id,
        version="v1",
        lyrics=effective_lyrics,
        style=effective_style,
        exclude=effective_exclude,
        requirements=tuple(requirements),
        field_provenance=field_provenance,
    )

    owns_client = client is None
    active_client = client or SunoPublicClient()
    try:
        records: dict[str, tuple[GenerationEvent, Take, ReleaseArtifact]] = {}
        snapshots: list[AcquisitionSnapshot] = []
        for clip in clips:
            local_path: Path | None = None
            acquisition_path = AcquisitionPath.UNKNOWN
            if download_audio:
                local_path = active_client.download_audio(
                    clip,
                    destination / f"{clip.id}{_audio_suffix(clip)}",
                )
                acquisition_path = _acquisition_path(local_path)
            event, take, artifact = clip_to_generation_records(
                clip,
                project_id=project_id,
                brief_id=brief.id,
                batch_id=(
                    str(clip.raw_payload["batch_id"])
                    if clip.raw_payload.get("batch_id") is not None
                    else None
                ),
                acquisition_path=acquisition_path,
            )
            if local_path is not None:
                digest, encoding, measured_duration = audio_identity(local_path)
                artifact = artifact.model_copy(
                    update={
                        "local_path": str(local_path),
                        "file_sha256": digest,
                        "encoding": encoding,
                        "measured_file_duration_s": measured_duration,
                    }
                )
            records[clip.id] = (event, take, artifact)
            snapshots.append(
                make_acquisition_snapshot(
                    project_id=project_id,
                    url=clip.source_url,
                    platform_id=clip.id,
                    raw_payload=clip.raw_payload,
                    file_sha256=artifact.file_sha256,
                    existing=snapshots,
                )
            )

        edges: list[ArtifactEdge] = []
        edge_ids: set[str] = set()
        extra_records: list[tuple[GenerationEvent, Take, ReleaseArtifact]] = []
        resolved_parent_inputs: dict[
            str, tuple[GenerationEvent, Take, ReleaseArtifact]
        ] = {}
        for declaration in parent_declarations:
            child_records = records.get(declaration.child_clip_id)
            if child_records is None:
                raise SunoImportError(
                    f"unknown child clip ID: {declaration.child_clip_id}"
                )
            child_artifact = child_records[2]
            parent_records = records.get(
                declaration.parent
            ) or resolved_parent_inputs.get(declaration.parent)
            if parent_records is None:
                parent_value = declaration.parent
                parsed = urlparse(parent_value)
                if parsed.scheme in {"http", "https"}:
                    parent_clips = active_client.fetch(parent_value)
                    if len(parent_clips) != 1:
                        raise SunoImportError(
                            "declared parent URL must resolve to exactly one clip"
                        )
                    parent_clip = parent_clips[0]
                    parent_records = records.get(parent_clip.id)
                    if parent_records is None:
                        parent_path = active_client.download_audio(
                            parent_clip,
                            destination
                            / f"{parent_clip.id}{_audio_suffix(parent_clip)}",
                        )
                        parent_event, parent_take, parent_artifact = (
                            clip_to_generation_records(
                                parent_clip,
                                project_id=project_id,
                                brief_id=brief.id,
                                acquisition_path=_acquisition_path(parent_path),
                            )
                        )
                        digest, encoding, measured_duration = audio_identity(
                            parent_path
                        )
                        parent_artifact = parent_artifact.model_copy(
                            update={
                                "local_path": str(parent_path),
                                "file_sha256": digest,
                                "encoding": encoding,
                                "measured_file_duration_s": measured_duration,
                                "raw_payload": {
                                    **parent_artifact.raw_payload,
                                    "analysis_role": "lineage_only",
                                },
                            }
                        )
                        parent_records = (
                            parent_event,
                            parent_take,
                            parent_artifact,
                        )
                        snapshots.append(
                            make_acquisition_snapshot(
                                project_id=project_id,
                                url=parent_clip.source_url,
                                platform_id=parent_clip.id,
                                raw_payload=parent_clip.raw_payload,
                                file_sha256=digest,
                                existing=snapshots,
                            )
                        )
                else:
                    parent_source = Path(parent_value)
                    suffix = parent_source.suffix.lower() or ".audio"
                    parent_path = cache_local_audio(
                        parent_source,
                        destination
                        / (
                            "declared-parent-"
                            f"{slugify(declaration.child_clip_id)}{suffix}"
                        ),
                    )
                    parent_id = f"declared_parent_{slugify(declaration.child_clip_id)}"
                    parent_event = GenerationEvent(
                        id=f"event_{parent_id}",
                        project_id=project_id,
                        brief_id=brief.id,
                        task=TaskType.UNKNOWN,
                        raw_metadata={
                            "declared_as_parent_for": declaration.child_clip_id
                        },
                    )
                    parent_take = Take(
                        id=f"take_{parent_id}",
                        project_id=project_id,
                        generation_event_id=parent_event.id,
                    )
                    digest, encoding, measured_duration = audio_identity(parent_path)
                    parent_artifact = ReleaseArtifact(
                        id=f"artifact_{parent_id}",
                        project_id=project_id,
                        take_id=parent_take.id,
                        title=(
                            "Declared parent of "
                            f"{child_artifact.title or declaration.child_clip_id}"
                        ),
                        local_path=str(parent_path),
                        file_sha256=digest,
                        operation=OperationType.RAW,
                        acquisition_path=AcquisitionPath.USER_PROVIDED_UNKNOWN,
                        encoding=encoding,
                        measured_file_duration_s=measured_duration,
                        raw_payload={"analysis_role": "lineage_only"},
                    )
                    parent_records = (parent_event, parent_take, parent_artifact)
                resolved_parent_inputs[declaration.parent] = parent_records
                main_artifact_ids = {item[2].id for item in records.values()}
                extra_artifact_ids = {item[2].id for item in extra_records}
                if (
                    parent_records[2].id not in main_artifact_ids
                    and parent_records[2].id not in extra_artifact_ids
                ):
                    extra_records.append(parent_records)
            parent_artifact = parent_records[2]
            edge_id = f"edge_{slugify(parent_artifact.id)}_{slugify(child_artifact.id)}"
            if edge_id in edge_ids:
                raise SunoImportError(
                    "duplicate parent declaration resolves to the same "
                    f"artifact pair: {parent_artifact.id} -> {child_artifact.id}"
                )
            edge_ids.add(edge_id)
            deterministic = False
            source_interval: tuple[float, float] | None = None
            verified_run_id: str | None = None
            if (
                child_artifact.operation == OperationType.CROP
                and child_artifact.local_path
                and parent_artifact.local_path
            ):
                verification = verify_deterministic_crop(
                    parent_artifact.local_path,
                    child_artifact.local_path,
                    parent_artifact_id=parent_artifact.id,
                    child_artifact_id=child_artifact.id,
                    analysis_run_id="intake_crop_verification",
                )
                deterministic = verification.verified
                if verification.verified:
                    source_interval = (
                        verification.lag_s,
                        verification.lag_s
                        + (child_artifact.measured_file_duration_s or 0.0),
                    )
                    verified_run_id = verification.analysis_run_id
                else:
                    warnings.append(
                        f"{declaration.child_clip_id}: declared Crop parent did "
                        "not pass deterministic sample verification"
                    )
            edges.append(
                ArtifactEdge(
                    id=edge_id,
                    project_id=project_id,
                    parent_artifact_id=parent_artifact.id,
                    child_artifact_id=child_artifact.id,
                    operation=child_artifact.operation,
                    generation_event_id=child_records[0].id,
                    source_interval_s=source_interval,
                    deterministic=deterministic,
                    evidence_inheritance={
                        "audio_identity": (
                            EvidenceInheritance.INHERIT_PRESERVED_REGION
                            if deterministic
                            else EvidenceInheritance.RECOMPUTE
                        )
                    },
                    provenance=ProvenanceRecord(
                        provenance=Provenance.DECLARED,
                        source="intake parent declaration",
                    ),
                    verified_run_id=verified_run_id,
                )
            )
    finally:
        if owns_client:
            active_client.close()

    all_records = [*records.values(), *extra_records]
    manifest = ProjectManifest(
        project_id=project_id,
        title=title,
        briefs=[brief],
        acquisition_snapshots=snapshots,
        sources=[],
        generation_events=[item[0] for item in all_records],
        takes=[item[1] for item in all_records],
        artifacts=[item[2] for item in all_records],
        edges=edges,
    )
    return IntakeResult(manifest=manifest, warnings=tuple(warnings))


def build_local_project(
    paths: Iterable[str | Path],
    *,
    project_id: str,
    title: str,
    media_dir: str | Path,
    lyrics: str | None = None,
    style: str | None = None,
    exclude: str | None = None,
) -> IntakeResult:
    """Copy local candidates byte-for-byte into a stable project media cache."""
    paths = tuple(Path(path).expanduser().resolve() for path in paths)
    if not paths:
        raise SunoImportError("at least one local audio file is required")
    destination = Path(media_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    declared = ProvenanceRecord(
        provenance=Provenance.DECLARED,
        source="local intake option",
    )
    requirements: list[BriefRequirement] = []
    if lyrics is not None and lyrics.strip():
        requirements.append(
            BriefRequirement(
                id="frozen_lyrics",
                label="Frozen lyrics must remain correct and intelligible",
                value=lyrics,
                hard=True,
                burden_bearing=True,
                provenance=declared,
            )
        )
    if style is not None and style.strip():
        requirements.append(
            BriefRequirement(
                id="declared_style",
                label="Declared style intent",
                value=style,
                provenance=declared,
            )
        )
    brief = CreativeBriefVersion(
        id=f"brief_{slugify(project_id)}_v1",
        project_id=project_id,
        version="v1",
        lyrics=lyrics or "",
        style=style,
        exclude=exclude,
        requirements=tuple(requirements),
        field_provenance={
            key: declared
            for key, value in (
                ("lyrics", lyrics),
                ("style", style),
                ("exclude", exclude),
            )
            if value is not None
        },
    )
    events: list[GenerationEvent] = []
    takes: list[Take] = []
    artifacts: list[ReleaseArtifact] = []
    for index, source in enumerate(paths, start=1):
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(source)
        original_digest, _, _ = audio_identity(source)
        suffix = source.suffix.lower() or ".audio"
        stable_name = (
            f"local-{index:02d}-{slugify(source.stem)}-{original_digest[:12]}{suffix}"
        )
        cached = cache_local_audio(source, destination / stable_name)
        digest, encoding, duration = audio_identity(cached)
        stable_id = f"local_{index:02d}_{digest[:16]}"
        event = GenerationEvent(
            id=f"event_{stable_id}",
            project_id=project_id,
            brief_id=brief.id,
            task=TaskType.UNKNOWN,
            batch_id="local-intake",
            raw_metadata={
                "source_filename": source.name,
                "source_kind": "user_provided_local_audio",
            },
        )
        take = Take(
            id=f"take_{stable_id}",
            project_id=project_id,
            generation_event_id=event.id,
            batch_index=index - 1,
        )
        artifact = ReleaseArtifact(
            id=f"artifact_{stable_id}",
            project_id=project_id,
            take_id=take.id,
            title=source.stem,
            local_path=str(cached),
            file_sha256=digest,
            acquisition_path=AcquisitionPath.USER_PROVIDED_UNKNOWN,
            encoding=encoding,
            measured_file_duration_s=duration,
            raw_payload={
                "intake_kind": "local",
                "source_filename": source.name,
            },
        )
        events.append(event)
        takes.append(take)
        artifacts.append(artifact)
    return IntakeResult(
        manifest=ProjectManifest(
            project_id=project_id,
            title=title,
            briefs=[brief],
            sources=[],
            generation_events=events,
            takes=takes,
            artifacts=artifacts,
        ),
        warnings=(),
    )
