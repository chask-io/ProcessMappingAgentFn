"""
ProcessMappingAgentFn - Business Logic

Agent to interview organization members, refine a process canvas, and enrich
the target user's profile. Uses the generic AgentFunctionBackend with
process-mapping-specific context injection.
"""

import json
import logging
import os
from typing import Any

from chask_foundation.backend.agent_wrapper import AgentConfig, AgentFunctionBackend, AgentWrapper
from chask_foundation.backend.models import OrchestrationEvent

logger = logging.getLogger()

MAX_FILE_CONTENT_CHARS = 3000
TEXT_MIME_PREFIXES = ("text/", "application/json", "application/xml")


def _apply_template_variables(template: str, data: dict) -> str:
    """Replace known template variables using str.replace().

    Safer than str.format() because unknown {variable} placeholders
    (e.g. from admin-configured LLM contexts) are left untouched
    instead of raising KeyError.
    """
    for key, value in data.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


def _build_system_prompt(oe: OrchestrationEvent) -> str:
    """Build system prompt from prompt file with format kwargs."""
    try:
        with open("backend/prompts/process_mapping_prompt.txt", "r") as f:
            template = f.read()
        kwargs = _build_format_kwargs(oe)
        return _apply_template_variables(template, kwargs)
    except Exception as e:
        logger.warning("Failed to load prompt file: %s", e)
        return "Eres un agente experto en mapear procesos de negocio y completar perfiles de usuarios."


def _build_format_kwargs(oe: OrchestrationEvent) -> dict:
    """Build prompt template kwargs including project context."""
    design_context = (oe.extra_params or {}).get("design_context", {})
    project_uuid = design_context.get("project_uuid")

    return {
        "organization_name": oe.organization.organization_name,
        "agent_alias": (
            oe.target_agent.agent_alias
            if oe.target_agent
            else "Agente de Mapeo de Procesos"
        ),
        "project_files": _fetch_and_format_project_files(oe, project_uuid),
    }


def _get_api_credentials(oe: OrchestrationEvent) -> dict:
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

        # Explicit project UUID (from design_context)
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

        # Fallback: derive project from orchestration session
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


def _fetch_and_format_project_files(oe: OrchestrationEvent, project_uuid: str) -> str:
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
        # Strip null bytes — raw binary content can't be stored in Postgres text fields
        content = content.replace("\x00", "")
        if len(content) > MAX_FILE_CONTENT_CHARS:
            return content[:MAX_FILE_CONTENT_CHARS] + "\n... [truncado]"
        return content
    except Exception as e:
        logger.warning("Failed to fetch content for file %s: %s", file_uuid, e)
        return ""


def _build_canvas_context(oe: OrchestrationEvent) -> str | None:
    """Build canvas + selection context as a trailing system message.

    Returns a formatted markdown string, or None if no canvas exists.
    Uses design_context from extra_params if available, otherwise falls back
    to deriving the project from the orchestration session.
    """
    design_context = (oe.extra_params or {}).get("design_context", {})
    project_uuid = design_context.get("project_uuid")
    canvas_uuid = design_context.get("canvas_uuid")
    logger.info(
        "Canvas context: project=%s, canvas=%s, session=%s",
        project_uuid, canvas_uuid, oe.orchestration_session_uuid,
    )

    # Fetch canvases (tries project_uuid first, then session fallback)
    canvases = _fetch_canvases(oe, project_uuid)
    if not canvases:
        logger.info("No canvases found for project=%s session=%s", project_uuid, oe.orchestration_session_uuid)
        return None

    # Resolve canvas: explicit UUID from frontend, or latest from project
    if canvas_uuid:
        canvas = next((c for c in canvases if c.get("uuid") == canvas_uuid), None)
    else:
        canvas = canvases[0]
        canvas_uuid = canvas.get("uuid", "")

    if not canvas:
        return None

    # Canvas status
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

    # Selections
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
                f"- **{sel_name}** (uuid: {sel_uuid}) — {s_nodes} nodos, {s_edges} conexiones"
            )

    # Active selection detail
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


def _fetch_target_user_profile(oe: OrchestrationEvent, target_user_uuid: str) -> dict | None:
    """Fetch ChaskUser + UserProfile via the foundation ApiManager pattern."""
    try:
        from chask_foundation.api.api_manager import ApiManager

        base_domain = os.getenv("BASE_DOMAIN")
        if not base_domain:
            logger.warning("BASE_DOMAIN is not set; cannot fetch target user profile")
            return None

        users_api_manager = ApiManager(base_url=f"https://{base_domain}/api/v2/users")

        @users_api_manager.register("get_user_profile", "get-user-profile", method="GET")
        def get_user_profile(user_uuid: str):
            return {
                "params": {
                    "user_uuid": user_uuid,
                    "target_user_uuid": user_uuid,
                }
            }

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


PROCESS_MAPPING_CONFIG = AgentConfig(
    source_name="process_mapping",
    request_event_type="process_mapping_request",
    response_event_type="process_mapping_response",
    enabled_event_types={
        "process_mapping_request",
        "process_mapping_response",
        "function_call",
        "function_call_response",
        "context",
    },
    prompt_builder=_build_system_prompt,
    socket_name="process_mapping_agent",
    enable_dynamic_tools=True,
    default_prompt="Eres un agente experto en mapear procesos de negocio y completar perfiles de usuarios.",
)


class _ProcessMappingAgentWrapper(AgentWrapper):
    """AgentWrapper subclass that appends canvas and user profile context."""

    def _build_system_prompt(self) -> str:
        """Build system prompt, applying template variables to socket context.

        When an admin assigns a socket context (via LLM context UI), the base
        AgentWrapper returns it raw — skipping the prompt_builder and leaving
        {organization_name}, {agent_alias}, etc. unresolved.
        """
        oe = self.orchestration_event

        if self.config.socket_name:
            socket_prompt = self._fetch_socket_context()
            if socket_prompt:
                logger.info("Applying template variables to socket-assigned context")
                kwargs = _build_format_kwargs(oe)
                return _apply_template_variables(socket_prompt, kwargs)

        return _build_system_prompt(oe)

    def _prepare_messages(self) -> list[dict[str, Any]]:
        messages = super()._prepare_messages()
        canvas_context = _build_canvas_context(self.orchestration_event)
        if canvas_context:
            logger.info("Appending canvas context system message (%d chars)", len(canvas_context))
            messages.append({"role": "system", "content": canvas_context})
        user_profile_context = _build_user_profile_context(self.orchestration_event)
        if user_profile_context:
            logger.info("Appending user profile context system message (%d chars)", len(user_profile_context))
            messages.append({"role": "system", "content": user_profile_context})
        return messages


class FunctionBackend(AgentFunctionBackend):
    """Process mapping agent backend.

    Preserves the handler.py contract:
        FunctionBackend(oe, key, model).process_request()
    """

    def __init__(
        self,
        orchestration_event: OrchestrationEvent,
        openai_api_key: str,
        model: str,
    ):
        super().__init__(
            config=PROCESS_MAPPING_CONFIG,
            orchestration_event=orchestration_event,
            openai_api_key=openai_api_key,
            model=model,
        )

    def _handle_agent_request(self) -> str:
        """Use _ProcessMappingAgentWrapper to inject conversation context."""
        agent = None
        try:
            agent = _ProcessMappingAgentWrapper(
                config=self.config,
                orchestration_event=self.orchestration_event,
                openai_api_key=self.openai_api_key,
                model=self.model,
            )
            response_message = agent.get_response()
            if response_message == "requested_orchestrator_assistance":
                self.response_event_sent = True
                return response_message
            if response_message:
                self._send_response(response_message)
            return response_message
        finally:
            if agent:
                agent.shutdown()
