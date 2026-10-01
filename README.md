# Agentic Meridian

A chat-first marketing mix modeling app. You talk to an ADK agent, served by
LiteLLM, while it builds, diagnoses and tunes a
[Google Meridian](https://github.com/google/meridian) model. Fits run in the
background, so you can keep asking questions, change priors or steer the tuner
while MCMC is sampling.

Regression is run by Meridian's Bayesian model with a 
conversational co-pilot plus an auto-tune loop you can interrupt and steer.

## Run

```bash
uv venv --python 3.12
source .venv/bin/activate               # GPU: see .vscode/launch.json
uv pip install -r requirements.txt      # google-adk, litellm, python-multipart
cp .env.example .env                    # fill in LITELLM URL / TOKEN / MODEL
uvicorn server:app --host 127.0.0.1 --port 8081
```

Open http://127.0.0.1:8081. The app starts on Meridian's simulated sample
dataset (40 geos × 156 weeks, 5 channels), which is checked in at
`data/geo_all_channels.csv`. If the file is missing, it is downloaded again from GitHub.

> **Model endpoint:** use a Claude endpoint, e.g. `claude-sonnet-5`.

## What you can do

| Ask the co-pilot to… | What happens |
|---|---|
| "fit the model" | A background MCMC fit starts. The dashboard updates when it finishes, and the co-pilot summarizes the result. |
| "set Channel2's ROI prior to 1.5 ± 0.5" | The prior is updated. You can also edit priors on the *Priors & settings* tab. |
| "benchmarks are 25/15/10/30/20 — auto-tune" | The loop fits, checks the targets, and lets the tuner move the ROI priors, then repeats. |
| "keep Channel4 below 3" (while tuning) | The guidance is passed to the tuner from its next iteration. |
| "stop" | The job stops once the current fit finishes. A running MCMC fit can't be interrupted. |
| "generate reports" | Meridian's model summary and health card appear in the *Reports* tab. |
| "optimize the budget" | A fixed-budget optimization runs (±30% per channel), shown in the *Budget* tab. |
| "save this as baseline" / "load baseline" | The model is saved to or loaded from `models/<name>.binpb`. |
| upload a CSV (⤒) | The co-pilot inspects the columns, proposes a mapping, and builds the dataset once you confirm. |

## How it works

```
Browser ──POST /api/chat──▶ server.py ──▶ ADK Runner(root_agent) ──tools──▶ Workspace ──▶ modeling.py
   ▲                                          │                                 │           (Meridian)
   └──────── GET /api/events (SSE) ◀──────────┴──────── event bus ◀─────────────┘
```

- **One event stream per session.**
  - `POST /api/chat` only queues the message.
  - Everything reaches the browser over the session's SSE stream: streamed agent text, tool calls, job progress and state snapshots.
  - Agent turns started by you and turns started when a job finishes render the same way.
- **Background jobs.**
  - Fits, auto-tune, reports, optimization and loading run as asyncio tasks, with Meridian in a worker thread.
  - Only one job runs at a time, because there is one GPU.
  - When a job finishes, the server sends the agent a `[system]` message so it can explain the result.
- **Live state in the prompt.** The co-pilot's instruction is rebuilt every turn from the session's Workspace. It stays correct when priors change from the dashboard or a job finishes in the background.
- **Auto-tune** is the Meridian port of `webapp/agent.py`.
  - A tool-less *tuner* agent with a JSON output schema reads the full fit history, the targets and your guidance, then proposes new ROI priors.
  - Prior sd can only tighten, never loosen. The tuner has to move means, not flatten the priors.

| File | Purpose |
|---|---|
| `modeling.py` | Every Meridian call: data → ModelSpec → fit → metrics, charts, reports, optimization, save/load |
| `workspace.py` | Per-session state, background jobs, auto-tune loop, event bus |
| `tools.py` | The co-pilot's tools (thin wrappers around Workspace) |
| `agent.py` | `root_agent`: the co-pilot and its dynamic instruction |
| `tuner.py` | The prior-tuning agent used by auto-tune |
| `llm.py` | LiteLLM model pointed at the Databricks serving endpoint |
| `server.py` | FastAPI routes and SSE streaming |
| `static/` | Single-page UI (chat + dashboard; charts are Meridian's own Altair specs rendered with vega-embed) |

### Modeling choices

- **Priors.**
  - Paid channels get LogNormal ROI priors, specified as mean and sd in ROI units.
  - The default is Meridian's own (mean 1.8, sd 2.2).
- **Holdout.** A random, seeded 20% of geo-weeks is held out, and fit quality is out-of-sample R²/MAPE at geo level.
  - It is not the last weeks. By default Meridian gives each week its own baseline knot, so weeks missing from every geo can't be predicted.
- **Baseline knots.** About one per month for geo data (39 for the sample data) instead of Meridian's default of one per week. With weekly knots and short chains, the baseline never converged (R-hat above 7).
- **Sampling.**
  - `quick` (4 chains × 500 draws) is for iterating and auto-tune. It takes about 4 minutes on the RTX laptop GPU and converges on the sample data (max R-hat 1.17, holdout R² 0.74).
  - `full` (4 chains × 1000 draws) is for the final model.
- **Auto-tune targets.** Defaults are holdout R² ≥ 0.7, benchmark gap ≤ 3% and max R-hat ≤ 1.2. You can change them in chat.
- **Backend.** Meridian runs on the TensorFlow backend, which measured about 2× faster than JAX on this GPU.

## Phase 2 (planned): agentic budget optimization

A `budget_planner` sub-agent that:
- turns goals ("grow revenue 10% at flat spend", "cap Channel3 at +20%") into `BudgetOptimizer` runs (fixed or flexible budget, target ROI/mROI, per-channel bounds);
- iterates scenarios and compares them side by side.

`optimize_budget` already stores every result as a scenario in the Workspace, and the Budget tab lists them. Phase 2 adds the sub-agent, the constraint parameters and a comparison view.
