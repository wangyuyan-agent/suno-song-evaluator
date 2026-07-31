# Claude Design prototype

This directory preserves the interactive visual prototype exported from
[Claude Design](https://claude.ai/design/p/e09b6bad-ae79-4777-ae2b-ce73032263a4).

## Run locally

Serve this directory over HTTP so the companion script resolves correctly:

```bash
cd design/claude
python3 -m http.server 4173
```

Then open:

<http://127.0.0.1:4173/Suno%20Song%20Evaluator.dc.html>

The prototype is a design artifact. Its waveforms are explicitly illustrative,
its form state is not persisted, and it is not connected to the evaluator API.
Do not treat values shown in the prototype as newly measured evidence.

## Export integrity

SHA-256 values of the original Claude Design export:

```text
ba78bc6161a4c4687e66c34f35ee28b6674d7dc92396e775af2c5b1eae543f25  Suno Song Evaluator.dc.html
8fe7df74405f3c55f49b7249c74ea1397e65d07dea2b1bd3b4a489bec2e28cbe  support.js
```
