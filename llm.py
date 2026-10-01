"""LLMs for ADK agents, via LiteLLM.

`databricks_model()` is what the app uses. `litellm_model()` is an alternative:
swap it in where an agent is built (agent.py, tuner.py) to run on any model
served by a LiteLLM proxy instead.
"""
import os

from google.adk.models.lite_llm import LiteLlm


def databricks_model() -> LiteLlm:
    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    token = os.environ.get("DATABRICKS_TOKEN", "")
    if not host or not token:
        raise RuntimeError("Set DATABRICKS_HOST and DATABRICKS_TOKEN (see .env.example).")
    endpoint = os.environ.get("DATABRICKS_MODEL", "databricks-claude-sonnet-4-6")
    return LiteLlm(
        model=f"databricks/{endpoint}",
        api_base=f"{host}/serving-endpoints",
        api_key=token,
    )


def litellm_model() -> LiteLlm:
    """A model served by a LiteLLM proxy, by its public model name on the proxy."""
    key = os.environ.get("LITELLM_API_KEY", "")
    if not key:
        raise RuntimeError("Set LITELLM_API_KEY (see .env.example).")
    return LiteLlm(
        model=f"litellm_proxy/{os.environ.get('LITELLM_MODEL', 'claude-sonnet-5')}",
        api_base=os.environ.get("LITELLM_API_BASE", "http://127.0.0.1:4000"),
        api_key=key,
    )
