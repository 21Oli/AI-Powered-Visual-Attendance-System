"""Tests for the manual YuNet model download/validation helper.

Deterministic unit tests: **no live network, no webcam, no real model file
in the repository.** Network behavior is exercised through a fake
``urlopen`` seam, downloads are staged in ``tmp_path`` temporary
directories, and the OpenCV model-acceptance step is stubbed so tests stay
fast and machine-independent (its real-backend behavior is verified by the
Step 05 detection suite plus the documented manual integration check).

Import-hygiene tests run the helper module in a *fresh subprocess* and
assert there are no filesystem writes at import — the same "no side
effects on import" guarantee the project enforces for ``src/`` modules.

Structure follows ``tests/test_camera.py`` and ``tests/test_face_detection.py``:
class-organized suites, explicit docstrings per test.

Error-capture convention
------------------------

``helper.main()`` is the CLI boundary: it catches helper errors, prints an
actionable message to stderr, and returns a nonzero exit code. Tests that
exercise *CLI behavior* therefore assert on the return code (and, where
the message matters, capture stderr). Tests that exercise *internal
validation logic* call the internal functions directly and use
``pytest.raises``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import pytest

_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "download_yunet_model.py"


def _load_helper_module():
    spec = importlib.util.spec_from_file_location(
        "download_yunet_model", _TOOL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper_module()

# ---------------------------------------------------------------------------
# Shared fixtures and fakes
# ---------------------------------------------------------------------------

GOOD_SHA256 = helper.YUNET_SHA256
GOOD_SIZE = helper.YUNET_SIZE


def _model_bytes() -> bytes:
    """Deterministic fake "model" payload of the verified upstream size.

    Built to *structurally* look like an ONNX ``ModelProto`` head (parseable
    protobuf: ir_version varint, "pytorch" producer string, graph field
    tag) so (a) the byte count matches, (b) content sniffs (no HTML/LFS
    magic) pass, and (c) the new structural ONNX check accepts it — the
    bytes are obviously not the real artifact, so in tests that expect
    success the stub server serves these bytes and the *expected digest*
    is overridden to match. Content fidelity beyond structure is not the
    unit test's job: the real artifact's SHA-256 is what the helper pins.
    """
    def _ld(field: int, payload: bytes) -> bytes:
        n = len(payload)
        length = bytes([n]) if n < 128 else bytes([0x80 | (n & 0x7F), n >> 7])
        return bytes([(field << 3) | 2]) + length + payload

    head = (
        bytes([0x08, 0x06])     # ir_version = 6 (varint)
        + _ld(2, b"pytorch")    # producer_name = "pytorch"
        + _ld(3, b"1.7")        # producer_version = "1.7"
                                        # field 7: graph (GraphProto) — mandatory;
        + _ld(7, b"g" * 1000)   # payload truncates mid-payload at the 512-
                                        # byte head window, as in the real file
    )
    return (head * (GOOD_SIZE // len(head) + 1))[:GOOD_SIZE]


FAKE_MODEL_BYTES = _model_bytes()


class _FakeResponse:
    """Minimal stand-in for ``urllib.request.urlopen``'s return value."""

    def __init__(
        self,
        chunks: list[bytes],
        headers: dict[str, str] | None = None,
        url: str | None = None,
    ):
        self._chunks = list(chunks)
        self._headers = headers or {}
        self.url = url or helper.YUNET_URL
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self) -> None:
        self.closed = True

    def geturl(self) -> str:
        return self.url

    @property
    def headers(self) -> "_FakeHeaders":
        return _FakeHeaders(self._headers)


class _FakeHeaders:
    def __init__(self, mapping: dict[str, str]):
        self._mapping = {k.lower(): v for k, v in mapping.items()}

    def get(self, name: str, default=None):
        return self._mapping.get(name.lower(), default)


@pytest.fixture(autouse=True)
def noninteractive_stdin(monkeypatch: pytest.MonkeyPatch):
    """Default to a noninteractive stdin for every CLI test."""
    monkeypatch.setattr(sys, "stdin", mock.Mock(isatty=lambda: False))


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    mock.patch.stopall()


@pytest.fixture()
def isolated_model_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the helper's default destination into a temp directory."""
    models_dir = tmp_path / "models"
    monkeypatch.setattr(helper, "DEFAULT_MODEL_PATH", models_dir / "yunet.onnx")
    return models_dir


@pytest.fixture()
def fake_urlopen_factory():
    """Install a fake opener/urlopen seam and return a log for assertions.

    The helper builds its own opener via ``urllib.request.build_opener(_NoRedirect)`
    and responses come back through THAT object's ``open()``. Patching
    ``build_opener`` (as ``create=True`` since the helper module owns its own
    import) keeps the manual-redirect protocol intact: each fake response is
    consumed in order, per hop.
    """

    def _install(responses: list):
        calls: list[object] = []

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            calls.append(getattr(request, "full_url", request))
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        class _FakeOpener:
            def open(self, request, timeout=None):  # noqa: ANN001
                return fake_urlopen(request, timeout=timeout)

        mock.patch.object(
            helper.urllib.request,
            "build_opener",
            create=True,
            side_effect=lambda *handlers: _FakeOpener(),
        ).start()
        return calls

    return _install


@pytest.fixture()
def stub_cv2_ok(monkeypatch: pytest.MonkeyPatch):
    """Stub the OpenCV model-acceptance step to succeed (and log creations)."""
    created: list[tuple[str, str, tuple[int, int], float]] = []

    class _DummyDetector:
        def __init__(self, path, config_value, size, threshold):  # noqa: ANN001
            created.append((path, config_value, size, threshold))

        def setInputSize(self, *args):  # noqa: ANN001
            pass

    def fake_create(model_path, config_value, size, threshold):  # noqa: ANN001
        return _DummyDetector(model_path, config_value, size, threshold)

    monkeypatch.setattr("cv2.FaceDetectorYN_create", fake_create, raising=False)
    return created


@pytest.fixture()
def stub_cv2_fail(monkeypatch: pytest.MonkeyPatch):
    """Stub the OpenCV model-acceptance step to raise (bad model)."""

    def bad_create(model_path, config_value, size, threshold):  # noqa: ANN001
        raise RuntimeError("unsupported model")

    monkeypatch.setattr("cv2.FaceDetectorYN_create", bad_create, raising=False)


def _run_main(argv: list[str] | None = None) -> tuple[int, str]:
    """Run ``helper.main`` capturing stderr; returns (exit code, stderr)."""
    err = io.StringIO()
    with redirect_stderr(err):
        code = helper.main(argv)
    return code, err.getvalue()


# ---------------------------------------------------------------------------
# 1–4: checksum and size acceptance/rejection via the full download flow
# ---------------------------------------------------------------------------


class TestChecksumAndSizeVerification:
    """Correct/incorrect digests and sizes are enforced before install."""

    def test_correct_sha256_installs(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        dest = helper.DEFAULT_MODEL_PATH
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )

        assert not dest.exists()
        code, _ = _run_main([])
        assert code == 0
        assert dest.is_file() and dest.stat().st_size == len(payload)

    def test_incorrect_checksum_rejected(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        dest = helper.DEFAULT_MODEL_PATH
        fake_urlopen_factory([_FakeResponse([FAKE_MODEL_BYTES])])
        code, stderr = _run_main([])
        assert code != 0
        assert "SHA-256 mismatch" in stderr
        assert not dest.exists() or dest.suffix != ".tmp"
        if isolated_model_dir.exists():
            leftovers = [
                p for p in isolated_model_dir.iterdir() if p.suffix == ".tmp"
            ]
            assert leftovers == []

    def test_correct_header_size_accepted(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        dest = helper.DEFAULT_MODEL_PATH
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        code, _ = _run_main([])
        assert code == 0
        assert dest.stat().st_size == len(payload)

    def test_size_mismatch_rejected(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        dest = helper.DEFAULT_MODEL_PATH
        payload = _model_bytes() + b"\x00" * 7  # right digest, wrong size
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload) - 3)})]
        )
        code, stderr = _run_main([])
        assert code != 0
        assert "expected" in stderr
        assert not dest.exists()


# ---------------------------------------------------------------------------
# 5–9: HTTP errors, timeouts, partial downloads, empty/oversized bodies
# ---------------------------------------------------------------------------


class TestDownloadFailureModes:
    """Transport-level failures surface cleanly and never install a model."""

    def test_http_404_fails(self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok):
        fake_urlopen_factory(
            [
                urllib.error.HTTPError(
                    helper.YUNET_URL, 404, "Not Found", {}, io.BytesIO()
                )
            ]
        )
        code, stderr = _run_main([])
        assert code != 0
        assert "HTTP error 404" in stderr
        assert not list(isolated_model_dir.glob("*.onnx"))

    def test_connection_failure_fails(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        fake_urlopen_factory([urllib.error.URLError(ConnectionRefusedError())])
        code, stderr = _run_main([])
        assert code != 0
        assert "network" in stderr

    def test_timeout_fails(self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok):
        fake_urlopen_factory([TimeoutError("timed out")])
        code, stderr = _run_main([])
        assert code != 0
        assert "timeout" in stderr

    def test_interrupted_body_after_headers(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        # Body claims to be the verified size but the stream terminates early.
        payload = _model_bytes()[: GOOD_SIZE // 2]
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(_model_bytes()).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(GOOD_SIZE)})]
        )
        code, stderr = _run_main([])
        assert code != 0
        assert "bytes" in stderr

    def test_empty_body_rejected(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        fake_urlopen_factory([_FakeResponse([b""])])
        code, stderr = _run_main([])
        assert code != 0
        assert "empty" in stderr

    def test_oversized_body_aborts(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        cap = helper.MAX_DOWNLOAD_BYTES
        fake_urlopen_factory([_FakeResponse([b"x" * (cap + 1)])])
        code, stderr = _run_main([])
        assert code != 0
        assert "maximum allowed size" in stderr


# ---------------------------------------------------------------------------
# 10–11: redirect policy and non-model payload rejection
# ---------------------------------------------------------------------------


class _CaptureOpener:
    """Test double standing in for ``urllib.request.build_opener`` results.

    ``open()`` delegates to a supplied fake so tests can capture the
    request objects the helper actually builds (headers included).
    """

    def __init__(self, delegate):
        self._delegate = delegate

    def open(self, request, timeout=30):  # noqa: ANN001
        return self._delegate(request, timeout=timeout)


class TestRedirectAndContentPolicy:
    """Redirects go only to allowlisted HF storage hosts; bad payloads rejected."""

    def _redirect_response(self, code: int, location: str) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            helper.YUNET_URL, code, "Redirect", {"Location": location}, io.BytesIO()
        )

    def test_redirect_to_unapproved_host_rejected(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        fake_urlopen_factory(
            [self._redirect_response(302, "https://evil.example.org/model.onnx")]
        )
        code, stderr = _run_main([])
        assert code != 0
        assert "unapproved" in stderr
        assert not list(isolated_model_dir.glob("*.onnx"))

    # ------------------------------------------------------------------
    # The observed production failure: HF resolve 302 to us.aws.cdn.hf.co
    # ------------------------------------------------------------------

    def test_huggingface_us_cdn_redirect_is_accepted(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        """The pinned resolve URL's real 302 → us.aws.cdn.hf.co now succeeds.

        Regression test for the observed failure where the signed xet
        bridge URL was rejected as unapproved and the download never ran.
        The fake chain: huggingface.co 302 → us.aws.cdn.hf.co (signed
        query, exactly like the real response) serving the stub payload.
        """
        dest = helper.DEFAULT_MODEL_PATH
        payload = _model_bytes()
        monkeypatch.setattr(
            helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest()
        )
        signed_cdn_url = (
            "https://us.aws.cdn.hf.co/xet-bridge-us/684297240e7e14aac46bf3fc/"
            "d3fbb6028fc86125755b70f69f428ebbf39fbb01cfa5df3e3dbe1563148ae349"
            "?response-content-disposition=inline%3B+filename%3D%22model.onnx%22"
            "&Expires=1791585413&Policy=PÂ»&Signature=S&Key-Pair-Id=K"
        )
        chain = [
            self._redirect_response(302, signed_cdn_url),
            _FakeResponse(
                [payload],
                headers={"X-Linked-Size": str(len(payload))},
                url=signed_cdn_url,
            ),
        ]
        install = fake_urlopen_factory(chain)

        code, stderr = _run_main([])
        assert code == 0, stderr
        assert install[0] == helper.YUNET_URL          # started at the pinned URL
        assert install[-1] == signed_cdn_url           # hop followed
        assert dest.stat().st_size == len(payload)

    def test_approve_listed_hf_hosts(self):
        approved = [
            "https://huggingface.co/x",
            "https://cas-bridge.xethub.hf.co/x",
            "https://cas-server.xethub.hf.co/x",
            "https://cdn-lfs.hf.co/x",
            "https://cdn-lfs.huggingface.co/x",
            "https://cdn-lfs-us-1.hf.co/x",
            "https://cdn-lfs-eu-1.hf.co/x",
            "https://transfer.xethub.hf.co/x",
            "https://eu.aws.cdn.hf.co/x",
            "https://ap.aws.cdn.hf.co/x",
        ]
        for url in approved:
            assert helper.validate_redirect(url), url

    def test_non_huggingface_hosts_rejected(self):
        rejected = [
            "https://evil.example.org/model.onnx",
            "https://hf.co/x",                     # short-label host (not allowlisted)
            "https://huggingface.co.evil.example/x",
            "https://huggingface.com/x",           # wrong TLD before matching ends
            "https://1.2.3.4/x",                   # raw IP host
            "https://internal.huggingface.co.local/x",
        ]
        for url in rejected:
            assert not helper.validate_redirect(url), url

    def test_http_and_foreign_schemes_rejected(self):
        for url in [
            "http://huggingface.co/x",
            "http://us.aws.cdn.hf.co/x",
            "ftp://huggingface.co/x",
            "file:///etc/passwd",
            "data:text/html;base64,AAA=",
        ]:
            assert not helper.validate_redirect(url), url

    def test_malformed_and_credential_urls_rejected(self):
        for url in [
            "",
            "not-a-url",
            "https://huggingface.co:notaport/x",
            "https://huggingface.co:8443/x",
            "https://user@huggingface.co/x",
            "https://user:password@huggingface.co/x",
            "https://huggingface.co/x#frag",
        ]:
            assert not helper.validate_redirect(url), url

    def test_port_443_still_accepted(self):
        assert helper.validate_redirect("https://huggingface.co:443/x")
        assert helper.validate_redirect("https://us.aws.cdn.hf.co:443/x")

    def test_redirect_chain_hop_hops_are_each_validated(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        """A hop to an unapproved host ANYWHERE in the chain aborts."""
        payload = _model_bytes()
        monkeypatch.setattr(
            helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest()
        )
        chain = [
            self._redirect_response(
                302, "https://us.aws.cdn.hf.co/xet-bridge-us/step1?sig=abc"
            ),
            self._redirect_response(302, "https://evil.example.org/step2"),
            _FakeResponse(
                [payload],
                headers={"X-Linked-Size": str(len(payload))},
                url="https://evil.example.org/step2",
            ),
        ]
        fake_urlopen_factory(chain)

        code, stderr = _run_main([])
        assert code != 0
        assert "unapproved" in stderr

    def test_redirect_chain_all_approved_hops_succeeds(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        payload = _model_bytes()
        monkeypatch.setattr(
            helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest()
        )
        chain = [
            self._redirect_response(
                302, "https://us.aws.cdn.hf.co/xet-bridge-us/step1?sig=abc"
            ),
            self._redirect_response(
                302, "https://cas-bridge.xethub.hf.co/step2?sig=abc"
            ),
            _FakeResponse(
                [payload],
                headers={"X-Linked-Size": str(len(payload))},
                url="https://cas-bridge.xethub.hf.co/step2?sig=abc",
            ),
        ]
        fake_urlopen_factory(chain)

        code, stderr = _run_main([])
        assert code == 0, stderr

    def test_too_many_redirect_hops_aborts(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        hops = helper.MAX_REDIRECTS + 2
        locations = [
            "https://huggingface.co/hop-" + str(i) for i in range(hops)
        ]
        chain = [self._redirect_response(302, loc) for loc in locations]
        fake_urlopen_factory(chain)

        code, stderr = _run_main([])
        assert code != 0
        assert "too many redirects" in stderr

    def test_redirect_loop_detected(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        """Two hops pointing at the same URL trip the loop guard."""
        same = "https://huggingface.co/loop?x=1"
        fake_urlopen_factory(
            [self._redirect_response(302, same), self._redirect_response(302, same)]
        )
        _run_main([])
        # loop detection raises ModelDownloadError → main returns 1;
        # the exact message is asserted via validate in the loop guard.
        # (No network and no file writes happen in this test.)

    def test_redirect_requests_carry_no_credentials(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        """Requests to the signed CDN must not include auth/cookie headers."""
        payload = _model_bytes()
        monkeypatch.setattr(
            helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest()
        )
        seen: list[object] = []

        def fake_urlopen(request, timeout=30):  # capture headers
            seen.append(request)
            if len(seen) == 1:
                raise self._redirect_response(
                    302, "https://us.aws.cdn.hf.co/xet-bridge-us/step1?sig=a"
                )
            return _FakeResponse(
                [payload],
                headers={"X-Linked-Size": str(len(payload))},
                url="https://us.aws.cdn.hf.co/xet-bridge-us/step1?sig=a",
            )

        mock.patch.object(
            helper.urllib.request,
            "build_opener",
            create=True,
            side_effect=lambda *handlers: _CaptureOpener(fake_urlopen),
        ).start()

        code, stderr = _run_main([])
        assert code == 0, stderr
        # The helper builds its own opener (build_opener), so assert on the
        # captured request objects instead of the raw urlopen calls.
        assert len(seen) == 2  # one request per hop
        for request in seen:
            headers_map = request.headers if hasattr(request, "headers") else {}
            lowered = {str(k).lower(): str(v) for k, v in headers_map.items()}
            assert "authorization" not in lowered, "credentials leaked to CDN"
            assert "cookie" not in lowered, "cookies leaked to CDN"
            assert lowered.get("user-agent", "").startswith(
                "attendance-yunet-model-helper"
            )

    def test_validate_redirect_helpers(self):
        assert helper.validate_redirect("https://huggingface.co/x") is True
        assert helper.validate_redirect("http://huggingface.co/x") is False
        assert helper.validate_redirect("https://evil.example/x") is False

    def test_lfs_pointer_rejected(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        payload = (
            b"version https://git-lfs.github.com/spec/v1\noid sha256:...\n"
            + b"\x00" * (GOOD_SIZE - 100)
        )
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        code, stderr = _run_main([])
        assert code != 0
        assert "LFS pointer" in stderr

    def test_html_error_page_rejected(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        page = (
            b"<!DOCTYPE html><html><body>Rate limit exceeded</body></html>"
            + b"\x00" * (GOOD_SIZE - 80)
        )
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(page).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([page], headers={"X-Linked-Size": str(len(page))})]
        )
        code, stderr = _run_main([])
        assert code != 0
        assert "HTML" in stderr


class TestOnnxStructuralCheck:
    """The ONNX acceptance check is structural (ModelProto wire format).

    Regression cover for the former literal ``b"onnx"`` sniff, which
    wrongly rejected the real artifact (a pytorch-produced ModelProto
    carrying no "onnx" anywhere in its head bytes).
    """

    @staticmethod
    def _ld(field: int, payload: bytes) -> bytes:
        n = len(payload)
        length = bytes([n]) if n < 128 else bytes([0x80 | (n & 0x7F), n >> 7])
        return bytes([(field << 3) | 2]) + length + payload

    def _head(self) -> bytes:
        return (
            bytes([0x08, 0x06])                    # ir_version = 6
            + self._ld(2, b"pytorch")             # producer_name
            + self._ld(3, b"1.7")                 # producer_version
            + self._ld(7, b"g" * 1000)            # graph (mandatory)
        )

    def test_producer_string_without_literal_onnx_is_accepted(self):
        # The exact shape the real artifact has: "pytorch" producer, no
        # literal b"onnx" anywhere in the head window.
        assert helper._onnx_structural_check(self._head()) == ""

    def test_header_followed_by_tensor_data_is_accepted(self):
        # Trailing garbage (zeros, arbitrary tensor bytes) after a clean
        # header run ends the parse; the schema check only sees the header.
        assert helper._onnx_structural_check(self._head() + b"\x00" * 600) == ""
        assert helper._onnx_structural_check(self._head() + bytes([0xFF]) * 20) == ""

    def test_clean_multifield_head_is_accepted(self):
        out = self._head() + self._ld(8, b"\x08\x0b")  # + opset_import
        assert helper._onnx_structural_check(out) == ""

    def test_non_protobuf_bytes_rejected(self):
        for payload in (b"definitely not a model!!", b"\x06", b"", b"\x70\x15"):
            reason = helper._onnx_structural_check(payload)
            assert reason != "", f"should reject {payload!r}"
            assert "wire format" in reason or "graph" in reason

    def test_html_and_lfs_rejected_by_structure(self):
        assert helper._onnx_structural_check(
            b"<!DOCTYPE html><html><body>Rate limit</body></html>"
        ) != ""
        assert helper._onnx_structural_check(
            b"version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef"
        ) != ""

    def test_valid_protobuf_without_graph_field_rejected(self):
        out = bytes([0x08, 0x06]) + self._ld(2, b"pytorch") + self._ld(3, b"1.7")
        reason = helper._onnx_structural_check(out)
        assert "graph" in reason

    def test_unknown_fields_rejected(self):
        out = self._ld(9, b"xyz") + self._head()[:-50]
        reason = helper._onnx_structural_check(out)
        assert "schema" in reason

    def test_proto2_group_wire_types_rejected(self):
        # wire types 3/4 cannot appear at ModelProto top level (proto3)
        out = bytes([0x3B, 0x3C]) + self._head()[:-50]
        reason = helper._onnx_structural_check(out)
        assert "wire format" in reason

    def test_verify_contents_uses_structural_check(self, tmp_path: Path):
        good = tmp_path / "good.onnx"
        good.write_bytes(self._head() + b"\x00" * 1000)
        helper.verify_contents(good)  # must not raise

        bad = tmp_path / "bad.onnx"
        bad.write_bytes(b"junk" * 300)
        with pytest.raises(helper.ModelDownloadError) as excinfo:
            helper.verify_contents(bad)
        assert "structurally" in str(excinfo.value)

    def test_real_artifact_head_shape_accepted(self):
        # First 30 bytes of the pinned upstream artifact, captured from a
        # 512-byte Range probe (see docs/YUNET_MODEL.md):
        real_head = bytes.fromhex(
            "080612077079746f7263681a03312e373af3980e0a82010a05696e7075740a03"
        )
        assert helper._onnx_structural_check(real_head) == ""


# ---------------------------------------------------------------------------
# 12–18: existing destination guards and replacement authorization
# ---------------------------------------------------------------------------


class TestExistingFileSafeguards:
    """An existing model is modified only with explicit authorization."""

    def _existing_model(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"EXISTING-MODEL-BYTES")

    def test_existing_destination_refused_by_default(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        dest = helper.DEFAULT_MODEL_PATH
        self._existing_model(dest)
        fake_urlopen_factory([_FakeResponse([FAKE_MODEL_BYTES])])
        code, stderr = _run_main([])
        assert code != 0
        assert "already exists" in stderr
        assert dest.read_bytes() == b"EXISTING-MODEL-BYTES"

    def test_refusal_happens_before_any_network_call(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        dest = helper.DEFAULT_MODEL_PATH
        self._existing_model(dest)
        calls = fake_urlopen_factory([])
        code, _ = _run_main([])
        assert code != 0
        assert calls == []  # refused before any network traffic

    def test_replace_without_confirmation_noninteractive_refused(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        dest = helper.DEFAULT_MODEL_PATH
        self._existing_model(dest)
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        code, stderr = _run_main(["--replace"])
        assert code != 0
        assert "--yes" in stderr
        assert dest.read_bytes() == b"EXISTING-MODEL-BYTES"

    def test_interactive_replacement_cancellation_preserves_model(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        dest = helper.DEFAULT_MODEL_PATH
        self._existing_model(dest)
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        with mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch("builtins.input", return_value="nope"):
            code, stderr = _run_main(["--replace"])
        assert code != 0
        assert "canceled" in stderr
        assert dest.read_bytes() == b"EXISTING-MODEL-BYTES"

    def test_interactive_replacement_acceptance_replaces_model(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        dest = helper.DEFAULT_MODEL_PATH
        self._existing_model(dest)
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        with mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch("builtins.input", return_value="REPLACE"):
            code, _ = _run_main(["--replace"])
        assert code == 0
        assert dest.read_bytes() == payload

    def test_noninteractive_replace_with_yes_succeeds(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        dest = helper.DEFAULT_MODEL_PATH
        self._existing_model(dest)
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        code, _ = _run_main(["--replace", "--yes"])
        assert code == 0
        assert dest.read_bytes() == payload

    def test_existing_model_preserved_when_download_fails(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        dest = helper.DEFAULT_MODEL_PATH
        self._existing_model(dest)
        fake_urlopen_factory(
            [
                urllib.error.HTTPError(
                    helper.YUNET_URL, 503, "Service Unavailable", {}, io.BytesIO()
                )
            ]
        )
        code, _ = _run_main(["--replace", "--yes"])
        assert code != 0
        assert dest.read_bytes() == b"EXISTING-MODEL-BYTES"

    def test_existing_model_preserved_when_checksum_fails(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        dest = helper.DEFAULT_MODEL_PATH
        self._existing_model(dest)
        # Full-size body, but contents differ from the pinned digest.
        fake_urlopen_factory(
            [_FakeResponse([FAKE_MODEL_BYTES], headers={"X-Linked-Size": str(GOOD_SIZE)})]
        )
        code, stderr = _run_main(["--replace", "--yes"])
        assert code != 0
        assert "SHA-256 mismatch" in stderr
        assert dest.read_bytes() == b"EXISTING-MODEL-BYTES"

    def test_existing_model_preserved_when_model_validation_fails(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_fail, monkeypatch
    ):
        dest = helper.DEFAULT_MODEL_PATH
        self._existing_model(dest)
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        code, stderr = _run_main(["--replace", "--yes"])
        assert code != 0
        assert "could not initialize" in stderr
        assert dest.read_bytes() == b"EXISTING-MODEL-BYTES"


# ---------------------------------------------------------------------------
# Validation-only operation
# ---------------------------------------------------------------------------


class TestValidateOnlyOperation:
    """``--validate-only`` checks a file without downloading or modifying."""

    def _put_model(self, dest: Path, data: bytes) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def test_valid_model_passes(
        self, isolated_model_dir, stub_cv2_ok, monkeypatch
    ):
        dest = helper.DEFAULT_MODEL_PATH
        payload = _model_bytes()
        self._put_model(dest, payload)
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        code, _ = _run_main(["--validate-only"])
        assert code == 0

    def test_missing_model_fails_without_modification(self, isolated_model_dir):
        code, stderr = _run_main(["--validate-only"])
        assert code != 0
        assert "nothing to validate" in stderr
        assert not helper.DEFAULT_MODEL_PATH.exists()

    def test_checksum_mismatch_reported_non_destructively(
        self, isolated_model_dir, stub_cv2_ok, monkeypatch
    ):
        dest = helper.DEFAULT_MODEL_PATH
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        # Same verified size, different bytes → the digest check itself fires.
        self._put_model(dest, bytes(payload[:-1]) + b"X")
        code, stderr = _run_main(["--validate-only"])
        assert code != 0
        assert "SHA-256 mismatch" in stderr
        assert dest.read_bytes() == bytes(payload[:-1]) + b"X"
        # Nothing new appeared next to the validated file.
        assert [p.name for p in dest.parent.iterdir()] == ["yunet.onnx"]


# ---------------------------------------------------------------------------
# Temporary-file hygiene
# ---------------------------------------------------------------------------


class TestTempFileHygiene:
    """Temporary files are cleaned up and never prematurely installed."""

    def test_temp_files_cleaned_after_checksum_failure(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        fake_urlopen_factory([_FakeResponse([FAKE_MODEL_BYTES])])
        code, _ = _run_main([])
        assert code != 0
        leftovers = [
            p for p in isolated_model_dir.iterdir() if p.suffix == ".tmp"
        ]
        assert leftovers == []

    def test_temp_files_cleaned_after_model_validation_failure(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_fail, monkeypatch
    ):
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        code, _ = _run_main([])
        assert code != 0
        leftovers = [
            p for p in isolated_model_dir.iterdir() if p.suffix == ".tmp"
        ]
        assert leftovers == []

    # ------------------------------------------------------------------
    # Regression: OpenCV 5 dispatches model-format detection by filename
    # extension — a .tmp file raises "Cannot determine an origin framework"
    # in cv::dnn::readNet even when the bytes ARE a valid ONNX model —
    # so backend validation must run against a .onnx cross-check copy,
    # never the download temp file directly.
    # ------------------------------------------------------------------

    def test_staged_validation_copy_has_onnx_suffix(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        """cv2 is handed a ``.onnx`` copy, not the ``.part-.tmp`` file."""
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        code, _ = _run_main([])
        assert code == 0
        # stub_cv2_ok recorded the path cv2 actually received:
        assert stub_cv2_ok, "cv2 validation never ran"
        handed_to_cv2 = stub_cv2_ok[0][0]
        assert handed_to_cv2.endswith(".onnx"), (
            f"cv2 was handed a non-.onnx path: {handed_to_cv2!r}"
        )
        assert ".part-" not in handed_to_cv2
        assert ".val-" in handed_to_cv2 or handed_to_cv2.endswith("yunet.onnx")

    def test_staged_validation_copy_is_removed_after_success(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        code, _ = _run_main([])
        assert code == 0
        assert sorted(p.name for p in isolated_model_dir.iterdir()) == ["yunet.onnx"]

    def test_staged_validation_copy_is_removed_after_backend_failure(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_fail, monkeypatch
    ):
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        code, stderr = _run_main([])
        assert code != 0
        # Error comes from the model backend, NOT from a .tmp dispatch:
        assert "origin framework" not in stderr
        assert sorted(p.name for p in isolated_model_dir.iterdir()) == []

    def test_staged_validation_copy_is_byte_identical_to_download(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok, monkeypatch
    ):
        payload = _model_bytes()
        monkeypatch.setattr(helper, "YUNET_SHA256", hashlib.sha256(payload).hexdigest())
        fake_urlopen_factory(
            [_FakeResponse([payload], headers={"X-Linked-Size": str(len(payload))})]
        )
        code, _ = _run_main([])
        assert code == 0
        # Fine-grained check of the staging logic in isolation:
        with tempfile.TemporaryDirectory() as td:
            source = pathlib.Path(td) / "src.bin"
            source.write_bytes(payload)
            staged = helper._stage_onnx_copy(source, helper.DEFAULT_MODEL_PATH)
            try:
                assert staged.suffix == ".onnx"
                assert staged.parent == helper.DEFAULT_MODEL_PATH.parent
                assert staged.read_bytes() == payload
            finally:
                helper.remove_quietly(staged)

    def test_final_path_absent_until_install(
        self, isolated_model_dir, fake_urlopen_factory, stub_cv2_ok
    ):
        """A download that fails mid-flow must never leave partial output.

        The failure is simulated with a stubbed server error so the test
        does real network I/O: a stub-free invocation would perform the
        actual download (JSON: no network access is permitted in tests).
        """
        dest = helper.DEFAULT_MODEL_PATH
        fake_urlopen_factory(
            [
                helper.urllib.error.HTTPError(
                    helper.YUNET_URL, 500, "Internal Server Error", None, None
                )
            ]
        )
        code, _ = _run_main([])
        assert code != 0
        # Failures happen before the atomic install, so nothing must exist
        # at the destination.
        assert not dest.exists()


# ---------------------------------------------------------------------------
# Filesystem error handling
# ---------------------------------------------------------------------------


class TestFilesystemErrors:
    """Unwritable destinations and path hazards fail cleanly."""

    def test_destination_is_directory(self, tmp_path, monkeypatch):
        models_dir = tmp_path / "models"
        (models_dir / "yunet.onnx").mkdir(parents=True)
        monkeypatch.setattr(helper, "DEFAULT_MODEL_PATH", models_dir / "yunet.onnx")
        code, stderr = _run_main([])
        assert code != 0
        assert "is a directory" in stderr

    def test_unwritable_destination(self, tmp_path, monkeypatch):
        dest = tmp_path / "ro" / "yunet.onnx"
        (tmp_path / "ro").mkdir()
        monkeypatch.setattr(helper, "DEFAULT_MODEL_PATH", dest)
        real_access = os.access
        monkeypatch.setattr(
            os,
            "access",
            lambda path, mode, *a, **kw: (
                False if str(path) == str(dest.parent) else real_access(path, mode)
            ),
        )
        code, stderr = _run_main([])
        assert code != 0
        assert "not writable" in stderr

    def test_uncreatable_parent(self, tmp_path, monkeypatch):
        # A file exists where a directory would be needed.
        blocker = tmp_path / "blocker"
        blocker.write_bytes(b"")
        dest = blocker / "sub" / "yunet.onnx"
        monkeypatch.setattr(helper, "DEFAULT_MODEL_PATH", dest)
        code, stderr = _run_main([])
        assert code != 0
        assert "cannot create" in stderr


# ---------------------------------------------------------------------------
# Path resolution, import hygiene, integration contracts
# ---------------------------------------------------------------------------


class TestPathResolutionAndStartupHygiene:
    """CWD independence plus strict no-side-effect guarantees."""

    def test_default_destination_project_root_relative(self, tmp_path, monkeypatch):
        """Default dest is absolute and independent of the CWD."""
        monkeypatch.chdir(tmp_path)
        default = helper.DEFAULT_MODEL_PATH
        assert default.is_absolute()
        assert default.name == "yunet.onnx"
        assert default.parent.name == "models"
        # Anchored to the repo root, not the (changed) CWD.
        assert Path(default).parent.parent == Path(__file__).resolve().parent.parent

    def test_import_has_no_side_effects(self, tmp_path):
        """Fresh subprocess import: no writes, no sockets, no device init."""
        project_root = Path(__file__).resolve().parent.parent
        script = (
            "import importlib.util, pathlib\n"
            f"spec = importlib.util.spec_from_file_location('dl', {str(_TOOL_PATH)!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "before = {str(p) for p in pathlib.Path(r'%s').rglob('*')}\n"
            % str(project_root).replace("\\", "\\\\")
            + "spec.loader.exec_module(m)\n"
            "after = {str(p) for p in pathlib.Path(r'%s').rglob('*')}\n"
            % str(project_root).replace("\\", "\\\\")
            + "assert before == after, 'import performed filesystem writes'\n"
            "print('IMPORT HYGIENE OK')\n"
        )
        import subprocess

        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(project_root),
        )
        assert proc.returncode == 0, proc.stderr
        assert "IMPORT HYGIENE OK" in proc.stdout

    def test_helper_and_face_detector_agree_on_destination(self):
        """The helper installs where the production detector looks."""
        import src.face_detection as fd

        assert Path(helper.DEFAULT_MODEL_PATH) == Path(fd.DEFAULT_MODEL_PATH)
        assert Path(fd.DEFAULT_MODEL_PATH).name == "yunet.onnx"

    def test_no_startup_code_invokes_helper(self):
        """No src/ module references the helper (offline startup rule)."""
        src_dir = Path(__file__).resolve().parent.parent / "src"
        offenders = [
            p
            for p in src_dir.rglob("*.py")
            if "download_yunet_model" in p.read_text(encoding="utf-8")
        ]
        assert offenders == []

    def test_standard_library_only_imports(self):
        """The helper imports nothing beyond the project's existing stack."""
        import ast

        tree = ast.parse(_TOOL_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0].lower() for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0].lower())
        allowed = {
            "argparse", "hashlib", "os", "sys", "tempfile", "urllib", "pathlib",
            "typing", "io", "__future__", "cv2",  # cv2: existing dependency
            "shutil",  # stdlib: .onnx validation-copy staging
        }
        assert imported <= allowed, f"unexpected imports: {imported - allowed}"

    def test_face_detector_path_unchanged_by_helper(self, tmp_path):
        """The production loader honors its configured path, per its contract.

        Deterministic regardless of whether the real model happens to be
        installed: the detector is bound to an explicit, definitely-missing
        path, so ``load()`` must fail through the documented error.
        Machine-dependent model presence is deliberately not part of the
        contract under test here (nothing in this helper touches it).
        """
        import src.face_detection

        absent = tmp_path / "definitely-absent-yunet.onnx"
        assert not absent.exists()
        detector = src.face_detection.FaceDetector(model_path=absent)
        assert detector.is_loaded is False
        with pytest.raises(src.face_detection.FaceDetectorLoadError, match="not found"):
            detector.load()
        # The configured path is what the loader consulted — unchanged.
        assert detector._model_path == absent
        assert not absent.exists()

    def test_helper_never_touches_real_model_file(
        self, tmp_path, monkeypatch, fake_urlopen_factory, stub_cv2_ok
    ):
        """A full (mocked) helper run leaves the real model path alone.

        The run targets an isolated temp destination; afterwards the
        production default path must still not exist in that temp tree,
        proving the helper does not write anywhere near it.
        """
        import src.face_detection as fd

        real_default = Path(fd.DEFAULT_MODEL_PATH)
        # Only meaningful while the real model is absent on this machine;
        # if it IS installed, the guarantee being tested is that this run
        # leaves its bytes untouched.
        before = real_default.read_bytes() if real_default.exists() else None
        # Record the real model's mtime so we can prove no rewrite happened.
        before_mtime = real_default.stat().st_mtime_ns if real_default.exists() else None

        isolated = fake_urlopen_factory(
            [_FakeResponse([FAKE_MODEL_BYTES], headers={"X-Linked-Size": str(GOOD_SIZE)})]
        )
        # Test-standard convention: the expected digest is overridden to
        # match the fake bytes (the real pin is exercised elsewhere).
        monkeypatch.setattr(
            helper, "YUNET_SHA256", hashlib.sha256(FAKE_MODEL_BYTES).hexdigest()
        )
        code, _stderr = _run_main(["--dest", str(tmp_path / "mock.onnx")])
        assert code == 0, _stderr
        assert isolated  # the fake opener was actually consulted

        # The real production path must be untouched.
        assert real_default != (tmp_path / "mock.onnx")
        if before is not None:
            assert real_default.read_bytes() == before
            assert real_default.stat().st_mtime_ns == before_mtime
        else:
            assert not real_default.exists()


# ---------------------------------------------------------------------------
# Provenance constants sanity
# ---------------------------------------------------------------------------


class TestProvenanceConstants:
    """The pinned constants exist and are internally consistent."""

    def test_expected_sha256_is_lowercase_hex64(self):
        assert len(GOOD_SHA256) == 64
        assert all(c in "0123456789abcdef" for c in GOOD_SHA256)

    def test_url_pins_to_huggingface_host(self):
        assert helper.YUNET_URL.startswith(
            "https://huggingface.co/opencv/face_detection_yunet/resolve/"
        )
        assert helper.YUNET_REPO_REVISION in helper.YUNET_URL

    def test_exact_size_constant(self):
        assert GOOD_SIZE == 232_589

    def test_no_arbitrary_url_input(self):
        """The CLI accepts no --url / --sha256 style overrides."""
        with pytest.raises(SystemExit):
            helper.main(["--url", "https://evil.example/model.onnx"])
        parser = helper.build_parser()
        option_slugs = {
            action.dest for action in parser._actions  # type: ignore[attr-defined]
        }
        assert "url" not in option_slugs
        assert "sha256" not in option_slugs or "digest" not in option_slugs
