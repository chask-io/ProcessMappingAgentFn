import importlib.util
import sys
import types
from pathlib import Path


def _install_layer_stubs():
    class AgentConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AgentFunctionBackend:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AgentWrapper:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    agent_wrapper = types.ModuleType("chask_foundation.backend.agent_wrapper")
    agent_wrapper.AgentConfig = AgentConfig
    agent_wrapper.AgentFunctionBackend = AgentFunctionBackend
    agent_wrapper.AgentWrapper = AgentWrapper

    models = types.ModuleType("chask_foundation.backend.models")
    models.OrchestrationEvent = object

    foundation = types.ModuleType("chask_foundation")
    backend = types.ModuleType("chask_foundation.backend")

    sys.modules["chask_foundation"] = foundation
    sys.modules["chask_foundation.backend"] = backend
    sys.modules["chask_foundation.backend.agent_wrapper"] = agent_wrapper
    sys.modules["chask_foundation.backend.models"] = models

    api = types.ModuleType("api")
    orchestrator_requests = types.ModuleType("api.orchestrator_requests")
    internal_whatsapp_requests = types.ModuleType("api.internal_whatsapp_requests")
    canvas_requests = types.ModuleType("api.canvas_requests")
    orchestrator_requests.orchestrator_api_manager = types.SimpleNamespace(
        call=lambda *args, **kwargs: {}
    )
    internal_whatsapp_requests.internal_whatsapp_api_manager = types.SimpleNamespace(
        call=lambda *args, **kwargs: {}
    )
    canvas_requests.canvas_api_manager = types.SimpleNamespace()
    sys.modules["api"] = api
    sys.modules["api.orchestrator_requests"] = orchestrator_requests
    sys.modules["api.internal_whatsapp_requests"] = internal_whatsapp_requests
    sys.modules["api.canvas_requests"] = canvas_requests


def _load_function_logic():
    _install_layer_stubs()
    module_path = Path(__file__).resolve().parents[1] / "src/backend/function_logic.py"
    package = types.ModuleType("backend")
    package.__path__ = [str(module_path.parent)]
    sys.modules["backend"] = package
    spec = importlib.util.spec_from_file_location("backend.function_logic", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["backend.function_logic"] = module
    spec.loader.exec_module(module)
    return module


def test_current_version_context_is_final_message(monkeypatch):
    module = _load_function_logic()
    captured = {}

    class Response:
        status_code = 200
        text = '{"ok": true}'

        def json(self):
            return {
                "version": {"version_number": 7},
                "context": "## Current Canvas Version\ncurrent_version: v7",
            }

    def request(method, url, headers):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        return Response()

    sys.modules["api.canvas_requests"].canvas_api_manager = types.SimpleNamespace(
        base_url="https://app.chask.it/api/v2/canvas",
        session=types.SimpleNamespace(request=request),
    )
    event = types.SimpleNamespace(
        event_type="received_email",
        access_token="token-123",
        organization=types.SimpleNamespace(organization_id="org-123"),
        extra_params={
            "design_context": {
                "canvas_uuid": "canvas-123",
                "project_uuid": "project-123",
            }
        },
    )
    wrapper = module._ProcessMappingAgentWrapper(
        config=module.PROCESS_MAPPING_CONFIG,
        orchestration_event=event,
        openai_api_key="test",
        model="gpt-test",
    )

    monkeypatch.setattr(wrapper, "_build_system_prompt", lambda: "base prompt")
    monkeypatch.setattr(
        wrapper,
        "_build_channel_conversation_history",
        lambda: [{"role": "user", "content": "conversation"}],
    )
    monkeypatch.setattr(wrapper, "_is_collecting_pipeline_data", lambda: False)
    monkeypatch.setattr(module, "_build_canvas_context", lambda oe: "canvas context")
    monkeypatch.setattr(module, "_build_user_profile_context", lambda oe: "profile context")

    messages = wrapper._prepare_messages()

    assert messages[-3] == {"role": "system", "content": "canvas context"}
    assert messages[-2] == {"role": "system", "content": "profile context"}
    assert messages[-1]["role"] == "system"
    assert messages[-1]["content"].startswith("## Current Canvas Version")
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/get-current-canvas-version-context?canvas_uuid=canvas-123")
    assert captured["headers"]["Authorization"] == "Bearer token-123"
    assert captured["headers"]["Organization-ID"] == "org-123"


def test_continuation_recovers_original_agent_turn_uuid():
    module = _load_function_logic()
    event = types.SimpleNamespace(
        event_id="function-response-1",
        event_type="function_call_response",
        extra_params={},
    )
    wrapper = module._ProcessMappingAgentWrapper(
        config=module.PROCESS_MAPPING_CONFIG,
        orchestration_event=event,
        openai_api_key="test",
        model="gpt-test",
    )
    wrapper._raw_events = [
        {
            "uuid": "turn-123",
            "created_at": "2026-06-12T10:00:00Z",
            "event_type": "received_email",
            "extra_params": {},
        },
        {
            "uuid": "function-call-1",
            "created_at": "2026-06-12T10:00:01Z",
            "event_type": "function_call",
            "extra_params": {
                "tool_calls": [
                    {"args": {"agent_turn_uuid": "turn-123"}}
                ]
            },
        },
        {
            "uuid": "function-response-1",
            "created_at": "2026-06-12T10:00:02Z",
            "event_type": "function_call_response",
            "extra_params": {},
        },
    ]

    assert wrapper._resolve_stable_agent_turn_uuid() == "turn-123"
