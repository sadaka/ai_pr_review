from __future__ import annotations

from agents.base_agent import SpecialistAgent
from agents.contracts import AgentType
from prompts.loader import load_prompt


class DocsAgent(SpecialistAgent):
    agent_type = AgentType.DOCS
    system_prompt = load_prompt("docs")
