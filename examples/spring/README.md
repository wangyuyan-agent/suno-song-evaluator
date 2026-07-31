# 《春》 acceptance dataset

The manifest references audio instead of copying it into the repository:

```bash
export SPRING_AUDIO_DIR="/path/to/downloaded-spring-candidates"
export SPRING_REFERENCE_AUDIO="/path/to/春-v1.4-锚点16s.wav"
```

The three candidate WAV files are the user downloads. The 16.36-second file is
the supplied v1.4 reference crop. The full v1.6 parent is represented as a
lineage-only artifact because it was not one of the three supplied release
candidates.

On 2026-07-29 the deterministic Crop relation was independently reverified
against the public CDN copy of full v1.6:

- lag: `0.005 s`
- retained-region Pearson correlation: `0.9983544273571525`
- configured positive threshold: `r >= 0.995`

`lag` is the measured start offset inside the parent, not an error tolerance;
an exact middle-region crop is therefore valid when its retained samples match.

The example policy intentionally contains only the priority the user explicitly
declared. It does not silently accept the proposed Compliance floor. Therefore
`review-pending.json` must produce an abstention, while still producing all
objective measurements, comparisons, hotspots, and preflight findings.

`b-route-plan.json` is the frozen single-use Suno Pro plan: keep v2.0-1 as the
edit parent, do not attach the 16-second crop as a new Sample, and use one local
Replace Section to request the structural gesture. It explicitly falls back to
the unmodified v2.0-1 after two failed batches.
