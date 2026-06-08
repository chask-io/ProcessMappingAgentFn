"""
Process Mapping Agent - System Prompt Builder

Builds the system prompt for the process mapping agent by fetching channel
context, user validation data, and active requirements from the Chask APIs.
Returns a plain string (no LangChain dependency).
"""

import os
import logging
from typing import Dict, Any

from chask_foundation.backend.models import OrchestrationEvent

logger = logging.getLogger(__name__)

PROMPT_FILE_PATH = "backend/prompts/process_mapping_prompt.txt"


def _load_prompt_template() -> str:
    """Load process mapping agent prompt template from file."""
    prompt_path = os.path.join(os.getcwd(), PROMPT_FILE_PATH)

    if not os.path.exists(prompt_path):
        prompt_path = os.path.join(
            os.path.dirname(__file__), "prompts", "process_mapping_prompt.txt"
        )

    with open(prompt_path, "r", encoding="utf-8") as fh:
        return fh.read()


def _get_api_credentials(oe: OrchestrationEvent) -> Dict[str, str]:
    """Return common API call kwargs."""
    return {
        "access_token": oe.access_token,
        "organization_id": str(oe.organization.organization_id),
    }


def _fetch_organization_context(oe: OrchestrationEvent) -> str:
    """Fetch organization context description for the channel."""
    if not oe.channel_id:
        return ""

    from api.agent_requests import agent_api_manager

    creds = _get_api_credentials(oe)
    try:
        context = agent_api_manager.call(
            "get-context-by-channel", channel_id=oe.channel_id, **creds,
        )
        return context.get("context", "")
    except Exception as e:
        logger.warning("Failed to fetch channel context: %s", e)
        return ""


def _fetch_user_data(oe: OrchestrationEvent) -> Dict[str, Any]:
    """Fetch user validation data from orchestrator."""
    from api.orchestrator_requests import orchestrator_api_manager

    creds = _get_api_credentials(oe)
    return orchestrator_api_manager.call(
        "get_orchestration_session_user_data",
        orchestration_session_uuid=oe.orchestration_session_uuid,
        internal_orchestration_session_uuid=oe.internal_orchestration_session_uuid,
        **creds,
    )


def _fetch_active_requirements(oe: OrchestrationEvent) -> str:
    """Fetch and format active requirements for the session."""
    from api.orchestrator_requests import orchestrator_api_manager

    creds = _get_api_credentials(oe)
    response = orchestrator_api_manager.call(
        "get_active_requirement_for_os",
        orchestration_session_uuid=oe.orchestration_session_uuid,
        **creds,
    )

    if not response or not isinstance(response, dict):
        return "None"

    active_pipeline = response.get("active_pipeline")
    if not active_pipeline:
        return "None"

    pipeline_id = active_pipeline.get("id", "N/A")
    title = active_pipeline.get("title", "N/A")
    description = active_pipeline.get("description", "N/A")
    status = active_pipeline.get("status", "N/A")

    return (
        f"- ID: {pipeline_id} | Estado: {status}\n"
        f"  Título: {title}\n"
        f"  Descripción: {description}"
    )


def apply_template_variables(template: str, data: Dict[str, Any]) -> str:
    """Replace known template variables using str.replace().

    Safer than str.format() because unknown {variable} placeholders
    (e.g. from admin-configured LLM contexts) are left untouched
    instead of raising KeyError.
    """
    for key, value in data.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


def get_whatsapp_prompt_data(oe: OrchestrationEvent) -> Dict[str, Any]:
    """Fetch all dynamic data for the process mapping prompt template.

    Returns a dict of template variable name -> value.
    """
    org_context = _fetch_organization_context(oe)
    user_data = _fetch_user_data(oe)
    client_requirements = _fetch_active_requirements(oe)

    validate_user = (
        "Hay que validar al usuario"
        if user_data.get("user_data") == "No hay datos del usuario"
        else "USUARIO VALIDADO"
    )

    bot_name = (
        oe.target_agent.agent_alias if oe.target_agent else "Agente de Mapeo de Procesos"
    )

    return {
        "bot_name": bot_name,
        "organizacion_name": oe.organization.organization_name,
        "organizacion_description": org_context,
        "client_requirements": client_requirements,
        "validate_user": validate_user,
        "user_data": user_data.get("user_data", "No hay datos del usuario"),
        "factual_summary": "None",
    }


def build_whatsapp_system_prompt(oe: OrchestrationEvent) -> str:
    """Build the complete system prompt for the process mapping agent.

    This is the prompt_builder callable used by AgentConfig.

    Args:
        oe: The orchestration event with all request context.

    Returns:
        Formatted system prompt string.
    """
    template = _load_prompt_template()
    data = get_whatsapp_prompt_data(oe)
    return apply_template_variables(template, data)
