# YuNet Face-Detection Model — Provenance & Management

How to obtain, validate, and (deliberately) replace the ONNX model used by
[`src/face_detection.py`](../src/face_detection.py) (`cv2.FaceDetectorYN`,
YuNet). **This is a manual operator task — the application never downloads
the model during startup or at any other time.**

## Why the model is required

Phase 1 Step 05 implemented `FaceDetector`, which locates faces in camera
frames via `cv2.FaceDetectorYN`. That backend is compiled into the pinned
`opencv-python==5.0.0.93` package, but **the ONNX weight file is not
bundled** and opencv-python 5.x ships no cascade alternatives.
`FaceDetector.load()` therefore fails with a clear
`FaceDetectorLoadError` until the operator supplies the model at
`models/yunet.onnx`.

## Official source and provenance

| Field | Value |
|---|---|
| Model | YuNet face detection, full-precision (FP32) ONNX variant |
| Upstream file name | `face_detection_yunet_2023mar.onnx` |
| Official repository | [github.com/opencv/opencv_zoo/models/face_detection_yunet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) and the OpenCV-maintained [huggingface.co/opencv/face_detection_yunet](https://huggingface.co/opencv/face_detection_yunet) |
| Selected revision (pinned) | `3cc26e7f1014a5ee5d74a42acee58bafc9d0a310` (Hugging Face repo commit — immutable; not the mutable `main` branch) |
| Download URL (pinned) | `https://huggingface.co/opencv/face_detection_yunet/resolve/3cc26e7f1014a5ee5d74a42acee58bafc9d0a310/face_detection_yunet_2023mar.onnx` |
| SHA-256 | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| Size | 232,589 bytes |
| Destination | `models/yunet.onnx` (exactly what `FaceDetector` uses by default) |
| Source metadata checked | 2026-10-09 |

### How the checksum was verified

The SHA-256 and byte size above were **not** merely calculated from a local
download. They were verified against two independent upstream sources:

1. The OpenCV Zoo repository README tracks the same March 2023 artifact,
   and the upstream model source (libfacedetection.train) publishes the
   ONNX export; the OpenCV Hub page is the canonical distribution the
   OpenCV documentation points to.
2. The **Hugging Face Hub resolve API** published the artifact's content
   hash in its `X-Linked-ETag` response header (`8f2383e4…2fa4`) and its
   size in `X-Linked-Size` (`232589`) for the pinned revision. These
   headers are produced by the Hub's storage layer for the pinned commit,
   independent of any local computation.

The values are pinned as constants in
[`tools/download_yunet_model.py`](../tools/download_yunet_model.py)
(`YUNET_SHA256`, `YUNET_SIZE`, `YUNET_URL`) — they are never taken from
the network at download time, and the helper never accepts a URL or
digest override from the command line.

## License and attribution

The YuNet model files in the upstream repository are **MIT licensed**
(copyright 2020 Shiqi Yu; see the upstream `LICENSE` file). Redistribution
and use are permitted with the standard MIT notice. If you redistribute
the model (e.g. in a deployment bundle), include the MIT license text and
cite the upstream work:

> Wu, Wei, Hanyang Peng, and Shiqi Yu. "Yunet: A tiny millisecond-level
> face detector." *Machine Intelligence Research* 20, no. 5 (2023): 656–665.

The model itself is *not* redistributed via this repository's Git history
(see "Git protection" below) — it is downloaded at deployment time from
the official source, so upstream licensing is preserved automatically.

## Variant and OpenCV compatibility

The FP32 `face_detection_yunet_2023mar.onnx` variant is selected (not the
`int8`/`int8bq` quantized variants) because:

- the full-precision variant is the reference artifact used throughout the
  OpenCV documentation and `FaceDetectorYN` examples;
- quantized variants trade accuracy and can have different loading
  behavior across OpenCV builds; the installed
  `opencv-python==5.0.0.93` initializes the FP32 model cleanly;
- the project's detection confidence threshold and frame contract
  (`CameraService.read_frame()` → BGR `(H, W, 3)` uint8) are validated
  against the FP32 variant in the Step 05 test suite.

After verification the helper additionally proves usability by
initializing a real `cv2.FaceDetectorYN` from the downloaded bytes — the
same backend the production detector uses. No camera is opened and no
application logic runs during this check.

## Manual usage

All commands are run from the repository root (or anywhere — paths are
resolved independently of the current working directory).

### Download and install (normal operation)

```bash
python tools/download_yunet_model.py
```

Downloads from the pinned URL, verifies HTTPS, byte size, SHA-256,
content sniffs (not HTML/LFS/Git-LFS pointer), a structural ONNX check
(the bytes must parse as an ONNX `ModelProto` protobuf header — ONNX
has no guaranteed leading magic bytes, so a literal-marker check would
wrongly reject e.g. the pytorch-produced artifact), and OpenCV
loadability, then atomically installs to `models/yunet.onnx`.

The OpenCV validation step runs against a short-lived `.onnx`
cross-check copy, not the `.tmp` download file itself: OpenCV's DNN
loader dispatches model-format detection by *filename extension*, so a
`.tmp` file is rejected with `Cannot determine an origin framework`
even when the bytes are a perfectly valid ONNX model (confirmed with
OpenCV 5.0.0). The cross-check copy is byte-identical to the verified
download, lives in the same directory (same filesystem, so the final
`os.replace` stays atomic), and is removed on every exit path — success,
validation failure, or cancellation.

### Validate an existing model (no download)

```bash
python tools/download_yunet_model.py --validate-only
```

Checks an existing `models/yunet.onnx` against size, SHA-256, content
sniffs, the same structural ONNX check, and OpenCV loadability (also
via a short-lived `.onnx` cross-check copy — see above).
**Non-destructive**: the file is never modified, whichever way the
check exits.

### Replace an existing model (explicit opt-in only)

An existing destination is **never** overwritten by default — the helper
exits with an error and tells you how to validate instead.

```bash
# Interactive (explicit typed confirmation 'REPLACE' is required):
python tools/download_yunet_model.py --replace

# Noninteractive / CI (both flags required):
python tools/download_yunet_model.py --replace --yes
```

Replacement downloads to a temporary file first, verifies it *fully*
(checksum + size + content + OpenCV initialization), and only then swaps
it in with an atomic move. If anything fails, the original model file is
left exactly as it was.

### Custom destination (rare)

```bash
python tools/download_yunet_model.py --dest path/to/model.onnx
```

## Troubleshooting

| Symptom | Meaning / action |
|---|---|
| `YuNet ONNX model not found…` from `FaceDetector.load()` | Model missing — run the download helper, then re-run the app. This is the *expected* offline failure path; the app never downloads for you. |
| `error: SHA-256 mismatch` | Download was corrupted, tampered with, or the upstream artifact really changed. **Do not** bypass; re-run to get a consistent attempt, inspect the pinned provenance table above, and raise an issue if the upstream digest has genuinely changed. |
| `error: server returned an HTML page…` | Usually a rate limit or auth wall (Hugging Face throttles unauthenticated traffic). Wait briefly and retry; do not disable verification. |
| `error: Git LFS pointer` | The source served a pointer file instead of the artifact — retry, or check whether the pinned revision is still served. |
| `error: HTTP error 4xx/5xx` | Upstream availability problem; retry later. The helper makes exactly one attempt per invocation (no retry loops, no runaway traffic). |
| `error: OpenCV could not initialize…` | The bytes authenticate (checksum OK) but the installed OpenCV cannot load them — an environment/compatibility problem, reported as-is. |
| `error: model file already exists` | Default no-overwrite protection. Use `--validate-only` to check the existing file, or `--replace` (with confirmation) to authorize replacement. |

## Security model (what this helper refuses to do)

- **No arbitrary inputs.** The URL, digest, and size are fixed, audited
  constants; the CLI has no `--url`/`--digest` overrides.
- **No trust-on-first-use.** A download's checksum is compared against
  the pre-verified constant only — never against itself, and never
  skipped because a server header is absent.
- **No untrusted redirects.** Every redirect hop — including hops emitted by
  earlier redirects — is followed manually and validated before the request
  is made: HTTPS-only, no credentials/userinfo, and an exact hostname
  allowlist covering the Hub itself plus its official storage/CDN endpoints
  the ``resolve`` endpoint redirects to (``huggingface.co``,
  ``us.aws.cdn.hf.co``/``eu.aws.cdn.hf.co``/``ap.aws.cdn.hf.co``,
  ``cas-bridge.xethub.hf.co``/``cas-server.xethub.hf.co``,
  ``cdn-lfs*.huggingface.co``/``cdn-lfs*.hf.co``). No substring matching,
  no credentials or sensitive headers are ever forwarded, the final
  effective URL is re-checked, and the chain is capped at 3 hops with
  loop detection; anything else aborts.
- **No silent replacement.** Existing models are modified only through
  the explicit, confirmed replacement flow above.
- **No partial installs.** The final path is written only by one atomic
  `os.replace` after every validation step; failures clean up their own
  temp files and leave the destination untouched.
- **No automatic downloads anywhere.** The attendance application, its
  config, its tests, and its import-time code never invoke this helper.

## Git protection

The helper installs the model into `models/`, which is **git-ignored**
(`models/**` in `.gitignore`, with the directory kept present via a
`.gitkeep`). The model binary is intentionally excluded from version
control and must not be committed.
