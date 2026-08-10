import json
import sqlite3

from lib.hermes_backfill import sessions_from_db


def test_hermes_projection_preserves_reasoning_tools_usage_and_lineage(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE sessions (id TEXT, source TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL,
      title TEXT, model TEXT, cwd TEXT, input_tokens INTEGER, output_tokens INTEGER, actual_cost_usd REAL,
      estimated_cost_usd REAL, origin_json TEXT, end_reason TEXT);
    CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
      tool_calls TEXT, tool_name TEXT, effect_disposition TEXT, timestamp REAL, token_count INTEGER,
      finish_reason TEXT, reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT);
    """)
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("h1", "cli", "parent", 1, 5, "Title", "model", "/work", 10, 20, None, .5, "{}", "done"))
    calls = json.dumps([{"id":"call-1","function":{"name":"terminal","arguments":"{\"command\":\"pwd\"}"}}])
    con.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (1,"h1","user","Hello",None,None,None,None,2,None,None,None,None,None),
        (2,"h1","assistant","Working",None,calls,None,None,3,4,"tool_calls",None,"Think",None),
        (3,"h1","tool","/work","call-1",None,"terminal",None,4,None,None,None,None,None),
    ])
    con.commit(); con.close()
    session = sessions_from_db(db)[0]
    assert session.parent_session_id == "parent"
    assert [event.type for event in session.events] == ["SessionStart","UserPromptSubmit","Reasoning","AssistantResponse","PreToolUse","PostToolUse","SessionEnd"]
    assert session.events[0].tokens_in == 10 and session.events[0].cost_usd == .5
    assert session.events[4].data["tool_use_id"] == session.events[5].data["tool_use_id"] == "call-1"
    assert all(event.runtime == "hermes" for event in session.events)
