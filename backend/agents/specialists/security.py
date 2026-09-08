from __future__ import annotations

from agents.base_agent import SpecialistAgent
from agents.contracts import AgentType
from prompts import registry

_PROMPT = registry.get("security")


class SecurityAgent(SpecialistAgent):
    agent_type = AgentType.SECURITY
    system_prompt = _PROMPT.text
    prompt_version = _PROMPT.version
