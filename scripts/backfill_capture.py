#!/usr/bin/env python3
"""Backfill capture provenance onto rows written before legion_capture.

    python3 scripts/backfill_capture.py            # dry run, reports only
    python3 scripts/backfill_capture.py --apply    # write

Gives existing `prompts` rows a derived `uuid7` so old and new share one
address space, and a `kind` derived from what the row still carries. Also
clears `dictated`, which held the output of a heuristic measured 40% wrong.

Three rules this follows, each because breaking it caused a real problem:

**Derived, not random.** `uuid7(ts, session_id, prompt_id)` is reproducible, so
re-running produces byte-identical ids and anything citing a prompt keeps
citing the same prompt. Re-running is free.

**Backfilled rows are labelled `capture_source='backfill'`.** A row whose kind
was reconstructed afterwards is not the same evidence as one stamped at the
boundary, and a reader must be able to tell. `discriminator` is `undeclared`
unless the row still carries a `prompt_source`, because nothing declared it at
the time.

**`dictated` is cleared, not recomputed.** There is no better heuristic to
recompute it with. NULL says "unknown", which is true; 0 would say "not
dictated", which is a claim nobody can support.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from lib import capture  # noqa: E402
from lib.token_meter import is_synthetic, open_db  # noqa: E402

LOGGING_ROOT = Path.home() / ".claude/local/logging"


def kind_for(text: str, prompt_source: str | None):
    """(kind, discriminator) from what the row still carries.

    Deliberately conservative. The transcript may declare more, but this reads
    only the `prompts` row, so anything richer would be an inference dressed as
    a record.
    """
    if is_synthetic(text or ""):
        return capture.Kind.EXPANSION, capture.Discriminator.CHANNEL
    if prompt_source == "queued":
        return capture.Kind.QUEUED, capture.Discriminator.CHANNEL
    if prompt_source == "typed":
        # The old code wrote "typed" as a DEFAULT, not as a declaration, so it
        # is not evidence of anything. Treated as undeclared on purpose.
        return capture.Kind.TYPED, capture.Discriminator.UNDECLARED
    return capture.Kind.TYPED, capture.Discriminator.UNDECLARED


def backfill_db(path: Path, apply: bool) -> dict:
    stats = {"rows": 0, "stamped": 0, "dictated_cleared": 0, "already": 0}
    conn = open_db(path.parent.parent)          # <slug>/db/logging.db -> <slug>
    try:
        rows = conn.execute(
            "SELECT prompt_id, session_id, ts, text, prompt_source, uuid7, dictated "
            "FROM prompts"
        ).fetchall()
        for pid, sid, ts, text, psrc, existing, dictated in rows:
            stats["rows"] += 1
            if existing:
                stats["already"] += 1
                if dictated is not None and apply:
                    conn.execute("UPDATE prompts SET dictated=NULL WHERE prompt_id=?", (pid,))
                    stats["dictated_cleared"] += 1
                elif dictated is not None:
                    stats["dictated_cleared"] += 1
                continue
            kind, disc = kind_for(text, psrc)
            try:
                uid = capture.uuid7(ts, sid or "", pid or "")
            except capture.IdentityError:
                # An unparseable timestamp is a real defect in the row; skip it
                # rather than stamp an id claiming 1970.
                continue
            if dictated is not None:
                stats["dictated_cleared"] += 1
            if apply:
                conn.execute(
                    "UPDATE prompts SET uuid7=?, kind=?, discriminator=?, "
                    "capture_source='backfill', captured_at=?, dictated=NULL "
                    "WHERE prompt_id=?",
                    (uid, str(kind), str(disc), ts, pid))
            stats["stamped"] += 1
        if apply:
            conn.commit()
    finally:
        conn.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--root", type=Path, default=LOGGING_ROOT)
    a = ap.parse_args()

    capture.require()
    dbs = sorted(a.root.glob("*/db/logging.db"))
    if not dbs:
        print(f"no logging databases under {a.root}")
        return 1

    total = {"rows": 0, "stamped": 0, "dictated_cleared": 0, "already": 0}
    failed = []
    for db in dbs:
        try:
            s = backfill_db(db, a.apply)
        except sqlite3.Error as e:
            failed.append((db, str(e)))
            continue
        for k in total:
            total[k] += s[k]

    verb = "stamped" if a.apply else "would stamp"
    print(f"databases       : {len(dbs)}  ({len(failed)} unreadable)")
    print(f"prompt rows     : {total['rows']:,}")
    print(f"{verb:16}: {total['stamped']:,}")
    print(f"already stamped : {total['already']:,}")
    print(f"dictated cleared: {total['dictated_cleared']:,}"
          f"{'' if a.apply else ' (would)'}")
    for db, err in failed[:5]:
        print(f"  unreadable: {db} — {err}")
    if not a.apply:
        print("\ndry run. re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
