from __future__ import annotations

from agents.base_agent import SpecialistAgent
from agents.contracts import AgentType
from prompts import registry

_PROMPT = registry.get("docs")


class DocsAgent(SpecialistAgent):
    agent_type = AgentType.DOCS
    system_prompt = _PROMPT.text
    prompt_version = _PROMPT.version
