from agents.specialists.docs import DocsAgent
from agents.specialists.quality import QualityAgent
from agents.specialists.security import SecurityAgent
from agents.specialists.tests import TestsAgent

ALL_SPECIALISTS = (SecurityAgent, QualityAgent, TestsAgent, DocsAgent)

__all__ = ["SecurityAgent", "QualityAgent", "TestsAgent", "DocsAgent", "ALL_SPECIALISTS"]
