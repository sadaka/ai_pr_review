from __future__ import annotations

from agents.base_agent import SpecialistAgent
from agents.contracts import AgentType
from prompts import registry

_PROMPT = registry.get("tests")


class TestsAgent(SpecialistAgent):
    __test__ = False  # not a pytest test case — name just starts with "Test"

    agent_type = AgentType.TESTS
    system_prompt = _PROMPT.text
    prompt_version = _PROMPT.version
