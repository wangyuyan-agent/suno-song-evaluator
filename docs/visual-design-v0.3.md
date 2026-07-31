# Visual design handoff v0.3

## Outcome

The visual direction is **Premium Minimalism**: warm porcelain surfaces,
near-black type, cobalt as the single action accent, precise hairlines, restrained
tonal depth, and a dark, studio-like blind-listening workspace. The design uses
spacing, typography, and contrast to establish hierarchy instead of gradients,
decorative cards, or oversized effects.

The interactive source is preserved in
[`design/claude/Suno Song Evaluator.dc.html`](../design/claude/Suno%20Song%20Evaluator.dc.html).
It contains four navigable product screens:

1. **Project overview** — evidence completeness, a lexical recommendation,
   candidate records, uncertainty, and provenance.
2. **Blind A/B listening** — identity-safe paired playback, seven empty human
   judgment fields, notes, and keyboard controls.
3. **Named review** — frozen lyric-line confirmation, ending-boundary listening,
   acquisition limitations, and release-policy confirmation.
4. **Reference → Suno plan** — a separate 《春》 example that keeps v2.0 as the
   edit parent and fallback while expressing the 16-second v1.4 reference as a
   structural gesture.

## Factual-integrity contract

The prototype follows the product's evidence rules:

- no total score and no similarity winner;
- unknown lineage stays `来源未知`;
- absent measurements stay `未采集`;
- waveforms are labelled `示意渲染`;
- blind mode hides record identity, lineage, and full duration;
- loudness matching is stated without inventing a numeric target;
- ASR is a locator only and cannot confirm a lyric defect;
- an active ending requires audible human confirmation;
- an LLM may narrate structured evidence but cannot create measurements;
- melody or rhythm retention abstains when no valid comparison source exists.

The 《十七》 example uses the captured records and declared preferences:

| Record | Duration | Preserved evidence |
| --- | ---: | --- |
| `17-v9-corp-1` | 3:48 | Crop; preferred for `声音曲调饱满温暖` |
| `17-v9` | 4:34 | Runner-up; `很抓人，末段有合唱和声` |
| `17-v9.3` | 4:17 | Third preference |
| `17-v9` | 4:15 | Lower preference only |
| `17-v9.3` | 3:34 | Ending boundary needs audible confirmation |

No platform version, hidden generation parameter, loudness figure, checksum,
source path, or rights conclusion is invented for visual realism.

## Design system

### Surfaces and color

- Porcelain canvas for evidence and review screens.
- Near-black rail and listening workspace for focus and identity safety.
- Cobalt for primary actions, focus, current playback, and selected state.
- Amber is reserved for uncertainty or required human confirmation.
- Green is reserved for evidence that is actually recorded or measured.

Status must never rely on color alone; every state retains a text label.

### Typography and density

- A restrained Chinese sans-serif stack with optical size contrast.
- Small uppercase English eyebrow labels support scanning without competing
  with Chinese task labels.
- Dense evidence tables use hairline separation and aligned numeric columns.
- Generous section rhythm is paired with compact controls; whitespace carries
  hierarchy rather than empty decoration.

### Interaction

- The four screens share one persistent navigation rail and project context.
- Blind listening supports `Space`, `A`, `B`, arrow keys, `L`, and `N`.
- Waveform, playhead, tick marks, and click-to-seek use one coordinate space.
- Rapid `±5s` actions accumulate from live playback state.
- The layout collapses to one column at 1180 px and 900 px, then uses a compact
  fixed rail below 760 px.
- Muted informational text was raised to readable contrast; intentionally
  disabled controls remain visually distinct.

## Runtime implementation

The Claude Design export remains an immutable interactive specification. The
production presentation layer now lives in `src/songeval/web/` and preserves
the existing analysis, persistence, and API model:

| Prototype screen | Existing runtime source |
| --- | --- |
| Project overview | project/report/recommendation payloads |
| Blind A/B listening | opaque session payload and round submission endpoints |
| Named review | named-review payload, lyric locator, and confirmation endpoints |
| Reference → Suno plan | reference directive and capability-aware plan payload |

The implementation:

1. uses a semantic shell, reusable view functions, and one shared token
   stylesheet without adding a second package or build system;
2. replaces all demo records with `/workspace-context` and existing API state;
3. decodes the current local candidate and stimulus audio for waveform drawing;
4. persists named-review drafts locally and blind drafts per opaque session;
5. surfaces validation and request failures in live status regions;
6. preserves missing evidence, blind identity, policy confirmation, and
   abstention invariants in endpoint tests.

The visual layer must never turn a missing value into plausible-looking content.

## Fidelity ledger

| Accepted design point | Production result |
| --- | --- |
| Warm porcelain workspace with near-black type | Preserved through shared canvas, surface, ink, and hairline tokens |
| Cobalt reserved for primary action/focus | Preserved across workspace actions, active audio, focus, and selection |
| Dark studio-like blind screen | Preserved with opaque stimuli, real waveforms, fixed round controls, and no candidate identity |
| Compact persistent navigation rail | Preserved on desktop and reduced to a 64 px rail on narrow screens |
| Evidence-rich overview without a total score | Bound to the current report, four separate axes, completeness, uncertainty, and provenance |
| Named review is human-gated | Ending choice remains disabled until the final ten seconds are actually played; seeking alone does not unlock it |
| Reference plan does not become a Sample workflow | Registered as local evidence; plan keeps the selected target as edit parent and fallback |
| Demo waveform disclosure | Replaced intentionally: production waveforms are decoded from current audio and labelled as such |

Desktop and 390 px mobile browser acceptance covered overview and blind
workspaces. The production UI also passed keyboard outcome selection,
play/seek, draft recovery, incomplete-round blocking, named-policy validation,
reference registration, plan generation, and zero-console-error checks.
