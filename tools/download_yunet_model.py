"""Model-management CLI for the AI-Powered Visual Attendance System.

Manually invoked helper that downloads the official YuNet ONNX face-detection
model (required by :mod:`src.face_detection`) into ``models/yunet.onnx`` and
verifies it before installation.

**This helper is never invoked by the application.** ``FaceDetector.load()``
fails with a clear error when the model is missing; the operator then runs
this script by hand. Imports here have no side effects: no network access,
no filesystem writes, no device work.

Design rules
------------

* **Standard library only** for the I/O path: ``urllib.request``,
  ``hashlib``, ``tempfile``, ``argparse``, ``pathlib``. The only third-party
  package touched (``cv2``, for the Section 8 model-validation step) is an
  existing project dependency — no new requirements.
* **Hard-coded provenance.** The source URL and expected SHA-256 are fixed,
  independently verified constants (see ``docs/YUNET_MODEL.md``). The CLI
  accepts no arbitrary URL and no arbitrary checksum.
* **Trust nothing unverified.** Checksum + byte size + content sniffing +
  a real ``cv2.FaceDetectorYN`` initialization on the downloaded bytes must
  all pass before the file is installed. A verification failure never
  installs the model and never modifies the destination.
* **Never overwrite silently.** An existing destination requires explicit
  ``--replace`` (plus ``--yes`` when noninteractive, or a typed confirmation
  when interactive).
* **Atomic install.** The model is streamed into a uniquely named temporary
  file in the destination directory and moved into place with
  :func:`os.replace` only after every validation step succeeds. Temporary
  files created by a failed invocation are cleaned up; unrelated files are
  never touched.

Provenance (verified 2026-10-09 — see docs/YUNET_MODEL.md for the full record):

* Source:    huggingface.co/opencv/face_detection_yunet — the official
             OpenCV-maintained model repository
* Revision:  ``3cc26e7f1014a5ee5d74a42acee58bafc9d0a310`` (pinned commit)
* File:      ``face_detection_yunet_2023mar.onnx`` (FP32, full-precision)
* SHA-256:   ``8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4``
             — confirmed against the uplink-published LISS ``X-Linked-ETag``
             header (the Hub's authoritative content hash), not merely
             calculated locally
* Size:      232,589 bytes — same independent header source
* License:   MIT (copyright 2020 Shiqi Yu; see the repo's LICENSE file)
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import IO, Final

# ---------------------------------------------------------------------------
# Verified provenance constants (docs/YUNET_MODEL.md is the audit record)
# ---------------------------------------------------------------------------

#: Official OpenCV-maintained repository and file, pinned to an immutable
#: commit instead of the mutable ``main`` branch.
YUNET_REPO_REVISION: Final[str] = "3cc26e7f1014a5ee5d74a42acee58bafc9d0a310"
YUNET_FILENAME: Final[str] = "face_detection_yunet_2023mar.onnx"
YUNET_URL: Final[str] = (
    "https://huggingface.co/opencv/face_detection_yunet/resolve/"
    f"{YUNET_REPO_REVISION}/{YUNET_FILENAME}"
    f"?revision={YUNET_REPO_REVISION}"
)

#: Independently verified expected content digest (see module docstring).
YUNET_SHA256: Final[str] = (
    "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
)

#: Independently verified exact size in bytes (same source as the digest).
YUNET_SIZE: Final[int] = 232_589

#: Slug-style names allowed to serve redirects and content. ``huggingface.co``
#: is the pinned model repository; the HF storage hosts (xet CDN bridges and
#: LFS CDN) are the official signed-URL destinations that ``resolve`` hands
#: out. Added deliberately and narrowly — anything else stays rejected.
REDIRECT_HOST_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "huggingface.co",
        "us.aws.cdn.hf.co",
        "cas-bridge.xethub.hf.co",
        "cas-server.xethub.hf.co",
        "cdn-lfs.huggingface.co",
        "cdn-lfs-us-1.huggingface.co",
        "cdn-lfs-us-1.hf.co",
        "cdn-lfs.hf.co",
        "cdn-lfs-eu-1.hf.co",
        "cdn-lfs-eu-1.huggingface.co",
        "transfer.xethub.hf.co",
        "eu.aws.cdn.hf.co",
        "ap.aws.cdn.hf.co",
    }
)

#: Default destination — exactly what ``FaceDetector`` (Step 05) expects.
DEFAULT_MODEL_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "models" / "yunet.onnx"
)

#: Slack factored into the size cap so HTML error pages and LFS pointers are
#: rejected by content checks (not just by length) while a hostile/redirected
#: server cannot run away with bandwidth.
MAX_DOWNLOAD_BYTES: Final[int] = 24 * YUNET_SIZE  # ~5.6 MiB, well above 232 KiB
MAX_REDIRECTS: Final[int] = 3
DOWNLOAD_TIMEOUT: Final[float] = 30.0  # seconds, connect + read
CHUNK_SIZE: Final[int] = 65_536

# Content sniffs that prove the payload is NOT the model.
_LFS_POINTER_MAGIC: Final[bytes] = b"version https://git-lfs"
_ONNX_MAGIC: Final[bytes] = b"onnx"  # ONNX protobuf must contain this marker

_EXIT_OK: Final[int] = 0
_EXIT_FAILURE: Final[int] = 1


# ---------------------------------------------------------------------------
# Error surface (actionable, single type for the CLI boundary)
# ---------------------------------------------------------------------------


class ModelDownloadError(RuntimeError):
    """Any download/validation failure meant to be reported to the operator."""


# ---------------------------------------------------------------------------
# Filesystem helpers (destination + unique temp file in the same directory)
# ---------------------------------------------------------------------------


def prepare_destination(destination: Path) -> Path:
    """Resolve the absolute destination and create its parent directory.

    Rules enforced here:

    * the destination must not be an existing **directory**;
    * if the destination already exists as a file the caller decides what
      to do (this function only reports the collision through its return
      contract below);
    * the parent directory is created only when this helper ran explicitly
      with a download intent (see ``main``) — never at import time.

    Returns:
        The absolute, resolved destination path. A pre-existing file is
        accepted here; overwrite policy is enforced by the caller.

    Raises:
        ModelDownloadError: destination is a directory, parent creation
            fails, or the destination is not writable.
    """
    dest = destination.expanduser()
    if not dest.is_absolute():
        dest = Path.cwd() / dest
    dest = dest.resolve()

    if dest.is_dir():
        raise ModelDownloadError(
            f"destination '{dest}' is a directory; expected a file path"
        )

    parent = dest.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ModelDownloadError(
                f"cannot create model directory '{parent}': {exc}"
            ) from exc
    if not parent.is_dir():
        raise ModelDownloadError(
            f"model directory '{parent}' is not a directory"
        )

    probe_writable(parent)
    return dest


def probe_writable(directory: Path) -> None:
    """Fail fast (with cleanup) when the directory is not writable."""
    if not os.access(directory, os.W_OK | os.X_OK):
        raise ModelDownloadError(
            f"directory '{directory}' is not writable; fix permissions "
            "or pass a different destination with --dest"
        )
    probe: Path | None = None
    try:
        handle = tempfile.NamedTemporaryFile(
            dir=directory, prefix=".probe-", suffix=".tmp", delete=False
        )
        probe = Path(handle.name)
        handle.close()
    except OSError as exc:
        raise ModelDownloadError(
            f"directory '{directory}' is not writable ('{exc}')"
        ) from exc
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass


def make_temp_file(destination: Path) -> IO[bytes]:
    """Open a uniquely named temp file next to the destination.

    Same directory keeps :func:`os.replace` atomic (same filesystem).
    """
    return tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.part-",
        suffix=".tmp",
        delete=False,
    )


def atomic_install(temp_path: Path, destination: Path) -> None:
    """Move a fully validated temp file onto the destination atomically."""
    os.replace(temp_path, destination)


def remove_quietly(path: Path | None) -> None:
    """Best-effort removal used for *our own* temp files on failure paths."""
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Contents checks
# ---------------------------------------------------------------------------


def verify_contents(buffer_path: Path) -> None:
    """Reject things that are clearly not the YuNet ONNX model.

    Runs after checksum verification in the normal flow; also callable on
    its own for validation-only operations.

    Raises:
        ModelDownloadError: empty file, Git LFS pointer, HTML error page,
            or an ONNX file missing its protobuf marker bytes.
    """
    size = buffer_path.stat().st_size
    if size == 0:
        raise ModelDownloadError(
            "downloaded file is empty; the upstream server returned nothing"
        )
    if buffer_path.suffix.lower() not in {".onnx", ".tmp"}:
        raise ModelDownloadError(f"unexpected file suffix: {buffer_path.suffix!r}")

    try:
        with buffer_path.open("rb") as handle:
            head = handle.read(512)
            tail = b""
            # The ONNX identifier lives near the start of the protobuf; the
            # model graph name may push it past the head window, so also
            # scan a tail window for it.
            if _ONNX_MAGIC not in head:
                handle.seek(max(0, size - 512))
                tail = handle.read(512)
    except OSError as exc:
        raise ModelDownloadError(f"cannot re-read downloaded file: {exc}") from exc

    if b"<html" in head[:128].lower() or b"<!doctype html" in head[:128].lower():
        raise ModelDownloadError(
            "downloaded content is an HTML page, not the model file; the "
            "upstream may have returned an error page (rate limit, auth)"
        )
    if head.startswith(_LFS_POINTER_MAGIC) or b"git-lfs" in head[:128]:
        raise ModelDownloadError(
            "downloaded content is a Git LFS pointer, not the model file; "
            "the source may not have been followed as a raw artifact"
        )

    if _ONNX_MAGIC not in head and _ONNX_MAGIC not in tail:
        raise ModelDownloadError(
            "downloaded file lacks the ONNX bytes signature; refusing to "
            "install something that is not an ONNX model"
        )


# ---------------------------------------------------------------------------
# Model-acceptance validation with the real project backend (no camera, no
# application logic — just FaceDetectorYN initialization)
# ---------------------------------------------------------------------------


def validate_model_file(model_path: Path, input_width: int = 320, input_height: int = 320) -> None:
    """Load the file with ``cv2.FaceDetectorYN`` to prove it is a usable model.

    Uses the same backend the production ``FaceDetector`` (Step 05) uses, and
    the mean-input values conventionally passed as 'no dynamic startup'.

    No camera is opened, no frames are captured, no application logic runs:
    this only initializes the DNN inference object from the model bytes and
    then releases it.

    Raises:
        ModelDownloadError: if OpenCV is unavailable (deviation from a
            declared project dependency is reported, never silently skipped)
            or the model cannot be initialized.
    """
    try:
        import cv2  # local import keeps the CLI import side-effect free
    except ImportError as exc:  # pragma: no cover - environment problem
        raise ModelDownloadError(
            "opencv-python is a project requirement but it is not "
            f"importable in this environment: {exc}"
        ) from exc

    try:
        detector = cv2.FaceDetectorYN_create(
            str(model_path),
            "",
            (input_width, input_height),
            0.6,  # same in-use threshold family as production config
        )
        detector.setInputSize((input_width, input_height))
        del detector  # explicit resource drop; nothing else to release
    except Exception as exc:  # noqa: BLE001 - wrap backend init failure
        raise ModelDownloadError(
            f"OpenCV could not initialize a face detector from the "
            f"downloaded file: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable urllib's automatic redirect following.

    Every ``Location`` must come back to this helper's manual loop, where
    each hop is validated against the exact-host allowlist before a new
    request is issued. Without this handler-class override, urllib would
    chase redirects on its own (to hosts we never vetted) before we get a
    look at the chain.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None  # signal: never auto-follow


def validate_redirect(url: str) -> bool:
    """Whether a redirect target is safe to follow.

    Policy (exact-match, never substring):

    * Scheme must be exactly ``https`` — no http, ftp, or exotic schemes.
    * Hostname must be an **exact** member of
      :data:`REDIRECT_HOST_ALLOWLIST` (case-insensitive, port stripped).
      ``evil-huggingface.co`` or ``not.huggingface.co.evil.example``-style
      labels are rejected; matching is on the full hostname only.
    * No credentials/embedded user information is accepted in the target.

    This keeps a malicious or misbehaving server from steering us to an
    arbitrary third-party host that would serve different bytes, while
    still permitting the legitimate signed-URL storage/CDN destinations
    Hugging Face hands out for the pinned artifact (e.g. the xet
    ``*-cdn.hf.co`` bridge for the model file itself).
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password or parsed.fragment:
        return False
    try:
        hostname = parsed.hostname
        port = parsed.port  # raises ValueError for non-numeric ports
    except ValueError:  # unparseable URL (malformed port etc.)
        return False
    if not hostname:
        return False
    if port is not None and port != 443:
        return False
    # netloc→hostname lowercases already; guard against IP-literal and
    # trailing-dot tricks by requiring an exact allowlist member.
    return hostname.rstrip(".") in REDIRECT_HOST_ALLOWLIST


def extract_response(response: object, buffer_path: Path, expected_sha256: str, expected_size: int | None) -> None:
    """Stream ``response`` into ``buffer_path`` with bounded, dry handling.

    Streaming is bounded by ``MAX_DOWNLOAD_BYTES``; partial/interrupted
    transfers, empty bodies, and oversized payloads all fail here instead
    of producing a truncated or silently accepted model file.
    """
    written = 0
    try:
        with buffer_path.open("wb") as out:
            while True:
                chunk = response.read(CHUNK_SIZE)  # type: ignore[attr-defined]
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise ModelDownloadError(
                        "download exceeded the maximum allowed size "
                        f"({MAX_DOWNLOAD_BYTES} bytes); aborting"
                    )
                out.write(chunk)
    except ModelDownloadError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if written == 0:
            raise ModelDownloadError(f"network failure before any data: {exc}") from exc
        raise ModelDownloadError(f"download interrupted after {written} bytes: {exc}") from exc

    if written == 0:
        raise ModelDownloadError("server returned an empty response body")

    if expected_size is not None and written != expected_size:
        raise ModelDownloadError(
            f"downloaded {written} bytes but expected {expected_size}; "
            "the transfer was likely interrupted or forged"
        )

    digest = hashlib.sha256(buffer_path.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise ModelDownloadError(
            f"SHA-256 mismatch: expected {expected_sha256}, got {digest}; "
            "refusing to install unverified bytes"
        )


def _build_request(url: str) -> urllib.request.Request:
    """A GET request carrying only non-sensitive standard headers.

    No credentials, cookies, or authorization headers are included: the
    pinned repository is public and forwarding credentials to a redirect
    destination would leak them to another host, which this helper must
    never do.
    """
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": "attendance-yunet-model-helper/1.0",
            "Accept": "application/octet-stream",
        },
        method="GET",
    )


def run_download_and_validate(url: str, temp_file: IO[bytes]) -> None:
    """HTTPS GET with bounded, hop-validated redirects into an open temp file.

    Every hop is followed *manually* (urllib's automatic redirect following
    is disabled) so each ``Location`` goes through
    :func:`validate_redirect` before a new request is made — including
    hops returned by earlier hops, not just the first response. The final
    response's effective URL is re-validated as defense in depth, and the
    downloaded bytes are ultimately trusted only after the pinned SHA-256
    comparison, so a compromised redirect chain cannot plant arbitrary
    content without being rejected.
    """
    buffer_path = Path(temp_file.name)
    current_url = url
    seen_urls: set[str] = {url}
    redirects_used = 0
    while True:
        # No automatic redirects: urllib must hand each hop back to us.
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            response = opener.open(_build_request(current_url), timeout=DOWNLOAD_TIMEOUT)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location", "") if exc.headers else ""
            if exc.code in {301, 302, 303, 307, 308} and location:
                if not validate_redirect(location):
                    raise ModelDownloadError(
                        f"upstream redirected to an unapproved destination "
                        f"('{location}'); refusing to follow"
                    ) from exc
                redirects_used += 1
                if redirects_used > MAX_REDIRECTS or current_url == location:
                    raise ModelDownloadError("too many redirects") from exc
                current_url = location
                if current_url in seen_urls:
                    raise ModelDownloadError("redirect loop detected")
                seen_urls.add(current_url)
                continue
            raise ModelDownloadError(f"HTTP error {exc.code} from upstream") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            raise ModelDownloadError(
                f"network error contacting upstream: {reason}"
            ) from exc
        except TimeoutError as exc:
            raise ModelDownloadError(
                f"network timeout after {DOWNLOAD_TIMEOUT:.0f}s: {exc}"
            ) from exc

        # Defense in depth: the effective final URL must also be approved
        # (it equals current_url with manual redirect handling; a tampered
        # or buggy stack must not slip an unapproved host by us).
        try:
            final_url = response.geturl() or current_url
        except Exception:  # noqa: BLE001 - final URL probe must not crash us
            final_url = current_url
        if final_url != current_url and not validate_redirect(final_url):
            response.close()
            raise ModelDownloadError(
                f"redirected through an unapproved destination "
                f"('{final_url}'); refusing"
            )
        try:
            expected_size_header = response.headers.get("X-Linked-Size")
            expected_size: int | None = (
                int(expected_size_header) if expected_size_header else None
            )
            extract_response(response, buffer_path, YUNET_SHA256, expected_size)
        finally:
            response.close()  # type: ignore[union-attr]
        return


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def confirm_replace_interactive(destination: Path) -> bool:
    """Require an unambiguous interactive confirmation for replacement."""
    prompt = (
        f"'{destination}' already exists. Replace it with a freshly "
        "downloaded, validated model? This changes the existing file.\n"
        "Type 'REPLACE' to authorize, anything else to abort: "
    )
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip() == "REPLACE"


def check_existing(destination: Path, *, replace: bool, yes: bool, interactive: bool) -> bool:
    """Return whether the operation may proceed when a model already exists.

    ``False`` means: report the situation and exit without modifying
    anything.
    """
    if not destination.exists():
        return True

    if not replace:
        raise ModelDownloadError(
            f"model file already exists at '{destination}'. "
            "Leave it alone, or run with --replace to explicitly "
            "authorize replacement (see docs/YUNET_MODEL.md for how to "
            "validate the existing model instead)."
        )

    if interactive:
        if not confirm_replace_interactive(destination):
            raise ModelDownloadError(
                "replacement canceled; the existing model was not modified"
            )
        return True

    if not yes:
        raise ModelDownloadError(
            "noninteractive --replace requested without --yes; refusing "
            "to modify the existing model without an explicit "
            "authorization option"
        )
    return True


def download_model(destination: Path, *, replace: bool, yes: bool, interactive: bool) -> None:
    """Download → verify → validate → atomically install the model."""
    dest = prepare_destination(destination)
    if not check_existing(dest, replace=replace, yes=yes, interactive=interactive):
        return

    temp_path: Path | None = None
    temp_file: IO[bytes] | None = None
    try:
        temp_file = make_temp_file(dest)
        temp_path = Path(temp_file.name)
        run_download_and_validate(YUNET_URL, temp_file)
        temp_file.close()  # fully flushed/closed before verification
        temp_file = None

        verify_contents(temp_path)
        validate_model_file(temp_path)
        atomic_install(temp_path, dest)
        temp_path = None  # installed; nothing left to clean up
        print(f"OK: model installed at {dest}")
    finally:
        if temp_file is not None:
            try:
                temp_file.close()
            except OSError:
                pass
        remove_quietly(temp_path)


def validate_existing(destination: Path) -> bool:
    """Validation-only operation for a model already on disk.

    Non-destructive: nothing is modified, moved, or deleted, whichever way
    this exits.
    """
    dest = Path(destination).expanduser()
    if not dest.is_absolute():
        dest = Path.cwd() / dest
    dest = dest.resolve()
    if not dest.is_file():
        raise ModelDownloadError(f"no model file at '{dest}' — nothing to validate")
    probe_writable(dest.parent)  # prove the environment is sane before trusting

    size = dest.stat().st_size
    if size != YUNET_SIZE:
        raise ModelDownloadError(
            f"size mismatch: file is {size} bytes, expected {YUNET_SIZE} "
            "— not the verified upstream artifact"
        )

    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != YUNET_SHA256:
        raise ModelDownloadError(
            f"SHA-256 mismatch: file does not match the independently "
            f"verified upstream digest ({YUNET_SHA256})"
        )

    verify_contents(dest)
    validate_model_file(dest)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="download_yunet_model.py",
        description=(
            "Download and install the official YuNet ONNX face-detection "
            "model used by the attendance system. This is a MANUAL helper: "
            "the application never downloads the model itself."
        ),
        epilog=(
            f"Source (pinned): {YUNET_URL}\n"
            f"Expected SHA-256: {YUNET_SHA256}\n"
            f"Expected size:    {YUNET_SIZE} bytes\n"
            "See docs/YUNET_MODEL.md for the full provenance record."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "destination file path (default: models/yunet.onnx resolved "
            "against the project root, independent of the current "
            "working directory)"
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "validate an existing model file (size, SHA-256, format, "
            "OpenCV loadability) without downloading anything"
        ),
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "authorize replacing an existing destination model AFTER "
            "the new download has been fully verified"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "skip the interactive confirmation for --replace "
            "(intended for noninteractive runs, e.g. CI)"
        ),
    )
    return parser


def _absent_default_path(destination: Path | None) -> Path:
    """Destination defaulting that ignores the current working directory.

    Reads the module global at call time so test harnesses (and any
    embedding consumer) can retarget the default via ``monkeypatch``.
    """
    if destination is not None:
        return destination
    return DEFAULT_MODEL_PATH  # module-level name lookup at call time


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    destination = _absent_default_path(args.dest)

    try:
        if args.validate_only:
            validate_existing(destination)
            print(f"OK: '{destination}' matches the verified upstream artifact")
        else:
            interactive = sys.stdin.isatty() if sys.stdin is not None else False
            download_model(
                destination,
                replace=args.replace,
                yes=args.yes,
                interactive=interactive,
            )
    except ModelDownloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_FAILURE
    return _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
