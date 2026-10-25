import subagent


def test_writing_override(monkeypatch):
    monkeypatch.setenv("SUBAGENT_PI_THINKING", "low")
    monkeypatch.setenv("SUBAGENT_PI_THINKING_WRITING", "high")
    assert "content_writing_contract_repair" in subagent.WRITING_THINKING_TASKS
    for t in subagent.WRITING_THINKING_TASKS:
        assert subagent._thinking_strategy(t) == ("high", "env:SUBAGENT_PI_THINKING_WRITING")
    assert subagent._thinking_strategy("content_map")[0] == "low"
    assert subagent._thinking_strategy("ai_review")[0] == "low"
