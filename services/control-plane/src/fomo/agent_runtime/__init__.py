"""Structured four-role SOP runtime."""

from .llm import ModelClient, OpenAICompatibleClient
from .metagpt_adapter import MetaGPTAdapter, MetaGPTUnavailable, prepare_metagpt_runtime
from .sop import SOPRunner

__all__ = [
    "MetaGPTAdapter",
    "MetaGPTUnavailable",
    "ModelClient",
    "OpenAICompatibleClient",
    "SOPRunner",
    "prepare_metagpt_runtime",
]
