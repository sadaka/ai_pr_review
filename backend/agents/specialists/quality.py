from __future__ import annotations

from agents.base_agent import SpecialistAgent
from agents.contracts import AgentType
from prompts import registry

_PROMPT = registry.get("quality")


class QualityAgent(SpecialistAgent):
    agent_type = AgentType.QUALITY
    system_prompt = _PROMPT.text
    prompt_version = _PROMPT.version
