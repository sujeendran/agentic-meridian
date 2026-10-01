"""Tuner agent: proposes new ROI priors for the auto-tune loop.

It is a tool-less ADK agent with a structured output schema, called once per
loop iteration with the full run history (the Meridian port of webapp/agent.py).
"""
import json
import uuid

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

from llm import litellm_model

INSTRUCTION = """\
You tune ROI priors for a Google Meridian marketing mix model.

Each paid channel has a LogNormal ROI prior given by its mean and sd (plain ROI
units: 1.0 = one unit of revenue per unit of spend). You receive the full
history of priors tried so far and the metrics each produced:
- r2: out-of-sample R-squared on randomly held-out geo-weeks (higher is better)
- max_rhat: MCMC convergence (must stay <= its target)
- shares / gap: each channel's share of media contribution vs benchmark shares,
  gap = mean absolute difference (lower is better; absent without a benchmark)

Rules:
- Learn from the history; never repeat an adjustment that made things worse.
- A metric that already meets its target is DONE. Focus on failing metrics and
  never trade away a passing metric's margin.
- A channel whose share is below its benchmark needs a higher ROI mean, and vice
  versa. Move means in proportion to the share gap.
- sd is capped at its starting value: you may tighten sd when confident, but
  loosening is silently clamped, so move the mean instead.
- Respect every note in user_guidance; it comes from the analyst.
Return priors for every channel and a one or two sentence rationale.
"""


# Plain JSON schema, fully inlined: Databricks' Gemini endpoints reject the
# $ref/$defs that nested Pydantic models generate.
PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "priors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "mean": {"type": "number"},
                    "sd": {"type": "number"},
                },
                "required": ["channel", "mean", "sd"],
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["priors", "rationale"],
}


def _tuner_agent() -> Agent:
    return Agent(
        name="prior_tuner",
        model=litellm_model(),
        instruction=INSTRUCTION,
        output_schema=PROPOSAL_SCHEMA,
    )


async def propose_priors(context: dict) -> tuple[dict, str]:
    """context: channels, current priors, history, targets, benchmark, guidance."""
    runner = InMemoryRunner(agent=_tuner_agent(), app_name="prior_tuner")
    session = await runner.session_service.create_session(
        app_name="prior_tuner", user_id="tuner", session_id=uuid.uuid4().hex
    )
    message = types.Content(role="user", parts=[types.Part(text=json.dumps(context))])
    text = ""
    async for event in runner.run_async(
        user_id="tuner", session_id=session.id, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(p.text or "" for p in event.content.parts if not p.thought)
    proposal = json.loads(text)
    priors = {p["channel"]: {"mean": p["mean"], "sd": p["sd"]} for p in proposal["priors"]}
    return priors, proposal["rationale"]
