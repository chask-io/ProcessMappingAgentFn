"""
ProcessMappingAgentFn - Business Logic

WhatsApp channel agent for automatic process mapping. Keeps the WhatsApp
AgentFunctionBackend event/response plumbing while adding canvas and target-user
profile context for member interviews.
"""

import json
import logging
import os
from typing import Any, Dict, List

from chask_foundation.backend.agent_wrapper import (
    AgentConfig,
    AgentFunctionBackend,
    AgentWrapper,
)
from chask_foundation.backend.models import OrchestrationEvent
from api.orchestrator_requests import orchestrator_api_manager
from api.internal_whatsapp_requests import internal_whatsapp_api_manager

from .whatsapp_prompt_builder import (
    apply_template_variables,
    build_whatsapp_system_prompt,
    get_whatsapp_prompt_data,
)
from .whatsapp_event_formatter import WhatsAppEventFormatter, WHATSAPP_DEFAULT_EVENTS

LAMBDA_NAME = os.getenv("AWS_LAMBDA_FUNCTION_NAME", "process_mapping_agent")
logger = logging.getLogger(__name__)

# =============================================================================
# Operator reminder
# =============================================================================

OPERATOR_REMINDER_TEXT = (
    "IMPORTANTE: El requerimiento está esperando una respuesta con la información "
    "solicitada. Usa la herramienta EnviarMensajeAlRequerimientoFn para enviar la "
    "información recopilada del usuario. Si ya enviaste la información o no aplica, "
    "ignora este mensaje."
)

PIPELINE_COLLECTION_REMINDER = (
    "Recuerda: hay un flujo activo esperando datos. "
    "Cuando tengas la información requerida, llama a SendPipelineDataFn "
    "para iniciar la ejecución."
)

MAX_FILE_CONTENT_CHARS = 3000
TEXT_MIME_PREFIXES = ("text/", "application/json", "application/xml")

# Module-level singleton; built once per container when BASE_DOMAIN is resolved.
_users_api_manager = None


def _get_api_credentials(oe: OrchestrationEvent) -> Dict[str, str]:
    """Return common API call kwargs."""
    return {
        "access_token": oe.access_token,
        "organization_id": str(oe.organization.organization_id),
    }


def _check_api_response(response: dict, label: str) -> bool:
    """Return True if the API response is successful. Logs warning otherwise."""
    status_code = response.get("status_code")
    if status_code is not None and status_code not in (200, 201):
        logger.warning("API error for %s: %s", label, response.get("error", response))
        return False
    return True


def _fetch_canvases(oe: OrchestrationEvent, project_uuid: str | None) -> list:
    """Fetch canvases for a project, or via session fallback. Returns [] on failure."""
    try:
        from api.canvas_requests import canvas_api_manager

        creds = _get_api_credentials(oe)

        if project_uuid:
            response = canvas_api_manager.call(
                "list_canvases_for_project",
                project_uuid=project_uuid,
                **creds,
            )
            if _check_api_response(response, "list_canvases_for_project"):
                canvases = response.get("canvases", [])
                if canvases:
                    return canvases

        session_uuid = oe.orchestration_session_uuid
        if session_uuid:
            logger.info("Falling back to session-based canvas lookup (session=%s)", session_uuid)
            response = canvas_api_manager.call(
                "list_canvases_for_session",
                orchestration_session_uuid=session_uuid,
                **creds,
            )
            if _check_api_response(response, "list_canvases_for_session"):
                return response.get("canvases", [])

        return []
    except Exception as e:
        logger.warning("Failed to fetch canvases: %s", e)
        return []


def _fetch_and_format_project_files(oe: OrchestrationEvent, project_uuid: str | None) -> str:
    """Fetch project files with content and format as markdown."""
    if not project_uuid:
        return ""
    try:
        from api.files_requests import files_api_manager

        creds = _get_api_credentials(oe)
        response = files_api_manager.call(
            "get_files_for_project",
            project_uuid=project_uuid,
            **creds,
        )
        if not _check_api_response(response, "get_files_for_project"):
            return ""
        files = response.get("files", [])
        if not files:
            return ""

        lines = ["\n\n## Archivos del Proyecto\n"]
        for f in files:
            filename = f.get("filename", "unknown")
            mime_type = f.get("mime_type", "")
            file_uuid = f.get("uuid", "")

            lines.append(f"### {filename} ({mime_type})")

            content = _fetch_file_content(files_api_manager, file_uuid, mime_type, creds)
            if content:
                lines.append(f"```\n{content}\n```")

        return "\n".join(lines)
    except Exception as e:
        logger.warning("Failed to fetch project files: %s", e)
        return ""


def _fetch_file_content(api_manager, file_uuid: str, mime_type: str, creds: dict) -> str:
    """Fetch and truncate file content. Returns '' for non-text or on failure."""
    if not file_uuid or not any(mime_type.startswith(p) for p in TEXT_MIME_PREFIXES):
        return ""
    try:
        response = api_manager.call(
            "get_file_content",
            file_uuid=file_uuid,
            **creds,
        )
        if not _check_api_response(response, "get_file_content"):
            return ""
        content = response.get("content", "")
        if not content:
            return ""
        content = content.replace("\x00", "")
        if len(content) > MAX_FILE_CONTENT_CHARS:
            return content[:MAX_FILE_CONTENT_CHARS] + "\n... [truncado]"
        return content
    except Exception as e:
        logger.warning("Failed to fetch content for file %s: %s", file_uuid, e)
        return ""


def _build_canvas_context(oe: OrchestrationEvent) -> str | None:
    """Build canvas + selection context as a trailing system message."""
    design_context = (oe.extra_params or {}).get("design_context", {})
    project_uuid = design_context.get("project_uuid")
    canvas_uuid = design_context.get("canvas_uuid")
    logger.info(
        "Canvas context: project=%s, canvas=%s, session=%s",
        project_uuid, canvas_uuid, oe.orchestration_session_uuid,
    )

    canvases = _fetch_canvases(oe, project_uuid)
    if not canvases:
        logger.info("No canvases found for project=%s session=%s", project_uuid, oe.orchestration_session_uuid)
        return None

    if canvas_uuid:
        canvas = next((c for c in canvases if c.get("uuid") == canvas_uuid), None)
    else:
        canvas = canvases[0]
        canvas_uuid = canvas.get("uuid", "")

    if not canvas:
        return None

    title = canvas.get("title", "Sin titulo")
    node_count = canvas.get("node_count", 0)
    edge_count = canvas.get("edge_count", 0)
    status = canvas.get("status", "unknown")
    lines = [
        "## Estado del Canvas",
        f"Canvas: **{title}** (uuid: {canvas_uuid})",
        f"- Nodos: {node_count}, Conexiones: {edge_count}, Estado: {status}",
    ]

    detail = _fetch_canvas_detail(oe, canvas_uuid)
    if detail:
        detailed_context = _format_canvas_detail(detail)
        if detailed_context:
            lines.append("")
            lines.append(detailed_context)

    project_files = _fetch_and_format_project_files(oe, project_uuid)
    if project_files:
        lines.append(project_files)

    selections = _fetch_selections(oe, canvas_uuid)
    if selections:
        lines.append("")
        lines.append("## Selecciones")
        for s in selections:
            sel_uuid = s.get("uuid", "")
            sel_name = s.get("name", "Sin nombre")
            s_nodes = s.get("node_count", 0)
            s_edges = s.get("edge_count", 0)
            lines.append(
                f"- **{sel_name}** (uuid: {sel_uuid}) - {s_nodes} nodos, {s_edges} conexiones"
            )

    selection_uuid = design_context.get("selection_uuid")
    if selection_uuid:
        detail = _fetch_selection_detail(oe, selection_uuid)
        if detail:
            detail_name = detail.get("name", "Sin nombre")
            element_ids = detail.get("element_ids", {})
            nodes = element_ids.get("nodes", [])
            edges = element_ids.get("edges", [])
            lines.append("")
            lines.append(
                f"## Seleccion Activa: {detail_name} (uuid: {selection_uuid})"
            )
            lines.append(f"Nodos: {nodes}, Conexiones: {edges}")

    return "\n".join(lines)


def _fetch_canvas_detail(oe: OrchestrationEvent, canvas_uuid: str) -> dict | None:
    """Fetch full canvas detail including v2 elements. Returns None on failure."""
    if not canvas_uuid:
        return None
    try:
        from api.canvas_requests import canvas_api_manager

        response = canvas_api_manager.call(
            "get_canvas_detail",
            canvas_uuid=canvas_uuid,
            **_get_api_credentials(oe),
        )
        if not _check_api_response(response, "get_canvas_detail"):
            return None
        return response
    except Exception as e:
        logger.warning("Failed to fetch canvas detail: %s", e)
        return None


def _format_canvas_detail(canvas: dict) -> str:
    """Format lanes, nodes, edges, and dataCatalog from a canvas detail response."""
    elements = canvas.get("elements") or canvas.get("flowchart") or {}
    if not isinstance(elements, dict):
        return ""

    lanes = elements.get("lanes") or elements.get("actors") or []
    nodes = elements.get("nodes") or []
    edges = elements.get("edges") or []
    data_catalog = (
        elements.get("dataCatalog")
        or elements.get("data_catalog")
        or canvas.get("dataCatalog")
        or canvas.get("data_catalog")
        or []
    )

    lines = ["## Detalle del Canvas Actual"]

    if lanes:
        lines.append("### Lanes / Actores")
        for lane in lanes[:20]:
            if not isinstance(lane, dict):
                lines.append(f"- {lane}")
                continue
            lane_id = lane.get("id") or lane.get("uuid") or lane.get("key") or "sin-id"
            label = lane.get("label") or lane.get("name") or lane.get("title") or "Sin nombre"
            lane_type = lane.get("type") or lane.get("kind") or "actor"
            lines.append(f"- {lane_id}: {label} ({lane_type})")

    if nodes:
        lines.append("### Nodos")
        for node in nodes[:40]:
            if not isinstance(node, dict):
                lines.append(f"- {node}")
                continue
            node_id = node.get("id") or node.get("uuid") or "sin-id"
            label = node.get("label") or node.get("title") or node.get("name") or node_id
            node_type = node.get("type") or node.get("kind") or node.get("nodeType") or "node"
            lane_id = node.get("laneId") or node.get("lane_id") or node.get("parentId") or ""
            summary = node.get("summary") or node.get("description") or node.get("data", {}).get("description") or ""
            line = f"- {node_id}: [{node_type}] {label}"
            if lane_id:
                line += f" | lane: {lane_id}"
            if summary:
                line += f" | {str(summary)[:180]}"
            lines.append(line)

    if edges:
        lines.append("### Conexiones")
        for edge in edges[:60]:
            if not isinstance(edge, dict):
                lines.append(f"- {edge}")
                continue
            edge_id = edge.get("id") or edge.get("uuid") or "edge"
            source = edge.get("source") or edge.get("from") or edge.get("sourceId") or "?"
            target = edge.get("target") or edge.get("to") or edge.get("targetId") or "?"
            label = edge.get("label") or edge.get("condition") or ""
            line = f"- {edge_id}: {source} -> {target}"
            if label:
                line += f" ({label})"
            lines.append(line)

    if data_catalog:
        lines.append("### Data Catalog / Credenciales")
        entries = data_catalog.values() if isinstance(data_catalog, dict) else data_catalog
        for entry in list(entries)[:30]:
            if not isinstance(entry, dict):
                lines.append(f"- {entry}")
                continue
            entry_id = entry.get("id") or entry.get("key") or entry.get("name") or "data"
            label = entry.get("label") or entry.get("name") or entry_id
            entry_type = entry.get("type") or entry.get("kind") or "data"
            linked = entry.get("linkedNodeIds") or entry.get("node_ids") or entry.get("nodes") or []
            line = f"- {entry_id}: {label} ({entry_type})"
            if linked:
                line += f" | nodos: {linked}"
            lines.append(line)

    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _fetch_selections(oe: OrchestrationEvent, canvas_uuid: str) -> list:
    """Fetch selections for a canvas. Returns [] on failure."""
    if not canvas_uuid:
        return []
    try:
        from api.canvas_requests import canvas_api_manager

        response = canvas_api_manager.call(
            "list_canvas_selections",
            canvas_uuid=canvas_uuid,
            **_get_api_credentials(oe),
        )
        if not _check_api_response(response, "list_canvas_selections"):
            return []
        return response.get("selections", [])
    except Exception as e:
        logger.warning("Failed to fetch canvas selections: %s", e)
        return []


def _fetch_selection_detail(oe: OrchestrationEvent, selection_uuid: str) -> dict | None:
    """Fetch full selection detail including element_ids. Returns None on failure."""
    if not selection_uuid:
        return None
    try:
        from api.canvas_requests import canvas_api_manager

        response = canvas_api_manager.call(
            "get_selection_detail",
            selection_uuid=selection_uuid,
            **_get_api_credentials(oe),
        )
        if not _check_api_response(response, "get_selection_detail"):
            return None
        return response
    except Exception as e:
        logger.warning("Failed to fetch selection detail: %s", e)
        return None


def _build_user_profile_context(oe: OrchestrationEvent) -> str | None:
    """Build target-user context from extra_params.target_user_uuid."""
    target_user_uuid = (oe.extra_params or {}).get("target_user_uuid")
    if not target_user_uuid:
        logger.info("No target_user_uuid found in extra_params")
        return None

    profile_response = _fetch_target_user_profile(oe, target_user_uuid)

    lines = [
        "## Persona Objetivo de esta Conversacion",
        f"target_user_uuid: {target_user_uuid}",
        "Estas hablando con este miembro de la organizacion. Usa este contexto para personalizar preguntas y no pedir datos ya conocidos.",
    ]

    if profile_response:
        lines.append("### Datos existentes")
        lines.append(_format_profile_payload(profile_response))
    else:
        lines.append("No se pudo obtener el perfil actual. Pregunta rol, area y responsabilidades con tacto y guarda lo que aprendas.")

    return "\n".join(lines)


def _get_users_api_manager():
    """Return a module-level ApiManager for the users endpoint, built once per container."""
    global _users_api_manager
    if _users_api_manager is not None:
        return _users_api_manager

    from chask_foundation.api.api_manager import ApiManager

    base_domain = os.getenv("BASE_DOMAIN")
    if not base_domain:
        return None

    manager = ApiManager(base_url=f"https://{base_domain}/api/v2/users")

    @manager.register("get_user_profile", "get-user-profile", method="GET")
    def get_user_profile(user_uuid: str):
        return {"params": {"user_uuid": user_uuid}}

    _users_api_manager = manager
    return _users_api_manager


def _fetch_target_user_profile(oe: OrchestrationEvent, target_user_uuid: str) -> dict | None:
    """Fetch target user profile via GET /api/v2/users/get-user-profile.

    The endpoint is provided by chask_api (Stream 1, on feat/process-mapping):
    GET /api/v2/users/get-user-profile?user_uuid=<uuid> → ChaskUser + UserProfile.
    Degrades gracefully (returns None) on any error or missing BASE_DOMAIN.
    """
    try:
        users_api_manager = _get_users_api_manager()
        if users_api_manager is None:
            logger.warning("BASE_DOMAIN is not set; cannot fetch target user profile")
            return None

        response = users_api_manager.call(
            "get_user_profile",
            target_user_uuid,
            **_get_api_credentials(oe),
            raise_on_error=False,
        )
        if not _check_api_response(response, "get_user_profile"):
            return None
        return response
    except Exception as e:
        logger.warning("Failed to fetch target user profile: %s", e)
        return None


def _format_profile_payload(payload: dict) -> str:
    """Format a profile response compactly for the LLM."""
    if not payload:
        return "{}"

    sanitized = {
        key: value
        for key, value in payload.items()
        if key not in {"status_code", "access_token", "token"}
    }
    formatted = json.dumps(sanitized, ensure_ascii=False, indent=2, default=str)
    if len(formatted) > 3000:
        return formatted[:3000] + "\n... [truncado]"
    return formatted


def _should_inject_operator_reminder(events: List[Dict[str, Any]]) -> bool:
    """Return True when an operator reminder should be injected.

    Conditions:
    1. At least one operator message exists (message_to_whatsapp_agent).
    2. The last relevant message is from the user (received_whatsapp_message).
    """
    sorted_events = sorted(events, key=lambda x: x.get("created_at", ""))

    has_operator = False
    last_relevant = None

    for evt in sorted_events:
        event_type = evt.get("event_type", "")
        if event_type == "message_to_whatsapp_agent":
            has_operator = True
            last_relevant = "operator"
        elif event_type == "received_whatsapp_message":
            last_relevant = "user"

    return has_operator and last_relevant == "user"


# =============================================================================
# AgentConfig
# =============================================================================

WHATSAPP_CONFIG = AgentConfig(
    source_name="agent",
    request_event_type="received_whatsapp_message",
    response_event_type="response_to_whatsapp_message",
    enabled_event_types={
        "received_whatsapp_message",
        "response_to_whatsapp_message",
        "message_to_whatsapp_agent",
        "function_call",
        "function_call_response",
        "function_call_async_error",
        "analyst_request",
        "analyst_response",
        "context",
        "batch_tool_execution",
        "execute_plan",
    },
    prompt_builder=build_whatsapp_system_prompt,
    trigger_event_types=[
        "need_agent_whatsapp",
        "function_call_response",
        "function_call_async_error",
        "execute_plan",
        "user_authenticated",
        "notify_whatsapp",
        "message_to_whatsapp_agent",
    ],
    socket_name="process_mapping_agent",
    enable_dynamic_tools=True,
    forward_topic="orchestrator",
    default_prompt=(
        "Eres un modelo de lenguaje desarrollado por Chask. "
        "Entrevista miembros por WhatsApp para mapear procesos y completar perfiles."
    ),
)


# =============================================================================
# Custom AgentWrapper
# =============================================================================

class _WhatsAppAgentWrapper(AgentWrapper):
    """AgentWrapper subclass with WhatsApp-specific message preparation.

    Overrides _prepare_messages to:
    - Use WhatsAppEventFormatter instead of the generic AgentEventFormatter
    - Inject operator reminder when applicable
    - Inject special event messages for user_authenticated / notify_whatsapp
    """

    def _build_system_prompt(self) -> str:
        """Build system prompt, applying template variables to socket context.

        When an admin assigns a socket context (via LLM context UI), the base
        AgentWrapper returns it raw — skipping the prompt_builder and leaving
        {bot_name}, {organizacion_name}, etc. unresolved.
        """
        oe = self.orchestration_event

        if self.config.socket_name:
            socket_prompt = self._fetch_socket_context()
            if socket_prompt:
                logger.info("Applying template variables to socket-assigned context")
                data = get_whatsapp_prompt_data(oe)
                return apply_template_variables(socket_prompt, data)

        return build_whatsapp_system_prompt(oe)

    def _call_llm(
        self, messages: List[Dict[str, Any]], force_tool_call: bool = True,
    ) -> Dict[str, Any]:
        """Call the LLM with WhatsApp-specific metadata and optional tool enforcement.

        Args:
            messages: Prepared message list to send to the LLM.
            force_tool_call: When True and tools are available, sets
                tool_choice="required" to ensure the model responds with a
                tool call. Set to False for notify_whatsapp direct responses.
        """
        temperature = 1.0 if self.model.startswith("gpt-5") else 0.7

        extra_kwargs: Dict[str, Any] = {}
        if force_tool_call and self.function_schemas:
            extra_kwargs["tool_choice"] = "required"

        response = self.llm_client.chat(
            messages=messages,
            tools=self.function_schemas if self.function_schemas else None,
            temperature=temperature,
            caller_function="process_mapping_agent.get_response",
            metadata={
                "event_type": self.orchestration_event.event_type,
                "lambda_name": LAMBDA_NAME,
            },
            **extra_kwargs,
        )

        if not response.get("success", True):
            error_msg = response.get("error", "Unknown LLM error")
            raise Exception(f"LLM call failed: {error_msg}")

        return response

    def _is_collecting_pipeline_data(self) -> bool:
        """Check if the session is in collecting_pipeline_data status."""
        try:
            response = orchestrator_api_manager.call(
                "get_active_requirement_for_os",
                orchestration_session_uuid=self.orchestration_event.orchestration_session_uuid,
                access_token=self.orchestration_event.access_token,
                organization_id=self.orchestration_event.organization.organization_id,
            )
            return response.get("session_status") == "collecting_pipeline_data"
        except Exception:
            return False

    def _prepare_messages(self) -> List[Dict[str, Any]]:
        system_prompt = self._build_system_prompt()
        conversation_history = self._build_whatsapp_conversation_history()

        # Operator reminder
        if hasattr(self, "_raw_events") and _should_inject_operator_reminder(self._raw_events):
            conversation_history.append({"role": "system", "content": OPERATOR_REMINDER_TEXT})
            logger.info("Injected operator reminder")

        # Special trigger events
        event_type = self.orchestration_event.event_type
        if event_type in ("user_authenticated", "notify_whatsapp"):
            conversation_history.append(
                {"role": "system", "content": self.orchestration_event.prompt}
            )

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages.extend(conversation_history)

        # Pipeline collection reminder (appended as last message)
        if self._is_collecting_pipeline_data():
            reminder = {"role": "system", "content": PIPELINE_COLLECTION_REMINDER}
            messages.append(reminder)
            logger.info("Injected pipeline collection reminder")

        canvas_context = _build_canvas_context(self.orchestration_event)
        if canvas_context:
            logger.info("Appending canvas context system message (%d chars)", len(canvas_context))
            messages.append({"role": "system", "content": canvas_context})

        user_profile_context = _build_user_profile_context(self.orchestration_event)
        if user_profile_context:
            logger.info("Appending user profile context system message (%d chars)", len(user_profile_context))
            messages.append({"role": "system", "content": user_profile_context})

        return messages

    def _build_whatsapp_conversation_history(self) -> List[Dict[str, Any]]:
        """Fetch events and format with WhatsAppEventFormatter."""
        try:
            response = orchestrator_api_manager.call(
                "get_orchestration_events",
                orchestration_session_id=self.orchestration_event.orchestration_session_uuid,
                access_token=self.orchestration_event.access_token,
                organization_id=self.orchestration_event.organization.organization_id,
            )

            orchestration_events = response.get("orchestration_events", [])
            logger.info(f"Retrieved {len(orchestration_events)} orchestration events")

            # Store raw events for operator reminder check
            self._raw_events = orchestration_events

            # Build channel map
            channel_map: Dict[str, Any] = {}
            if self.orchestration_event.channel_id:
                channel_map[self.orchestration_event.channel_id] = (0, "whatsapp")

            relevant = [
                evt for evt in orchestration_events
                if evt.get("event_type") in WHATSAPP_DEFAULT_EVENTS
            ]

            return WhatsAppEventFormatter.format_events(
                relevant,
                channel_map=channel_map,
                enabled_events=WHATSAPP_DEFAULT_EVENTS,
            )

        except Exception as e:
            logger.error(f"Failed to build WhatsApp conversation history: {e}")
            return []


# =============================================================================
# FunctionBackend
# =============================================================================

class FunctionBackend(AgentFunctionBackend):
    """Process mapping WhatsApp agent backend.

    Preserves the handler.py contract:
        FunctionBackend(oe, key, model).process_request()
    """

    def __init__(
        self,
        orchestration_event: OrchestrationEvent,
        openai_api_key: str,
        model: str,
    ):
        model = model or "gpt-5.1-2025-11-13"
        super().__init__(
            config=WHATSAPP_CONFIG,
            orchestration_event=orchestration_event,
            openai_api_key=openai_api_key,
            model=model,
        )

    def _handle_agent_request(self) -> str:
        """Use _WhatsAppAgentWrapper for WhatsApp-specific message preparation.

        WhatsApp-specific flow:
        1. Get initial LLM response with tool_choice="required"
        2. If tool call -> invoke tool, return "requested_orchestrator_assistance"
        3. If no tool call (edge case) -> re-invoke via Kafka with explicit
           instruction to use a tool
        """
        agent = None
        try:
            agent = _WhatsAppAgentWrapper(
                config=self.config,
                orchestration_event=self.orchestration_event,
                openai_api_key=self.openai_api_key,
                model=self.model,
            )

            # Handle notify_whatsapp specially - direct response, no tools
            if self.orchestration_event.event_type == "notify_whatsapp":
                return self._handle_notify_whatsapp(agent)

            response_message = agent.get_response()

            if response_message == "requested_orchestrator_assistance":
                self.response_event_sent = True
                return response_message

            # Safety net: tool_choice="required" should prevent this, but if it
            # happens, re-invoke the whatsapp agent so it tries again with an
            # explicit instruction to use a tool.
            logger.warning(
                "LLM returned no tool calls despite tool_choice=required — re-invoking"
            )
            self._re_invoke_whatsapp_agent()
            return response_message
        finally:
            if agent:
                agent.shutdown()

    def _handle_notify_whatsapp(self, agent: _WhatsAppAgentWrapper) -> str:
        """Handle notify_whatsapp events with a direct LLM response (no tools)."""
        messages = agent._prepare_messages()

        response = agent._call_llm(messages, force_tool_call=False)
        content = response.get("content", "")

        if content:
            self._send_whatsapp_response(content)

        return content

    def _re_invoke_whatsapp_agent(self) -> None:
        """Re-invoke the whatsapp agent when the LLM fails to produce a tool call.

        Emits an execute_plan event with source=agent so the orchestrator
        re-targets the whatsapp agent. The prompt instructs the LLM to always
        respond with a tool call. Failures in the API calls are not raised —
        the current invocation already returned without a tool call, so this
        is a best-effort recovery step.
        """
        oe = self.orchestration_event
        extra_params = {"original_source": "agent"}
        re_invoke_prompt = (
            "DEBES responder con una llamada a herramienta. "
            "Analiza la conversación y ejecuta la herramienta apropiada. "
            "Si necesitas enviar un mensaje de WhatsApp, usa la herramienta correspondiente."
        )

        try:
            evolve_response = orchestrator_api_manager.call(
                "evolve_event",
                parent_event_uuid=str(oe.event_id),
                event_type="execute_plan",
                source="agent",
                target="orchestrator",
                prompt=re_invoke_prompt,
                extra_params=extra_params,
                access_token=oe.access_token,
                organization_id=oe.organization.organization_id,
            )

            re_invoke_event = oe.model_copy(deep=True)
            re_invoke_event.event_type = "execute_plan"
            re_invoke_event.source = "agent"
            re_invoke_event.target = "orchestrator"
            re_invoke_event.prompt = re_invoke_prompt
            re_invoke_event.event_id = evolve_response.get("uuid", oe.event_id)
            re_invoke_event.extra_params = evolve_response.get("extra_params", extra_params)

            orchestrator_api_manager.call(
                "forward_oe_to_kafka",
                orchestration_event=re_invoke_event.model_dump(),
                topic="orchestrator",
                access_token=oe.access_token,
                organization_id=oe.organization.organization_id,
            )
            logger.info(
                "Emitted execute_plan to re-invoke whatsapp agent with tool requirement"
            )
        except Exception as e:
            logger.error(f"Failed to re-invoke whatsapp agent: {e}")

    def _send_whatsapp_response(self, response_message: str) -> None:
        """Send response as response_to_whatsapp_message with phone numbers."""
        if self.response_event_sent:
            logger.warning("[DUPLICATE_GUARD] Response already sent, skipping")
            return

        oe = self.orchestration_event
        conversation_uuid = oe.channel_id
        if not conversation_uuid:
            raise ValueError("Missing channel_id (conversation_uuid)")

        extra_params = self._get_phone_numbers(oe, conversation_uuid)
        evolved_uuid = self._evolve_response_event(oe, response_message, extra_params)
        self._forward_to_kafka(oe, evolved_uuid, response_message, extra_params)

        self.response_event_sent = True
        logger.info(f"WhatsApp response sent [evolved from {oe.event_id} -> {evolved_uuid}]")

    def _get_phone_numbers(self, oe: OrchestrationEvent, conversation_uuid: str) -> Dict[str, Any]:
        """Get user and agent phone numbers from extra_params or API."""
        original_extra = oe.extra_params or {}
        user_phone = original_extra.get("user_phone_number")
        agent_phone = original_extra.get("agent_phone_number")

        if user_phone and agent_phone:
            return {
                "user_phone_number": user_phone,
                "agent_phone_number": agent_phone,
                "original_source": "agent",
            }

        phone_data = internal_whatsapp_api_manager.call(
            "get_phone_from_conversation",
            conversation_uuid=conversation_uuid,
            access_token=oe.access_token,
            organization_id=oe.organization.organization_id,
        )

        if not user_phone:
            user_phone = phone_data.get("phone_number")
            if not user_phone:
                raise ValueError("API returned no phone_number")

        if not agent_phone:
            agent_phone = phone_data.get("phone_number_id")
            if not agent_phone:
                raise ValueError("API returned no phone_number_id")

        return {
            "user_phone_number": user_phone,
            "agent_phone_number": agent_phone,
            "original_source": "agent",
        }

    def _evolve_response_event(
        self, oe: OrchestrationEvent, response_message: str, extra_params: Dict[str, Any]
    ) -> str:
        """Evolve the orchestration event and return the new UUID."""
        evolve_response = orchestrator_api_manager.call(
            "evolve_event",
            parent_event_uuid=str(oe.event_id),
            event_type="response_to_whatsapp_message",
            source="agent",
            target="whatsapp",
            prompt=response_message,
            extra_params=extra_params,
            access_token=oe.access_token,
            organization_id=oe.organization.organization_id,
        )

        status_code = evolve_response.get("status_code")
        if status_code and status_code not in (200, 201):
            raise Exception(f"Failed to evolve event: {evolve_response.get('error', 'Unknown')}")

        evolved_uuid = evolve_response.get("uuid")
        if not evolved_uuid:
            raise Exception("API response missing uuid for evolved event")

        return evolved_uuid

    def _forward_to_kafka(
        self, oe: OrchestrationEvent, evolved_uuid: str, response_message: str, extra_params: Dict[str, Any]
    ) -> None:
        """Forward the response event to Kafka."""
        response_event = oe.model_copy(deep=True)
        response_event.event_id = evolved_uuid
        response_event.event_type = "response_to_whatsapp_message"
        response_event.source = "agent"
        response_event.target = "whatsapp"
        response_event.prompt = response_message
        response_event.extra_params = extra_params
        response_event.extra_params["_already_persisted"] = True

        orchestrator_api_manager.call(
            "forward_oe_to_kafka",
            orchestration_event=response_event.model_dump(),
            topic="orchestrator",
            access_token=response_event.access_token,
            organization_id=response_event.organization.organization_id,
        )
