import importlib.util
from pathlib import Path


def _load_formatter_module():
    module_path = Path(__file__).resolve().parents[1] / "src/backend/email_event_formatter.py"
    spec = importlib.util.spec_from_file_location("email_event_formatter", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_originated_email_body_flushes_after_tool_result():
    formatter = _load_formatter_module()
    events = [
        {
            "uuid": "event-call",
            "created_at": "2026-06-10T10:00:00Z",
            "event_type": "function_call",
            "prompt": "",
            "extra_params": {
                "tool_calls": [
                    {
                        "id": "call-email-1",
                        "name": "EmailAlUsuarioFn",
                        "args": {
                            "reasoning": "Ask for exceptions.",
                            "body": "Hay alguna excepcion que debamos considerar?",
                        },
                    }
                ]
            },
        },
        {
            "uuid": "event-email",
            "created_at": "2026-06-10T10:00:01Z",
            "event_type": "email_to_user",
            "prompt": "Ask for exceptions.",
            "extra_params": {
                "tool_call_id": "call-email-1",
                "body": "Hay alguna excepcion que debamos considerar?",
            },
        },
        {
            "uuid": "event-response",
            "created_at": "2026-06-10T10:00:02Z",
            "event_type": "function_call_response",
            "prompt": "Se ha enviado un correo al usuario.",
            "extra_params": {
                "tool_call_id": "call-email-1",
                "tool_name": "EmailAlUsuarioFn",
            },
        },
    ]

    messages = formatter.EmailEventFormatter.format_events(
        events,
        enabled_events=formatter.EMAIL_DEFAULT_EVENTS,
    )

    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] is None
    assert messages[0]["tool_calls"][0]["function"]["name"] == "EmailAlUsuarioFn"
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "call-email-1"
    assert messages[2]["role"] == "assistant"
    assert "Hay alguna excepcion que debamos considerar?" in messages[2]["content"]
    assert "Ask for exceptions." not in messages[2]["content"]


def test_terminal_email_body_renders_without_dangling_tool_call():
    formatter = _load_formatter_module()
    events = [
        {
            "uuid": "event-inbound",
            "created_at": "2026-06-10T10:00:00Z",
            "event_type": "received_email",
            "prompt": "",
            "extra_params": {
                "body": "Si falta el RUT de empresa, pedimos e-rut y RUT del representante legal.",
                "sender_email": "leo@chask.io",
            },
        },
        {
            "uuid": "event-call",
            "created_at": "2026-06-10T10:01:00Z",
            "event_type": "function_call",
            "prompt": "",
            "extra_params": {
                "tool_calls": [
                    {
                        "id": "call-email-terminal",
                        "name": "EmailAlUsuarioFn",
                        "args": {
                            "reasoning": "Acknowledge and ask next question.",
                            "body": "Perfecto, incorporo e-rut y RUT del representante legal. Hay algun otro documento que finanzas revise?",
                        },
                    }
                ]
            },
        },
        {
            "uuid": "event-email",
            "created_at": "2026-06-10T10:01:01Z",
            "event_type": "email_to_user",
            "prompt": "Acknowledge and ask next question.",
            "extra_params": {
                "tool_call_id": "call-email-terminal",
                "body": "Perfecto, incorporo e-rut y RUT del representante legal. Hay algun otro documento que finanzas revise?",
            },
        },
    ]

    messages = formatter.EmailEventFormatter.format_events(
        events,
        enabled_events=formatter.EMAIL_DEFAULT_EVENTS,
    )

    assert messages[-1]["role"] == "assistant"
    assert "[Email enviado]" in messages[-1]["content"]
    assert "Perfecto, incorporo e-rut y RUT del representante legal." in messages[-1]["content"]
    assert "Acknowledge and ask next question." not in messages[-1]["content"]
    for index, message in enumerate(messages):
        if "tool_calls" not in message:
            continue
        assert index + 1 < len(messages)
        assert messages[index + 1]["role"] == "tool"


def test_received_email_same_prompt_distinct_bodies_are_not_deduped():
    formatter = _load_formatter_module()
    generic_prompt = "New email received with subject: Re: Mapeo de proceso"
    events = [
        {
            "uuid": "event-slack",
            "created_at": "2026-06-10T10:00:00Z",
            "event_type": "received_email",
            "prompt": generic_prompt,
            "extra_params": {
                "body": "Cuando Operaciones aprueba el pedido, notificamos por Slack.",
                "sender_email": "leo@chask.io",
            },
        },
        {
            "uuid": "event-calendar",
            "created_at": "2026-06-10T10:01:00Z",
            "event_type": "received_email",
            "prompt": generic_prompt,
            "extra_params": {
                "body": "Antes de iniciar, agendamos el kickoff en Google Calendar.",
                "sender_email": "leo@chask.io",
            },
        },
    ]

    messages = formatter.EmailEventFormatter.format_events(
        events,
        enabled_events=formatter.EMAIL_DEFAULT_EVENTS,
    )

    received_messages = [
        msg for msg in messages if msg["role"] == "user" and "[Email recibido]" in msg["content"]
    ]
    assert len(received_messages) == 2
    assert "Slack" in received_messages[0]["content"]
    assert "Google Calendar" in received_messages[1]["content"]


def test_received_email_same_prompt_identical_body_is_deduped():
    formatter = _load_formatter_module()
    generic_prompt = "New email received with subject: Re: Mapeo de proceso"
    events = [
        {
            "uuid": "event-slack-original",
            "created_at": "2026-06-10T10:00:00Z",
            "event_type": "received_email",
            "prompt": generic_prompt,
            "extra_params": {
                "body": "Cuando Operaciones aprueba el pedido, notificamos por Slack.",
                "sender_email": "leo@chask.io",
            },
        },
        {
            "uuid": "event-slack-redelivery",
            "created_at": "2026-06-10T10:01:00Z",
            "event_type": "received_email",
            "prompt": generic_prompt,
            "extra_params": {
                "body": "Cuando Operaciones aprueba el pedido, notificamos por Slack.",
                "sender_email": "leo@chask.io",
            },
        },
    ]

    messages = formatter.EmailEventFormatter.format_events(
        events,
        enabled_events=formatter.EMAIL_DEFAULT_EVENTS,
    )

    received_messages = [
        msg for msg in messages if msg["role"] == "user" and "[Email recibido]" in msg["content"]
    ]
    assert len(received_messages) == 1
    assert "Slack" in received_messages[0]["content"]
