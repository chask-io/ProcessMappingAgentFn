import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _load_function_logic():
    src_path = Path(__file__).resolve().parents[1] / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))
    for module_name in list(sys.modules):
        if module_name == "backend" or module_name.startswith("backend."):
            del sys.modules[module_name]
    with patch.dict(
        "sys.modules",
        {
            "chask_foundation.backend.agent_wrapper": MagicMock(
                AgentConfig=lambda **kwargs: SimpleNamespace(**kwargs),
                AgentFunctionBackend=object,
                AgentWrapper=object,
            ),
            "chask_foundation.backend.models": MagicMock(OrchestrationEvent=object),
            "api.orchestrator_requests": MagicMock(orchestrator_api_manager=MagicMock()),
            "api.internal_whatsapp_requests": MagicMock(internal_whatsapp_api_manager=MagicMock()),
            "dynamic_tools": MagicMock(),
        },
    ):
        return importlib.import_module("backend.function_logic")


def _tool(name):
    return type(name, (), {})


def _wrapper(module, reply_channel=None, event_type="received_email"):
    wrapper = module._ProcessMappingAgentWrapper.__new__(module._ProcessMappingAgentWrapper)
    wrapper.orchestration_event = SimpleNamespace(
        event_type=event_type,
        extra_params={"reply_channel": reply_channel} if reply_channel else {},
    )
    wrapper.all_dynamic_tools = [
        _tool("EmailAlUsuarioFn"),
        _tool("ReplyInCanvasFn"),
        _tool("WhatsappAlUsuarioFn"),
        _tool("EditCanvasNodesFn"),
    ]
    wrapper.tool_selection_dict = {tool.__name__: tool for tool in wrapper.all_dynamic_tools}
    wrapper.function_schemas = [
        {"type": "function", "function": {"name": "EmailAlUsuarioFn"}},
        {"type": "function", "function": {"name": "ReplyInCanvasFn"}},
        {"type": "function", "function": {"name": "WhatsappAlUsuarioFn"}},
        {"type": "function", "function": {"name": "EditCanvasNodesFn"}},
    ]
    return wrapper


def _schema_names(wrapper):
    return {schema["function"]["name"] for schema in wrapper.function_schemas}


def test_canvas_reply_channel_only_exposes_canvas_reply_tool():
    module = _load_function_logic()
    wrapper = _wrapper(module, reply_channel="canvas", event_type="canvas_designer_request")

    wrapper._filter_reply_tools_for_channel()

    assert "ReplyInCanvasFn" in wrapper.tool_selection_dict
    assert "EmailAlUsuarioFn" not in wrapper.tool_selection_dict
    assert "WhatsappAlUsuarioFn" not in wrapper.tool_selection_dict
    assert "EditCanvasNodesFn" in wrapper.tool_selection_dict
    assert _schema_names(wrapper) == {"ReplyInCanvasFn", "EditCanvasNodesFn"}


def test_email_reply_channel_only_exposes_email_reply_tool_by_default():
    module = _load_function_logic()
    wrapper = _wrapper(module)

    wrapper._filter_reply_tools_for_channel()

    assert "EmailAlUsuarioFn" in wrapper.tool_selection_dict
    assert "ReplyInCanvasFn" not in wrapper.tool_selection_dict
    assert "WhatsappAlUsuarioFn" not in wrapper.tool_selection_dict
    assert "EditCanvasNodesFn" in wrapper.tool_selection_dict
    assert _schema_names(wrapper) == {"EmailAlUsuarioFn", "EditCanvasNodesFn"}


def test_whatsapp_reply_channel_only_exposes_whatsapp_reply_tool():
    module = _load_function_logic()
    wrapper = _wrapper(module, reply_channel="whatsapp")

    wrapper._filter_reply_tools_for_channel()

    assert "WhatsappAlUsuarioFn" in wrapper.tool_selection_dict
    assert "EmailAlUsuarioFn" not in wrapper.tool_selection_dict
    assert "ReplyInCanvasFn" not in wrapper.tool_selection_dict
    assert "EditCanvasNodesFn" in wrapper.tool_selection_dict
    assert _schema_names(wrapper) == {"WhatsappAlUsuarioFn", "EditCanvasNodesFn"}
