"""The capture boundary: provenance stamped where it is still known.

Each test corresponds to a defect this adoption closes. Measured against the
real corpus before the change:

  dictated        40% wrong — 77 of 192 positives on rows that structurally
                  could not be speech
  prompt_source   defaulted to "typed", a fabricated declaration
  model           NULL in 2,955 of 2,955 rows, no writer, no payload field
  cron identity   1,085 machine runs wearing the human's identity
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib import capture  # noqa: E402
from lib import token_meter as tm  # noqa: E402

pytestmark = pytest.mark.skipif(
    not capture.CAPTURE_AVAILABLE,
    reason=f"legion_capture unavailable: {capture.IMPORT_ERROR}")


@pytest.fixture
def conn(tmp_path):
    c = tm.open_db(tmp_path)
    yield c
    c.close()


def row(conn, pid="p1"):
    cur = conn.execute("SELECT * FROM prompts WHERE prompt_id=?", (pid,))
    names = [d[0] for d in cur.description]
    r = cur.fetchone()
    return dict(zip(names, r)) if r else None


# --------------------------------------------------------------------------
# what the hook stamps
# --------------------------------------------------------------------------


def test_hook_stamps_kind_from_the_channel(conn):
    """The UserPromptSubmit hook fires on submit and on nothing else, so the
    channel proves a human submitted. It proves nothing about typed-versus-
    spoken, which is why the discriminator is `channel` and not `declared`."""
    tm.record_prompt(conn, {"prompt_id": "p1", "session_id": "s1", "prompt": "hi"})
    r = row(conn)
    assert r["kind"] == "typed"
    assert r["discriminator"] == "channel"
    assert r["capture_source"] == "UserPromptSubmit"
    assert capture.is_valid(r["uuid7"])


def test_dictated_is_never_written_again(conn):
    """The heuristic was 40% wrong and silent for months. NULL says unknown,
    which is true. 0 would say 'not dictated', which nobody can support."""
    tm.record_prompt(conn, {"prompt_id": "p1", "session_id": "s1",
                            "prompt": "um, so, I I think the the thing"})
    assert row(conn)["dictated"] is None


def test_looks_dictated_is_a_loud_tombstone():
    """Retired, not deleted, so a caller fails instead of silently
    reintroducing the guess."""
    with pytest.raises(NotImplementedError):
        tm.looks_dictated("um so I I think")


def test_prompt_source_is_not_fabricated(conn):
    """It used to default to "typed" whether or not anything said so. An
    absent value is recoverable; a fabricated one is indistinguishable from
    evidence."""
    tm.record_prompt(conn, {"prompt_id": "p1", "session_id": "s1", "prompt": "hi"})
    assert row(conn)["prompt_source"] is None


def test_declared_prompt_source_is_preserved(conn):
    tm.record_prompt(conn, {"prompt_id": "p1", "session_id": "s1",
                            "prompt": "hi", "prompt_source": "queued"})
    assert row(conn)["prompt_source"] == "queued"


def test_synthetic_turns_are_not_submits(conn):
    """A task notification rides in on the user channel but nobody submitted
    it. Saying so at capture time is what makes counting prompts a COUNT with
    no subtraction."""
    tm.record_prompt(conn, {"prompt_id": "p1", "session_id": "s1",
                            "prompt": "<task-notification>x</task-notification>"})
    r = row(conn)
    assert r["kind"] == "expansion"
    assert not capture.is_submit({"kind": r["kind"]})


def test_counting_submits_needs_no_subtraction(conn):
    for i, text in enumerate([
        "a real prompt", "<task-notification>x</task-notification>",
        "another real one", "<command-name>journal</command-name>",
    ]):
        tm.record_prompt(conn, {"prompt_id": f"p{i}", "session_id": "s1",
                                "prompt": text})
    kinds = [k for (k,) in conn.execute("SELECT kind FROM prompts")]
    assert sum(1 for k in kinds if capture.is_submit({"kind": k})) == 2


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


def test_ids_are_derived_so_a_rebuild_does_not_renumber(tmp_path):
    a = tm.open_db(tmp_path / "a")
    b = tm.open_db(tmp_path / "b")
    payload = {"prompt_id": "p1", "session_id": "s1", "prompt": "hi"}
    tm.record_prompt(a, payload)
    tm.record_prompt(b, payload)
    assert row(a)["uuid7"] == row(b)["uuid7"]
    a.close()
    b.close()


def test_ids_carry_their_own_timestamp(conn):
    tm.record_prompt(conn, {"prompt_id": "p1", "session_id": "s1", "prompt": "hi"})
    r = row(conn)
    assert capture.matches_ts(r["uuid7"], r["ts"], tolerance_ms=2000)


def test_distinct_prompts_get_distinct_ids(conn):
    tm.record_prompt(conn, {"prompt_id": "p1", "session_id": "s1", "prompt": "x"})
    tm.record_prompt(conn, {"prompt_id": "p2", "session_id": "s1", "prompt": "x"})
    ids = {u for (u,) in conn.execute("SELECT uuid7 FROM prompts")}
    assert len(ids) == 2, "identical text in one session must not collide"


# --------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------


def test_an_unstamped_row_cannot_be_inserted(conn):
    """For the third writer somebody adds later. A convention holds only until
    the next capture site; a trigger is checked on every insert."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO prompts (prompt_id, session_id, ts, text) "
                     "VALUES ('x','s1','2026-01-01T00:00:00Z','hi')")


def test_a_row_cannot_be_updated_to_null(conn):
    tm.record_prompt(conn, {"prompt_id": "p1", "session_id": "s1", "prompt": "hi"})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE prompts SET kind=NULL WHERE prompt_id='p1'")


def test_guards_are_actually_installed(conn):
    assert capture.guard.guarded_columns(conn, "prompts") == {"uuid7", "kind"}


# --------------------------------------------------------------------------
# transcript path — where a real declaration exists
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rec,kind,disc", [
    ({"origin": {"kind": "human"}, "promptSource": "typed"}, "typed", "declared"),
    ({"origin": {"kind": "human"}}, "typed", "declared"),
    ({"promptSource": "queued"}, "queued", "channel"),
    ({}, "typed", "undeclared"),
    ({"origin": {"kind": "cron"}}, "scheduled", "declared"),
    ({"origin": {"kind": "sdk"}}, "scheduled", "declared"),
    ({"origin": {"kind": "task-notification"}}, "injection", "declared"),
])
def test_transcript_classification_uses_the_declaration(rec, kind, disc):
    """Unlike the hook payload, a transcript record often states its origin.
    The cascade takes the most authoritative declaration and records which one
    it used, so a reader can tell evidence from a fallback."""
    k, d = tm._classify_transcript_prompt(rec, synth=False, capture=capture)
    assert (str(k), str(d)) == (kind, disc)


def test_machine_origins_do_not_wear_the_human_identity():
    """1,085 cron and headless runs were counted as Shawn's prompts because
    nothing recorded the origin the harness had already declared."""
    for origin in ("cron", "sdk", "agent", "task-notification"):
        k, _ = tm._classify_transcript_prompt(
            {"origin": {"kind": origin}}, synth=False, capture=capture)
        assert not capture.is_submit({"kind": str(k)}), origin
