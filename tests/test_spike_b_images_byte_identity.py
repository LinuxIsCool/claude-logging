"""task-508 Phase 0 Spike B — byte-identity test for lib/images.py extraction.

Verifies extract_images_from_prompt() and extract_images_from_transcript()
produce IDENTICAL outputs to the inline versions in hooks/log_event.py
across 100+ synthetic fixture inputs spanning the full input space:

  - prompt as string (5 fixtures)
  - prompt as None / non-list / non-dict (5 fixtures)
  - prompt as list of text blocks only (10 fixtures, varying length)
  - prompt as list with base64 image blocks (40 fixtures, all 5 ALLOWED_IMAGE_TYPES × variants)
  - prompt as list with URL image blocks (10 fixtures, varying media types)
  - prompt as list mixing text + base64 + URL blocks (20 fixtures)
  - prompt with invalid base64 / unknown media types / missing fields (10 fixtures)
  - transcript-path fixtures (10 fixtures, various user-message configurations)

If ANY fixture produces a different output between the inline and extracted
versions, the test fails with the specific input that diverged.

Spike B PASSES → Phase 2 can safely promote lib/images.py and remove inline
versions. Spike B FAILS → root-cause before any refactor commitment.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from base64 import b64encode
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
HOOKS_DIR = PLUGIN_ROOT / "hooks"
LIB_DIR = PLUGIN_ROOT / "lib"

# Ensure plugin paths importable
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))


def _load_log_event_module():
    """Load hooks/log_event.py as a module without executing its main()."""
    spec = importlib.util.spec_from_file_location(
        "log_event_baseline", HOOKS_DIR / "log_event.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["log_event_baseline"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def baseline():
    """Inline versions from hooks/log_event.py."""
    return _load_log_event_module()


@pytest.fixture(scope="module")
def refactored():
    """Extracted versions from lib/images.py."""
    from lib import images as images_mod
    return images_mod


# ---- Fixture generators ---------------------------------------------------

def _make_b64_image(payload: bytes, media_type: str = "image/png") -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": b64encode(payload).decode("ascii"),
        },
    }


def _make_url_image(url: str, media_type: str = "image/jpeg") -> dict:
    return {
        "type": "image",
        "source": {
            "type": "url",
            "url": url,
            "media_type": media_type,
        },
    }


def _make_text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def generate_prompt_fixtures() -> list:
    """Generate 100+ synthetic prompt-shape fixtures spanning input space."""
    fixtures = []

    # Group 1: string prompts (5)
    fixtures += [
        ("string-empty", ""),
        ("string-short", "hello"),
        ("string-long", "x" * 5000),
        ("string-unicode", "café 日本語 🌍"),
        ("string-with-newlines", "line1\nline2\nline3"),
    ]

    # Group 2: non-list / non-dict edge cases (5)
    fixtures += [
        ("none", None),
        ("int", 42),
        ("dict-not-list", {"foo": "bar"}),
        ("tuple", ("a", "b")),
        ("nested-list", [[1, 2], [3, 4]]),
    ]

    # Group 3: list of text blocks only (10)
    for i in range(10):
        blocks = [_make_text_block(f"part {j}") for j in range(i + 1)]
        fixtures.append((f"text-blocks-{i}", blocks))

    # Group 4: base64 image blocks across all 5 media types (40 fixtures)
    media_types = ["image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp", "image/svg+xml"]
    payloads = [b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF89a", b"RIFF", b"\x00" * 100]
    for mt_idx, media_type in enumerate(media_types):
        for p_idx, payload in enumerate(payloads):
            blocks = [_make_b64_image(payload, media_type)]
            fixtures.append((f"b64-{mt_idx}-{p_idx}", blocks))
    # Also: payload prepended with text
    for mt_idx, media_type in enumerate(media_types[:5]):
        blocks = [_make_text_block("describe this"), _make_b64_image(b"data", media_type)]
        fixtures.append((f"b64-mixed-text-{mt_idx}", blocks))

    # Group 5: URL image blocks (10)
    for i in range(10):
        url = f"https://example.com/img{i}.{['jpg', 'png', 'gif', 'webp'][i % 4]}"
        media_type = ["image/jpeg", "image/png", "image/gif", "image/webp"][i % 4]
        blocks = [_make_url_image(url, media_type)]
        fixtures.append((f"url-{i}", blocks))

    # Group 6: mixed text + b64 + URL (20)
    for i in range(20):
        blocks = [
            _make_text_block(f"intro {i}"),
            _make_b64_image(f"img{i}".encode(), "image/png"),
            _make_url_image(f"https://example.com/{i}.jpg"),
            _make_text_block(f"outro {i}"),
        ]
        fixtures.append((f"mixed-{i}", blocks))

    # Group 7: edge cases (10)
    fixtures += [
        ("empty-list", []),
        ("non-dict-block", ["raw string element"]),
        ("malformed-block-no-type", [{"data": "no type"}]),
        ("malformed-block-bad-type", [{"type": "video"}]),
        ("b64-no-data", [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ""}}]),
        ("b64-no-source", [{"type": "image"}]),
        ("b64-no-media-type", [{"type": "image", "source": {"type": "base64", "data": b64encode(b"x").decode()}}]),
        ("url-no-url", [{"type": "image", "source": {"type": "url"}}]),
        ("unknown-source-type", [{"type": "image", "source": {"type": "filesystem", "path": "/tmp/x.png"}}]),
        ("multiple-image-types-stress", [
            _make_b64_image(b"a", "image/png"),
            _make_b64_image(b"b", "image/jpeg"),
            _make_url_image("https://x.io/y.gif", "image/gif"),
            _make_b64_image(b"c", "image/webp"),
        ]),
    ]

    # Group 8: additional stress fixtures to clear the 100-fixture bar
    fixtures += [
        ("deeply-nested-mixed", [
            _make_text_block("a"), _make_text_block("b"), _make_text_block("c"),
            _make_b64_image(b"\x00\x01\x02", "image/jpeg"),
            _make_url_image("https://cdn.example.com/path/to/image.png", "image/png"),
            _make_text_block("d"),
        ]),
        ("large-text-blocks", [_make_text_block("x" * 1000) for _ in range(5)]),
        ("ten-base64-images", [
            _make_b64_image(f"payload-{i}".encode(), "image/png") for i in range(10)
        ]),
        ("ten-url-images", [
            _make_url_image(f"https://example.com/{i}", "image/jpeg") for i in range(10)
        ]),
        ("alternating-text-image", [
            _make_text_block(f"text {i}") if i % 2 == 0 else _make_b64_image(f"img{i}".encode(), "image/png")
            for i in range(20)
        ]),
    ]

    return fixtures


def generate_transcript_fixtures(tmp_path: Path) -> list:
    """Write 10 transcript JSONL files spanning user-message configurations.

    Each fixture writes a transcript at tmp_path/<id>/transcript.jsonl
    and returns (id, path, expected_user_msg_count).
    """
    fixtures = []
    tmp_path.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        d = tmp_path / f"t{i}"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "transcript.jsonl"
        lines = []
        # Mix in user messages and non-user entries
        for j in range(i + 1):
            if j % 3 == 0:
                # user with text-only string content
                lines.append({"type": "user", "message": {"content": "plain string"}})
            elif j % 3 == 1:
                # user with image block list
                lines.append({"type": "user", "message": {"content": [
                    {"type": "text", "text": f"q{j}"},
                    _make_b64_image(f"data{j}".encode(), "image/png"),
                ]}})
            else:
                # non-user
                lines.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "ack"}]}})
        f.write_text("\n".join(json.dumps(line) for line in lines))
        fixtures.append((f"transcript-{i}", str(f)))
    return fixtures


# ---- Tests ----------------------------------------------------------------

@pytest.mark.parametrize("name,prompt", generate_prompt_fixtures())
def test_prompt_byte_identity(tmp_path, baseline, refactored, name, prompt):
    """Each prompt fixture must produce IDENTICAL outputs from both versions."""
    storage_a = tmp_path / "baseline"
    storage_b = tmp_path / "refactored"
    storage_a.mkdir()
    storage_b.mkdir()

    session_id = "sess-spikeB"
    event_id = "evt-" + name

    baseline_text, baseline_refs = baseline.extract_images_from_prompt(
        prompt, storage_a, session_id, event_id
    )
    refactored_text, refactored_refs = refactored.extract_images_from_prompt(
        prompt, storage_b, session_id, event_id
    )

    assert baseline_text == refactored_text, (
        f"[{name}] text divergence: baseline={baseline_text!r} refactored={refactored_text!r}"
    )
    # image_refs should be identical excluding the path (which differs due to
    # storage_path root). Compare on stable fields only.
    def _normalize_refs(refs):
        out = []
        for r in refs:
            normalized = {k: v for k, v in r.items() if k != "path"}
            if "path" in r:
                # Only the trailing filename matters for identity; both versions
                # use the same naming rule.
                normalized["_path_tail"] = Path(r["path"]).name
            out.append(normalized)
        return out

    assert _normalize_refs(baseline_refs) == _normalize_refs(refactored_refs), (
        f"[{name}] image_refs divergence:\n  baseline={baseline_refs}\n  refactored={refactored_refs}"
    )


def test_transcript_byte_identity(tmp_path, baseline, refactored):
    """Each transcript fixture must produce IDENTICAL outputs from both versions."""
    storage_a = tmp_path / "baseline"
    storage_b = tmp_path / "refactored"
    storage_a.mkdir()
    storage_b.mkdir()

    session_id = "sess-spikeBT"
    fixtures = generate_transcript_fixtures(tmp_path / "fixtures")

    divergences = []
    for name, path in fixtures:
        baseline_out = baseline.extract_images_from_transcript(path, storage_a, session_id)
        refactored_out = refactored.extract_images_from_transcript(path, storage_b, session_id)

        # Normalize: paths differ by storage root, but the filename tail must match
        def _normalize(d):
            out = {}
            for k, refs in d.items():
                out[k] = []
                for r in refs:
                    nr = {kk: vv for kk, vv in r.items() if kk != "path"}
                    if "path" in r:
                        nr["_path_tail"] = Path(r["path"]).name
                    out[k].append(nr)
            return out

        if _normalize(baseline_out) != _normalize(refactored_out):
            divergences.append(f"[{name}] baseline={baseline_out} refactored={refactored_out}")

    assert not divergences, f"transcript fixtures diverged:\n" + "\n".join(divergences)


def test_fixture_count_above_100():
    """Sanity check — we have >=100 prompt fixtures as Spike B promises."""
    fixtures = generate_prompt_fixtures()
    assert len(fixtures) >= 100, f"only {len(fixtures)} fixtures; Spike B requires >=100"
