from __future__ import annotations

from agents.base_agent import SpecialistAgent
from agents.contracts import AgentType
from prompts.loader import load_prompt


class SecurityAgent(SpecialistAgent):
    agent_type = AgentType.SECURITY
    system_prompt = load_prompt("security")
