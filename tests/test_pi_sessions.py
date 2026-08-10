import json
import sqlite3

from web import pi_sessions


def test_pi_session_graph_preserves_tree_depth_and_leaves(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_sessions, "PI_SESSIONS", tmp_path)
    path = tmp_path / "now_session-id.jsonl"
    rows = [
        {"type":"session","version":3,"id":"session-id"},
        {"type":"message","id":"a","parentId":None,"timestamp":"t1","message":{"role":"user","content":"one"}},
        {"type":"message","id":"b","parentId":"a","timestamp":"t2","message":{"role":"assistant","content":"two"}},
        {"type":"message","id":"c","parentId":"a","timestamp":"t3","message":{"role":"user","content":"branch"}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows))
    graph = pi_sessions.session_graph("session-id")
    assert graph["version"] == 3
    assert graph["kind"] == "message_tree" and graph["runtime"] == "pi"
    assert [node["depth"] for node in graph["nodes"]] == [0, 1, 1]
    assert graph["leaves"] == ["b", "c"]


def test_prime_agent_uses_the_shared_message_tree(tmp_path, monkeypatch):
    monkeypatch.setitem(pi_sessions.FAMILY_SESSIONS, "prime-agent", tmp_path)
    path = tmp_path / "prime-id.jsonl"
    path.write_text("\n".join([
        json.dumps({"type":"session","version":3,"id":"prime-id"}),
        json.dumps({"type":"message","id":"a","parentId":None,"message":{"role":"user","content":"hello"}}),
    ]))
    graph = pi_sessions.session_graph("prime-id", "prime-agent")
    assert graph["runtime"] == "prime-agent" and graph["nodes"][0]["role"] == "user"


def test_large_family_tree_returns_a_bounded_structural_projection(tmp_path, monkeypatch):
    monkeypatch.setitem(pi_sessions.FAMILY_SESSIONS, "omp", tmp_path)
    rows = [{"type":"session","version":3,"id":"large"}]
    parent = None
    for index in range(500):
        node_id = f"n{index}"
        rows.append({"type":"message","id":node_id,"parentId":parent,"message":{"role":"assistant","content":"x"}})
        parent = node_id
    (tmp_path / "large.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    graph = pi_sessions.session_graph("large", "omp")
    assert graph["total_nodes"] == 500 and len(graph["nodes"]) <= 300
    assert graph["collapsed_nodes"] == 500 - len(graph["nodes"])
    assert graph["nodes"][0]["id"] == "n0" and graph["nodes"][-1]["id"] == "n499"


def test_hermes_graph_projects_parent_and_child_sessions(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT,parent_session_id TEXT,title TEXT,source TEXT,started_at REAL)")
    con.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", [
        ("root", None, "Root", "cli", 1), ("child", "root", "Child", "subagent", 2),
        ("grandchild", "child", None, "subagent", 3), ("unrelated", None, "Other", "cli", 4),
    ])
    con.commit(); con.close()
    monkeypatch.setattr(pi_sessions, "HERMES_DB", db)
    graph = pi_sessions.session_graph("child", "hermes")
    assert graph["kind"] == "session_lineage"
    assert [node["id"] for node in graph["nodes"]] == ["root", "child", "grandchild"]
    assert [node["depth"] for node in graph["nodes"]] == [0, 1, 2]
    assert graph["leaves"] == ["grandchild"]
    assert next(node for node in graph["nodes"] if node["id"] == "child")["current"] is True
