from __future__ import annotations

from agents.base_agent import SpecialistAgent
from agents.contracts import AgentType
from prompts.loader import load_prompt


class TestsAgent(SpecialistAgent):
    __test__ = False  # not a pytest test case — name just starts with "Test"

    agent_type = AgentType.TESTS
    system_prompt = load_prompt("tests")
