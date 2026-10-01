"""Per-session modeling state, background jobs and the UI event bus.

A Workspace is what both the agent's tools and the dashboard act on. Slow work
(MCMC fits, reports, optimization) runs as a background job so the chat stays
responsive; when a job ends, `notify` hands a [system] message to the agent.
"""
import asyncio
import copy
import json
import time
from dataclasses import dataclass, field

import modeling
import tuner

GPU = asyncio.Semaphore(1)  # one Meridian job at a time on the single GPU
DEFAULT_TARGETS = {"min_r2": 0.7, "max_gap": 0.03, "max_rhat": 1.2}


@dataclass
class Run:
    id: int
    source: str  # "manual", "autotune" or "loaded"
    priors: dict
    settings: dict
    metrics: dict
    seconds: float
    rationale: str = ""
    model: object = field(default=None, repr=False)
    charts: dict = field(default_factory=dict, repr=False)

    def summary(self) -> dict:
        return {k: getattr(self, k) for k in
                ("id", "source", "priors", "settings", "metrics", "seconds", "rationale")}


def metrics_line(m: dict) -> str:
    parts = [f"R²={m['r2']:.3f}", f"MAPE={m['mape']:.1%}", f"max R-hat={m['max_rhat']}"]
    if m.get("gap") is not None:
        parts.append(f"benchmark gap={m['gap']:.1%}")
    return ", ".join(parts)


class Workspace:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.subscribers: set[asyncio.Queue] = set()
        self.agent_lock = asyncio.Lock()
        self.notify = None  # async callable(text), wired up by the server
        self.job = None
        self.upload = None  # {"filename", "frame"} waiting for a column mapping
        self.use_dataset("Meridian sample data", modeling.load_sample_frame(),
                         modeling.SAMPLE_MAPPING)

    # ------------------------------------------------------------ events

    def subscribe(self) -> asyncio.Queue:
        self.loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        self.subscribers.discard(queue)

    def publish(self, type: str, **data):
        """Safe to call from the event loop or from a job's worker thread."""
        if not self.subscribers:
            return
        event = {"type": type, **data}
        try:
            asyncio.get_running_loop()
            self._deliver(event)
        except RuntimeError:  # worker thread: hand over to the loop
            self.loop.call_soon_threadsafe(self._deliver, event)

    def _deliver(self, event: dict):
        for queue in self.subscribers:
            queue.put_nowait(event)

    def changed(self):
        self.publish("state", state=self.snapshot())

    # ------------------------------------------------------------ data

    def use_dataset(self, name: str, frame, mapping: modeling.ColumnMapping):
        if self.job:
            raise ValueError(f"wait for '{self.job['label']}' to finish before switching data")
        self._set_input_data(name, modeling.build_input_data(frame, mapping))
        self.upload = None

    def _set_input_data(self, name: str, input_data, priors=None, settings=None):
        """Switching data resets everything that depended on the old channels."""
        self.dataset_name = name
        self.input_data = input_data
        self.info = modeling.describe(input_data)
        self.priors = priors or modeling.default_priors(self.info["channels"])
        self.settings = settings or {"max_lag": 8, "knots": modeling.default_knots(input_data),
                                     "holdout_fraction": 0.2, "sampling": "quick"}
        self.targets = dict(DEFAULT_TARGETS)
        self.benchmark_shares = None
        self.guidance = []
        self.runs: list[Run] = []
        self.reports = {}
        self.scenarios = []
        self.changed()

    def set_upload(self, filename: str, frame):
        self.upload = {"filename": filename, "frame": frame}
        self.changed()

    @property
    def latest(self) -> Run | None:
        return self.runs[-1] if self.runs else None

    def run(self, run_id: int) -> Run:
        if not self.runs:
            raise ValueError("no model has been fitted yet")
        if run_id <= 0:
            return self.latest
        if run_id > len(self.runs):
            raise ValueError(f"no fit #{run_id}; there are {len(self.runs)} fits")
        return self.runs[run_id - 1]

    # ------------------------------------------------------------ configuration

    def set_prior(self, channel: str, mean: float, sd: float):
        if channel not in self.priors:
            raise ValueError(f"unknown channel {channel!r}; channels: {list(self.priors)}")
        if mean <= 0 or sd <= 0:
            raise ValueError("ROI prior mean and sd must be positive")
        self.priors[channel] = {"mean": float(mean), "sd": float(sd)}
        self.changed()

    def set_priors(self, priors: dict):
        for channel, p in priors.items():
            self.set_prior(channel, p["mean"], p["sd"])

    def reset_priors(self):
        self.priors = modeling.default_priors(self.info["channels"])
        self.changed()

    def set_settings(self, max_lag: int, knots: int, holdout_fraction: float, sampling: str):
        if sampling not in modeling.SAMPLING:
            raise ValueError(f"sampling must be one of {list(modeling.SAMPLING)}")
        if not 0 <= holdout_fraction < 0.5:
            raise ValueError("holdout_fraction must be in [0, 0.5)")
        if max_lag < 0:
            raise ValueError("max_lag must be >= 0")
        if not 1 <= knots <= self.info["n_weeks"]:
            raise ValueError(f"knots must be between 1 and {self.info['n_weeks']}")
        self.settings = {"max_lag": int(max_lag), "knots": int(knots),
                         "holdout_fraction": float(holdout_fraction), "sampling": sampling}
        self.changed()

    def set_benchmark(self, shares: dict):
        unknown = set(shares) - set(self.info["channels"])
        if unknown:
            raise ValueError(f"unknown channels: {sorted(unknown)}")
        total = sum(shares.values())
        if total <= 0:
            raise ValueError("benchmark shares must sum to a positive number")
        self.benchmark_shares = {c: shares.get(c, 0.0) / total for c in self.info["channels"]}
        for run in self.runs:  # the gap only depends on shares, so past fits can be updated
            self._apply_benchmark(run)
        self.changed()
        return {"normalized_shares": self.benchmark_shares}

    def _apply_benchmark(self, run: Run):
        run.metrics["gap"] = modeling.benchmark_gap(run.metrics["shares"], self.benchmark_shares)
        run.charts["shares"] = json.loads(
            modeling.shares_chart(run.metrics["shares"], self.benchmark_shares).to_json())

    def _add_run(self, run: Run):
        self._apply_benchmark(run)  # the benchmark may have changed while it was fitting
        self.runs.append(run)
        self.publish("run", run=run.summary())

    def set_targets(self, min_r2: float, max_gap: float, max_rhat: float):
        self.targets = {"min_r2": min_r2, "max_gap": max_gap, "max_rhat": max_rhat}
        self.changed()

    def add_guidance(self, note: str):
        self.guidance.append(note)
        self.changed()

    def failing_targets(self, metrics: dict) -> list[str]:
        t, failing = self.targets, []
        if metrics["r2"] < t["min_r2"]:
            failing.append(f"R² {metrics['r2']:.3f} < {t['min_r2']}")
        if metrics["max_rhat"] is not None and metrics["max_rhat"] > t["max_rhat"]:
            failing.append(f"max R-hat {metrics['max_rhat']} > {t['max_rhat']}")
        if metrics.get("gap") is not None and metrics["gap"] > t["max_gap"]:
            failing.append(f"gap {metrics['gap']:.1%} > {t['max_gap']:.1%}")
        return failing

    # ------------------------------------------------------------ background jobs

    def start_job(self, label: str, work) -> dict:
        """work: async callable returning a one-line outcome for the agent."""
        if self.job:
            raise ValueError(f"'{self.job['label']}' is already running; wait or stop it first")
        self.job = {"label": label, "started": time.time(), "stop": False}
        self.job["task"] = asyncio.create_task(self._run_job(label, work))
        self.changed()
        return {"status": "started", "job": label,
                "note": "Runs in the background; you will get a [system] message when done."}

    async def _run_job(self, label: str, work):
        try:
            if GPU.locked():
                self._progress("Waiting for another session's job to finish…")
            async with GPU:
                outcome = await work()
        except Exception as e:  # report any failure back to the user and agent
            outcome = f"{label} failed: {e}"
            self.publish("error", message=outcome)
        finally:
            self.job = None
            self.changed()
        if self.notify:
            await self.notify(f"[system] {outcome}")

    def stop(self) -> dict:
        if not self.job:
            raise ValueError("nothing is running")
        self.job["stop"] = True
        self.publish("progress", message="Stopping after the current step…")
        return {"status": "stopping", "note": "An MCMC fit in progress cannot be interrupted; "
                                              "the job stops as soon as it finishes."}

    def _progress(self, message: str):
        self.publish("progress", message=message)

    async def _fit(self, source: str, rationale: str = "") -> Run:
        priors, settings = copy.deepcopy(self.priors), dict(self.settings)
        self._progress(f"Sampling posterior ({settings['sampling']} settings)…")
        run = await asyncio.to_thread(self._fit_blocking, priors, settings, source, rationale)
        self._add_run(run)
        return run

    def _fit_blocking(self, priors, settings, source, rationale) -> Run:
        started = time.time()
        spec = modeling.build_spec(self.input_data, priors, settings["max_lag"],
                                   settings["knots"], settings["holdout_fraction"])
        mmm = modeling.fit(self.input_data, spec, settings["sampling"])
        self._progress("Computing diagnostics…")
        return self._make_run(mmm, source, priors, settings, rationale, started)

    def _make_run(self, mmm, source, priors, settings, rationale, started) -> Run:
        metrics = modeling.evaluate(mmm, self.benchmark_shares)
        charts = modeling.charts(mmm, metrics["shares"], self.benchmark_shares)
        return Run(id=len(self.runs) + 1, source=source, priors=priors, settings=settings,
                   metrics=metrics, seconds=round(time.time() - started, 1),
                   rationale=rationale, model=mmm, charts=charts)

    def start_fit(self) -> dict:
        async def work():
            run = await self._fit("manual")
            return f"Fit #{run.id} finished in {run.seconds:.0f}s: {metrics_line(run.metrics)}"
        return self.start_job("Fitting model", work)

    def start_autotune(self, max_iterations: int) -> dict:
        if not 1 <= max_iterations <= 10:
            raise ValueError("max_iterations must be between 1 and 10")
        return self.start_job("Auto-tuning priors", lambda: self._autotune(max_iterations))

    async def _autotune(self, max_iterations: int) -> str:
        """Fit -> evaluate -> ask the tuner for new priors, until targets are met."""
        reference = copy.deepcopy(self.priors)  # sd may only tighten from here
        rationale = "Starting priors."
        for i in range(1, max_iterations + 1):
            self._progress(f"Auto-tune {i}/{max_iterations}: fitting…")
            run = await self._fit("autotune", rationale)
            failing = self.failing_targets(run.metrics)
            if not failing:
                return (f"Auto-tune met all targets at fit #{run.id} "
                        f"(iteration {i}): {metrics_line(run.metrics)}")
            if self.job["stop"]:
                return f"Auto-tune stopped by the user after fit #{run.id}: {metrics_line(run.metrics)}"
            if i == max_iterations:
                break
            self._progress(f"Auto-tune {i}/{max_iterations}: tuner is proposing new priors…")
            proposed, rationale = await tuner.propose_priors(self._tuner_context(reference))
            self.priors = _clamp_priors(proposed, reference)
            self.changed()
        return (f"Auto-tune used all {max_iterations} iterations; still failing: "
                f"{'; '.join(failing)}. Latest fit #{run.id}: {metrics_line(run.metrics)}")

    def _tuner_context(self, reference: dict) -> dict:
        return {
            "channels": self.info["channels"],
            "current_priors": self.priors,
            "sd_caps": {c: p["sd"] for c, p in reference.items()},
            "history": [{"fit": r.id, "priors": r.priors,
                         **{k: r.metrics[k] for k in ("r2", "max_rhat", "shares", "gap")}}
                        for r in self.runs],
            "targets": self.targets,
            "benchmark_shares": self.benchmark_shares,
            "user_guidance": self.guidance,
        }

    def start_reports(self) -> dict:
        run = self.run(0)

        async def work():
            name = f"{self.session_id[:8]}_fit{run.id}"
            self._progress(f"Rendering Meridian reports for fit #{run.id}…")
            files = await asyncio.to_thread(modeling.write_reports, run.model, name)
            self.reports = {**files, "run_id": run.id}
            return f"Model summary and health card for fit #{run.id} are ready in the Reports tab."
        return self.start_job("Generating reports", work)

    def start_optimization(self, budget: float | None) -> dict:
        run = self.run(0)

        async def work():
            scenario_id = len(self.scenarios) + 1
            name = f"{self.session_id[:8]}_scenario{scenario_id}"
            self._progress(f"Optimizing budget with fit #{run.id}…")
            result = await asyncio.to_thread(modeling.optimize_budget, run.model, budget, name)
            self.scenarios.append({"id": scenario_id, "run_id": run.id, **result})
            return (f"Budget scenario #{scenario_id} (fit #{run.id}, budget {result['budget']:,.0f}): "
                    f"ROI {result['roi_before']} -> {result['roi_after']}. "
                    f"Spend by channel: {json.dumps(result['spend'])}")
        return self.start_job("Optimizing budget", work)

    async def save(self, name: str) -> dict:
        run = self.run(0)
        filename = await asyncio.to_thread(modeling.save, run.model, name)
        self.changed()
        return {"saved": filename, "fit": run.id}

    def start_load(self, name: str) -> dict:
        if name not in modeling.list_saved():
            raise ValueError(f"no saved model {name!r}; saved: {modeling.list_saved()}")

        async def work():
            started = time.time()
            self._progress(f"Loading {name} and computing diagnostics…")
            mmm = await asyncio.to_thread(modeling.load, name)
            priors, settings = modeling.model_config(mmm)
            self._set_input_data(f"Saved model: {name}", mmm.input_data, priors,
                                 {**settings, "sampling": "quick"})
            run = await asyncio.to_thread(self._make_run, mmm, "loaded", priors,
                                          self.settings, "", started)
            self._add_run(run)
            return f"Loaded {name} as fit #{run.id}: {metrics_line(run.metrics)}"
        return self.start_job("Loading model", work)

    # ------------------------------------------------------------ views

    def snapshot(self) -> dict:
        """Everything the dashboard renders (charts are fetched separately)."""
        return {
            "dataset": {"name": self.dataset_name, **self.info},
            "upload": self.upload and self.upload["filename"],
            "priors": self.priors,
            "settings": self.settings,
            "targets": self.targets,
            "benchmark_shares": self.benchmark_shares,
            "guidance": self.guidance,
            "runs": [r.summary() for r in self.runs],
            "job": self.job and {"label": self.job["label"], "started": self.job["started"]},
            "reports": self.reports,
            "scenarios": self.scenarios,
            "saved_models": modeling.list_saved(),
        }

    def agent_context(self) -> str:
        """Compact live state injected into the co-pilot's instruction every turn."""
        latest = self.latest
        state = {
            "dataset": {"name": self.dataset_name, **self.info},
            "uploaded_csv_awaiting_mapping": self.upload and self.upload["filename"],
            "roi_priors": self.priors,
            "settings": self.settings,
            "targets": self.targets,
            "benchmark_shares": self.benchmark_shares,
            "tuning_guidance": self.guidance,
            "running_job": self.job and self.job["label"],
            "n_fits": len(self.runs),
            "latest_fit": latest and {"id": latest.id, "source": latest.source,
                                      **latest.metrics},
            "reports_for_fit": self.reports.get("run_id"),
            "n_budget_scenarios": len(self.scenarios),
            "saved_models": modeling.list_saved(),
        }
        return json.dumps(state, indent=1, default=str)


def _clamp_priors(proposed: dict, reference: dict) -> dict:
    """Keep every channel; sd may tighten but never exceed its starting value."""
    clamped = {}
    for channel, ref in reference.items():
        p = proposed.get(channel, ref)
        clamped[channel] = {"mean": max(float(p["mean"]), 1e-3),
                            "sd": min(max(float(p["sd"]), 1e-3), ref["sd"])}
    return clamped


WORKSPACES: dict[str, Workspace] = {}


def get_workspace(session_id: str) -> Workspace:
    if session_id not in WORKSPACES:
        WORKSPACES[session_id] = Workspace(session_id)
    return WORKSPACES[session_id]
