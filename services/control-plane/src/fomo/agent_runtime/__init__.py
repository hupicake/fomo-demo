"""Structured four-role SOP runtime."""

from .llm import ModelClient, OpenAICompatibleClient
from .sop import SOPRunner

__all__ = [
    "ModelClient",
    "OpenAICompatibleClient",
    "SOPRunner",
]
