import inspect
import sys
import types

fake_milvus_tool_module = types.ModuleType("tools.milvus_tool")
fake_milvus_tool_module.create_milvus_tools = lambda *args, **kwargs: {"milvus": True}
sys.modules.setdefault("tools.milvus_tool", fake_milvus_tool_module)

fake_rag_tool_module = types.ModuleType("tools.rag_retrieval_tool")
fake_rag_tool_module.KnowledgeRetrievalToolkit = object
fake_rag_tool_module.create_knowledge_retrieval_toolkit = lambda **kwargs: {"rag": kwargs}
sys.modules.setdefault("tools.rag_retrieval_tool", fake_rag_tool_module)

import agent.agent_initializer as agent_initializer_module
import agent.summary_agent as summary_agent_module
import agent.work_reply_agent as work_reply_agent_module


class DummyConfigLoader:
    def __init__(self, with_tools: bool = True):
        self.with_tools = with_tools

    def get_llm_config(self):
        return {"api_key": "k", "base_url": "u", "model_name": "m"}

    def get_milvus_config(self):
        return {"uri": "milvus"} if self.with_tools else None

    def get_embedding_config(self):
        return {"model": "embed"} if self.with_tools else None


def _patch_base_agent_init(monkeypatch, target_module):
    captured = {}

    def fake_agent_init(self, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(target_module, "DashScope", lambda id, api_key, base_url: {"id": id, "api_key": api_key, "base_url": base_url})
    monkeypatch.setattr("agno.agent.Agent.__init__", fake_agent_init)
    return captured


def test_summary_agent_only_uses_summary_prompt(monkeypatch):
    captured = _patch_base_agent_init(monkeypatch, summary_agent_module)
    monkeypatch.setattr(summary_agent_module, "SUMMARY_AGENT_INSTRUCTIONS", "SUMMARY_ONLY_PROMPT")
    summary_agent_module.SummaryAgent(config_loader=DummyConfigLoader())
    assert captured["instructions"] == "SUMMARY_ONLY_PROMPT"
    source = inspect.getsource(summary_agent_module)
    assert "prompt.summary_agent_prompt" in source
    assert "prompt.work_reply_agent_prompt" not in source


def test_create_summary_agent_factory_passthrough(monkeypatch):
    called = {}

    class FakeSummaryAgent:
        def __init__(self, **kwargs):
            called.update(kwargs)

    monkeypatch.setattr(summary_agent_module, "SummaryAgent", FakeSummaryAgent)
    result = summary_agent_module.create_summary_agent(api_key="ak", base_url="bu", model_id="mid")
    assert isinstance(result, FakeSummaryAgent)
    assert called == {"api_key": "ak", "base_url": "bu", "model_id": "mid"}


def test_work_reply_agent_only_uses_work_prompt(monkeypatch):
    captured = _patch_base_agent_init(monkeypatch, work_reply_agent_module)
    monkeypatch.setattr(work_reply_agent_module, "WORK_REPLY_AGENT_INSTRUCTIONS", "WORK_ONLY_PROMPT")
    toolkit = object()
    work_reply_agent_module.WorkReplyAgent(config_loader=DummyConfigLoader(), toolkit=toolkit)
    assert captured["instructions"] == "WORK_ONLY_PROMPT"
    assert captured["tools"] == [toolkit]
    source = inspect.getsource(work_reply_agent_module)
    assert "prompt.work_reply_agent_prompt" in source
    assert "prompt.summary_agent_prompt" not in source


def test_work_reply_agent_auto_init_success(monkeypatch):
    captured = _patch_base_agent_init(monkeypatch, work_reply_agent_module)
    monkeypatch.setattr(work_reply_agent_module, "create_milvus_tools", lambda milvus_config, embedder_config: {"milvus": milvus_config, "embedder": embedder_config})
    monkeypatch.setattr(work_reply_agent_module, "create_knowledge_retrieval_toolkit", lambda **kwargs: {"toolkit": kwargs})
    work_reply_agent_module.WorkReplyAgent(config_loader=DummyConfigLoader(with_tools=True), toolkit=None, auto_init_tools=True)
    assert len(captured["tools"]) == 1
    assert "toolkit" in captured["tools"][0]


def test_work_reply_agent_auto_init_exception(monkeypatch):
    captured = _patch_base_agent_init(monkeypatch, work_reply_agent_module)
    monkeypatch.setattr(work_reply_agent_module, "create_milvus_tools", lambda milvus_config, embedder_config: (_ for _ in ()).throw(RuntimeError("x")))
    work_reply_agent_module.WorkReplyAgent(config_loader=DummyConfigLoader(with_tools=True), toolkit=None, auto_init_tools=True)
    assert captured["tools"] == []


def test_work_reply_agent_auto_init_disabled(monkeypatch):
    captured = _patch_base_agent_init(monkeypatch, work_reply_agent_module)

    def _should_not_call(*args, **kwargs):
        raise AssertionError("should not call")

    monkeypatch.setattr(work_reply_agent_module, "create_milvus_tools", _should_not_call)
    work_reply_agent_module.WorkReplyAgent(config_loader=DummyConfigLoader(with_tools=True), toolkit=None, auto_init_tools=False)
    assert captured["tools"] == []


def test_create_work_reply_agent_factory_passthrough(monkeypatch):
    called = {}

    class FakeWorkReplyAgent:
        def __init__(self, **kwargs):
            called.update(kwargs)

    monkeypatch.setattr(work_reply_agent_module, "WorkReplyAgent", FakeWorkReplyAgent)
    result = work_reply_agent_module.create_work_reply_agent(
        api_key="ak",
        base_url="bu",
        model_id="mid",
        toolkit="tk",
        auto_init_tools=False,
    )
    assert isinstance(result, FakeWorkReplyAgent)
    assert called == {
        "toolkit": "tk",
        "api_key": "ak",
        "base_url": "bu",
        "model_id": "mid",
        "auto_init_tools": False,
    }


def test_agent_initializer_responsibility_matrix_zero_overlap():
    matrix = agent_initializer_module.AgentInitializer.RESPONSIBILITY_MATRIX
    summary_set = set(matrix["summary_agent"])
    work_set = set(matrix["work_reply_agent"])
    expected = {"总结", "提炼", "归档", "业务回复", "上下文衔接", "用户交互"}
    assert summary_set.isdisjoint(work_set)
    assert summary_set | work_set == expected


def test_agent_initializer_builders(monkeypatch):
    called = {}

    def fake_summary(**kwargs):
        called["summary"] = kwargs
        return "summary-agent"

    def fake_work(**kwargs):
        called["work"] = kwargs
        return "work-agent"

    monkeypatch.setattr(agent_initializer_module, "create_summary_agent", fake_summary)
    monkeypatch.setattr(agent_initializer_module, "create_work_reply_agent", fake_work)

    init = agent_initializer_module.AgentInitializer(model_id="m", api_key="k", base_url="u")
    summary = init.create_summary_agent()
    work = init.create_work_reply_agent(toolkit="tk", enable_tools=False)
    assert summary == "summary-agent"
    assert work == "work-agent"
    assert called["summary"] == {"api_key": "k", "base_url": "u", "model_id": "m"}
    assert called["work"] == {"api_key": "k", "base_url": "u", "model_id": "m", "toolkit": "tk", "auto_init_tools": False}


def test_agent_initializer_team_and_agentos(monkeypatch):
    class FakeTeam:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeAgentOS:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(agent_initializer_module, "Team", FakeTeam)
    monkeypatch.setattr(agent_initializer_module, "AgentOS", FakeAgentOS)
    monkeypatch.setattr(agent_initializer_module, "DashScope", lambda id, api_key, base_url: {"id": id, "api_key": api_key, "base_url": base_url})

    init = agent_initializer_module.AgentInitializer(model_id="m", api_key="k", base_url="u")
    team = init.create_team_router("s-agent", "sum-agent")
    assert team.kwargs["members"] == ["s-agent", "sum-agent"]
    assert team.kwargs["instructions"] == agent_initializer_module.AgentInitializer.TEAM_ROUTER_INSTRUCTIONS

    agentos = init.create_agentos("s-agent", "sum-agent", team)
    assert agentos.kwargs["agents"] == ["s-agent", "sum-agent"]
    assert agentos.kwargs["teams"] == [team]
