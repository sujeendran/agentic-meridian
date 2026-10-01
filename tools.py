"""The co-pilot's tools. Each one is a thin, documented wrapper around a
Workspace method; docstrings are what the LLM sees."""
from google.adk.tools import ToolContext

import modeling
from workspace import get_workspace


def _call(tool_context: ToolContext, action) -> dict:
    """Run action(workspace) and turn validation errors into a tool result."""
    try:
        return {"status": "ok", **(action(get_workspace(tool_context.session.id)) or {})}
    except ValueError as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------- data


def inspect_uploaded_csv(tool_context: ToolContext) -> dict:
    """Show the columns, dtypes and first rows of the CSV the user uploaded, so you
    can propose a column mapping."""
    def action(ws):
        if not ws.upload:
            raise ValueError("no CSV has been uploaded")
        frame = ws.upload["frame"]
        return {
            "filename": ws.upload["filename"],
            "n_rows": len(frame),
            "dtypes": {c: str(t) for c, t in frame.dtypes.items()},
            "head": frame.head(5).to_dict(orient="records"),
        }
    return _call(tool_context, action)


def map_csv_columns(
    time: str,
    geo: str,
    population: str,
    kpi: str,
    kpi_type: str,
    revenue_per_kpi: str,
    controls: list[str],
    media: list[str],
    spend: list[str],
    channels: list[str],
    tool_context: ToolContext,
) -> dict:
    """Build the modeling dataset from the uploaded CSV. Confirm the mapping with
    the user first. This replaces the current dataset and resets priors and fits.

    Args:
        time: Date column (weekly dates).
        geo: Geo column, or "" for national data.
        population: Population column, or "" if none (national data).
        kpi: Outcome column (e.g. sales, conversions).
        kpi_type: "revenue" if the KPI is money, else "non_revenue".
        revenue_per_kpi: Revenue-per-KPI column, or "" if none.
        controls: Control variable columns (may be empty).
        media: Media execution columns (impressions etc.), one per channel.
        spend: Spend columns, same order as media.
        channels: Display names for the channels, same order as media.
    """
    def action(ws):
        if not ws.upload:
            raise ValueError("no CSV has been uploaded")
        mapping = modeling.ColumnMapping(
            time=time, geo=geo or None, population=population or None, kpi=kpi,
            kpi_type=kpi_type, revenue_per_kpi=revenue_per_kpi or None,
            controls=controls, media=media, spend=spend, channels=channels,
        )
        ws.use_dataset(ws.upload["filename"], ws.upload["frame"], mapping)
        return {"dataset": ws.info}
    return _call(tool_context, action)


def use_sample_data(tool_context: ToolContext) -> dict:
    """Switch back to Meridian's bundled sample dataset (resets priors and fits)."""
    return _call(tool_context, lambda ws: ws.use_dataset(
        "Meridian sample data", modeling.load_sample_frame(), modeling.SAMPLE_MAPPING))


# ---------------------------------------------------------------- configuration


def set_roi_prior(channel: str, mean: float, sd: float, tool_context: ToolContext) -> dict:
    """Set one channel's LogNormal ROI prior, in plain ROI units (1.0 = break-even).

    Args:
        channel: Channel name exactly as in the dataset.
        mean: Prior mean ROI (> 0).
        sd: Prior standard deviation of ROI (> 0); smaller = more confident.
    """
    return _call(tool_context, lambda ws: ws.set_prior(channel, mean, sd))


def reset_priors(tool_context: ToolContext) -> dict:
    """Reset every channel to Meridian's default ROI prior (mean 1.8, sd 2.2)."""
    return _call(tool_context, lambda ws: ws.reset_priors())


def set_model_settings(
    max_lag: int, knots: int, holdout_fraction: float, sampling: str, tool_context: ToolContext
) -> dict:
    """Change model settings. Pass the current value for anything not changing.

    Args:
        max_lag: Maximum adstock carry-over in weeks (typically 4-13).
        knots: Number of baseline (time trend) knots; more = more flexible baseline
            but slower convergence. Default is about one per month for geo data.
        holdout_fraction: Share of geo-weeks held out at random for out-of-sample R² (0-0.5).
        sampling: "quick" (~4 min, for iterating) or "full" (~2x slower, for a final model).
    """
    return _call(tool_context,
                 lambda ws: ws.set_settings(max_lag, knots, holdout_fraction, sampling))


def set_benchmark_shares(channels: list[str], shares: list[float], tool_context: ToolContext) -> dict:
    """Set benchmark shares of media contribution per channel (e.g. from experiments
    or a previous study). They are normalized to sum to 1; missing channels get 0.

    Args:
        channels: Channel names.
        shares: Share for each channel, same order (fractions or percentages).
    """
    if len(channels) != len(shares):
        return {"status": "error", "message": "channels and shares must have the same length"}
    return _call(tool_context, lambda ws: ws.set_benchmark(dict(zip(channels, shares))))


def set_targets(min_r2: float, max_gap: float, max_rhat: float, tool_context: ToolContext) -> dict:
    """Set the auto-tune targets. Pass current values for anything not changing.

    Args:
        min_r2: Minimum out-of-sample R².
        max_gap: Maximum mean absolute gap to benchmark shares (fraction, e.g. 0.03).
        max_rhat: Maximum R-hat for convergence (1.2 is standard, 1.1 is strict).
    """
    return _call(tool_context, lambda ws: ws.set_targets(min_r2, max_gap, max_rhat))


# ---------------------------------------------------------------- modeling jobs


def fit_model(tool_context: ToolContext) -> dict:
    """Start fitting Meridian with the current priors and settings in the
    background. Returns immediately; results arrive later as a [system] message."""
    return _call(tool_context, lambda ws: ws.start_fit())


def start_autotune(max_iterations: int, tool_context: ToolContext) -> dict:
    """Start the auto-tune loop in the background: fit, check targets, let the tuner
    adjust ROI priors, repeat until targets are met or iterations run out.

    Args:
        max_iterations: Maximum number of fits (1-10; 5 is a good default).
    """
    return _call(tool_context, lambda ws: ws.start_autotune(max_iterations))


def add_tuning_guidance(note: str, tool_context: ToolContext) -> dict:
    """Pass an instruction from the user to the tuner (applies from the next
    iteration), e.g. "keep Channel2 ROI above 1.5".

    Args:
        note: The guidance, in plain language.
    """
    return _call(tool_context, lambda ws: ws.add_guidance(note))


def stop_job(tool_context: ToolContext) -> dict:
    """Stop the running background job after its current step."""
    return _call(tool_context, lambda ws: ws.stop())


# ---------------------------------------------------------------- results


def get_results(run_id: int, tool_context: ToolContext) -> dict:
    """Full results of one fit: R², MAPE, max R-hat, ROI (mean and 90% CI) and
    contribution shares per channel, benchmark gap, priors and settings used.

    Args:
        run_id: Fit number, or 0 for the latest fit.
    """
    return _call(tool_context, lambda ws: ws.run(run_id).summary())


def compare_runs(tool_context: ToolContext) -> dict:
    """One row per fit so far with headline metrics, to compare iterations."""
    def action(ws):
        return {"fits": [
            {"fit": r.id, "source": r.source, "r2": r.metrics["r2"], "mape": r.metrics["mape"],
             "max_rhat": r.metrics["max_rhat"], "gap": r.metrics["gap"],
             "roi_means": {c: v["mean"] for c, v in r.metrics["roi"].items()}}
            for r in ws.runs
        ]}
    return _call(tool_context, action)


def generate_reports(tool_context: ToolContext) -> dict:
    """Render Meridian's model-results summary and health card for the latest fit
    (background job; shown in the Reports tab)."""
    return _call(tool_context, lambda ws: ws.start_reports())


def optimize_budget(budget: float, tool_context: ToolContext) -> dict:
    """Find the ROI-maximizing split of a fixed total budget across channels using
    the latest fit (background job; shown in the Budget tab). Each channel may move
    up to ±30% from its historical spend.

    Args:
        budget: Total budget to allocate, or 0 to use the historical total spend.
    """
    return _call(tool_context, lambda ws: ws.start_optimization(budget or None))


async def save_model(name: str, tool_context: ToolContext) -> dict:
    """Save the latest fitted model to disk.

    Args:
        name: File name without extension (letters, digits, - and _).
    """
    if not name.replace("-", "").replace("_", "").isalnum():
        return {"status": "error", "message": "use only letters, digits, - and _"}
    try:
        return {"status": "ok", **await get_workspace(tool_context.session.id).save(name)}
    except ValueError as e:
        return {"status": "error", "message": str(e)}


def load_model(name: str, tool_context: ToolContext) -> dict:
    """Load a saved model (replaces the current dataset and fits; background job).

    Args:
        name: Saved model name (see saved_models in the state).
    """
    return _call(tool_context, lambda ws: ws.start_load(name))


ALL_TOOLS = [
    inspect_uploaded_csv, map_csv_columns, use_sample_data,
    set_roi_prior, reset_priors, set_model_settings, set_benchmark_shares, set_targets,
    fit_model, start_autotune, add_tuning_guidance, stop_job,
    get_results, compare_runs, generate_reports, optimize_budget, save_model, load_model,
]
