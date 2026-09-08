"""Cross-cutting security middleware: prompt-injection defense for untrusted
content (PR diff text, retrieved code chunks) before it reaches a specialist
LLM. See `threat_model.md` for the model this mitigates.
"""
