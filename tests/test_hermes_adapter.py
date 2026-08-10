from adapters.hermes.log_event import normalize


def test_hermes_tool_result_preserves_correlation_duration_and_failure():
    event, payload = normalize({
        "hook_event_name": "post_tool_call", "session_id": "h1", "cwd": "/work",
        "tool_name": "terminal", "tool_input": {"command": "false"},
        "extra": {"tool_call_id": "call-1", "turn_id": "turn-1", "status": "error", "duration_ms": 42, "error_message": "failed"},
    })
    assert event == "PostToolUseFailure"
    assert payload["data"]["tool_use_id"] == "call-1"
    assert payload["turn_id"] == "turn-1" and payload["duration_ms"] == 42


def test_hermes_llm_hooks_map_prompt_response_and_usage():
    prompt, prompt_payload = normalize({"hook_event_name":"pre_llm_call","session_id":"h1","extra":{"user_message":"Hello"}})
    response, response_payload = normalize({"hook_event_name":"post_llm_call","session_id":"h1","extra":{"assistant_response":"Hi"}})
    usage, usage_payload = normalize({"hook_event_name":"post_api_request","session_id":"h1","extra":{"api_duration":1.5,"usage":{"input_tokens":10,"output_tokens":4}}})
    assert (prompt, prompt_payload["data"]["prompt"]) == ("UserPromptSubmit", "Hello")
    assert (response, response_payload["data"]["response"]) == ("AssistantResponse", "Hi")
    assert usage == "Usage" and usage_payload["duration_ms"] == 1500
    assert usage_payload["tokens_in"] == 10 and usage_payload["tokens_out"] == 4


def test_hermes_doctor_payload_is_synthetic():
    _, payload = normalize({"hook_event_name":"pre_llm_call","session_id":"test-session","extra":{"user_message":"test"}})
    assert payload["_source_kind"] == "synthetic"
