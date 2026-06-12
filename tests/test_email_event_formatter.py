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


def test_canvas_designer_request_uses_prepended_prompt_and_sender():
    formatter = _load_formatter_module()
    events = [
        {
            "uuid": "event-canvas",
            "created_at": "2026-06-12T10:00:00Z",
            "event_type": "canvas_designer_request",
            "prompt": "[Boss <boss@example.com>]: Aprobamos descuentos sobre 10% en Slack.",
            "channel_id": "conversation-1",
            "extra_params": {
                "reply_channel": "canvas",
                "sender": {
                    "name": "Boss",
                    "email": "boss@example.com",
                },
                "sender_organization_customer_uuid": "customer-boss",
            },
        },
    ]

    messages = formatter.EmailEventFormatter.format_events(
        events,
        channel_map={"conversation-1": (0, "canvas")},
        enabled_events=formatter.EMAIL_DEFAULT_EVENTS,
    )

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["name"] == "canvas_user"
    assert "[Mensaje canvas] [0: canvas]" in messages[0]["content"]
    assert "[Boss <boss@example.com>]: Aprobamos descuentos" in messages[0]["content"]


def test_canvas_designer_request_dedupes_on_actual_prompt():
    formatter = _load_formatter_module()
    events = [
        {
            "uuid": "event-canvas-1",
            "created_at": "2026-06-12T10:00:00Z",
            "event_type": "canvas_designer_request",
            "prompt": "[Ana <ana@example.com>]: Usamos HubSpot.",
            "extra_params": {"reply_channel": "canvas"},
        },
        {
            "uuid": "event-canvas-2",
            "created_at": "2026-06-12T10:01:00Z",
            "event_type": "canvas_designer_request",
            "prompt": "[Ana <ana@example.com>]: Usamos HubSpot.",
            "extra_params": {"reply_channel": "canvas"},
        },
        {
            "uuid": "event-canvas-3",
            "created_at": "2026-06-12T10:02:00Z",
            "event_type": "canvas_designer_request",
            "prompt": "[Boss <boss@example.com>]: Usamos HubSpot.",
            "extra_params": {"reply_channel": "canvas"},
        },
    ]

    messages = formatter.EmailEventFormatter.format_events(
        events,
        enabled_events=formatter.EMAIL_DEFAULT_EVENTS,
    )

    canvas_messages = [
        msg for msg in messages if msg["role"] == "user" and "[Mensaje canvas]" in msg["content"]
    ]
    assert len(canvas_messages) == 2
    assert "Ana <ana@example.com>" in canvas_messages[0]["content"]
    assert "Boss <boss@example.com>" in canvas_messages[1]["content"]
