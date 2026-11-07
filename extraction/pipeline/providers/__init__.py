from pipeline.config import ModelSpec, RetrySettings
from pipeline.providers.base import ExtractionError, PageExtraction, Provider
from pipeline.providers.claude import ClaudeProvider


def build_provider(model: ModelSpec, retry: RetrySettings) -> Provider:
    if model.provider == "claude":
        return ClaudeProvider(model, retry)
    raise ValueError(f"Unsupported provider '{model.provider}' for model '{model.name}'")


__all__ = ["ExtractionError", "PageExtraction", "Provider", "build_provider"]
