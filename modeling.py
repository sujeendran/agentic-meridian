"""Every Google Meridian call lives here.

data -> ModelSpec (ROI priors) -> MCMC fit -> metrics / charts -> reports,
budget optimization, save/load. Nothing in this module knows about the agent
or the web server, so it can be used (and tested) on its own.

(Not named `mmm.py`: that would shadow the `mmm` proto package Meridian's
serializer imports.)
"""
import json
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# JAX backend (Meridian's default): Tensorflow is deprecated.
# Set MERIDIAN_BACKEND=tensorflow to switch back. 
# Must be set before importing meridian.
os.environ.setdefault("MERIDIAN_BACKEND", "jax")
# Persist compiled kernels across server restarts.
# os.environ.setdefault("JAX_COMPILATION_CACHE_DIR", str(Path(__file__).parent / ".jax_cache"))

import altair as alt
import numpy as np
import pandas as pd
from meridian.analysis import analyzer, optimizer, summarizer, visualizer
from meridian.analysis.review import reviewer
from meridian.data import data_frame_input_data_builder
from meridian.model import model, prior_distribution, spec
from meridian.schema.serde import meridian_serde

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
MODELS_DIR = ROOT / "models"
REPORTS_DIR = ROOT / "reports"

SAMPLE_URL = (
    "https://raw.githubusercontent.com/google/meridian/refs/heads/main/"
    "meridian/data/simulated_data/csv/geo_all_channels.csv"
)

# MCMC settings. "quick" (~4 min on the laptop GPU, converges on the sample data)
# is for iterating and the auto-tune loop; "full" is for a final model.
SAMPLING = {
    "quick": dict(n_chains=4, n_adapt=500, n_burnin=500, n_keep=500),
    "full": dict(n_chains=4, n_adapt=1000, n_burnin=1000, n_keep=1000),
}
N_PRIOR_DRAWS = 200

# Meridian's default ROI prior is LogNormal(0.2, 0.9), i.e. mean ~1.8, sd ~2.2.
DEFAULT_ROI_PRIOR = {"mean": 1.8, "sd": 2.2}


@dataclass
class ColumnMapping:
    """Which CSV columns feed which Meridian inputs."""

    time: str
    kpi: str
    media: list[str]
    spend: list[str]
    channels: list[str]
    kpi_type: str = "non_revenue"  # or "revenue"
    geo: str | None = None
    population: str | None = None
    revenue_per_kpi: str | None = None
    controls: list[str] = field(default_factory=list)

    def columns(self):
        optional = [self.geo, self.population, self.revenue_per_kpi]
        return [self.time, self.kpi, *self.media, *self.spend, *self.controls,
                *[c for c in optional if c]]


SAMPLE_MAPPING = ColumnMapping(
    geo="geo",
    time="time",
    kpi="conversions",
    revenue_per_kpi="revenue_per_conversion",
    population="population",
    controls=["competitor_sales_control", "sentiment_score_control"],
    media=[f"Channel{i}_impression" for i in range(5)],
    spend=[f"Channel{i}_spend" for i in range(5)],
    channels=[f"Channel{i}" for i in range(5)],
)


# ---------------------------------------------------------------- data


def load_sample_frame() -> pd.DataFrame:
    """Meridian's simulated geo dataset (40 geos x 156 weeks), cached locally."""
    path = DATA_DIR / "geo_all_channels.csv"
    if not path.exists():
        DATA_DIR.mkdir(exist_ok=True)
        urllib.request.urlretrieve(SAMPLE_URL, path)
    return pd.read_csv(path, index_col=0)


def clean_frame(df: pd.DataFrame, mapping: ColumnMapping) -> pd.DataFrame:
    """Apply Meridian's input guardrails; raise ValueError with a readable message."""
    missing = [c for c in mapping.columns() if c not in df.columns]
    if missing:
        raise ValueError(f"columns not found in data: {missing}")
    if not (len(mapping.media) == len(mapping.spend) == len(mapping.channels)):
        raise ValueError("media, spend and channels must have the same length")

    df = df[mapping.columns()].copy()
    df[mapping.time] = pd.to_datetime(df[mapping.time]).dt.strftime("%Y-%m-%d")
    numeric = [c for c in mapping.columns() if c not in (mapping.time, mapping.geo)]
    for col in numeric:
        if df[col].dtype == object:
            df[col] = df[col].str.replace(r"[,$€£\s]", "", regex=True)
        df[col] = pd.to_numeric(df[col], errors="raise")
    df[mapping.media + mapping.spend] = df[mapping.media + mapping.spend].fillna(0)

    for col in mapping.media + mapping.spend + [c for c in [mapping.population] if c]:
        if (df[col] < 0).any():
            raise ValueError(f"column {col!r} has negative values")
    keys = [c for c in [mapping.geo, mapping.time] if c]
    if df.duplicated(keys).any():
        raise ValueError(f"duplicate rows for {keys}")
    return df


def build_input_data(df: pd.DataFrame, mapping: ColumnMapping):
    df = clean_frame(df, mapping)
    builder = data_frame_input_data_builder.DataFrameInputDataBuilder(
        kpi_type=mapping.kpi_type,
        default_geo_column=mapping.geo or "geo",
        default_time_column=mapping.time,
        default_media_time_column=mapping.time,
    )
    builder = builder.with_kpi(df, kpi_col=mapping.kpi)
    if mapping.revenue_per_kpi:
        builder = builder.with_revenue_per_kpi(df, revenue_per_kpi_col=mapping.revenue_per_kpi)
    if mapping.population:
        builder = builder.with_population(df, population_col=mapping.population)
    if mapping.controls:
        builder = builder.with_controls(df, control_cols=mapping.controls)
    builder = builder.with_media(
        df,
        media_cols=mapping.media,
        media_spend_cols=mapping.spend,
        media_channels=mapping.channels,
    )
    return builder.build()


def describe(input_data) -> dict:
    """Compact dataset summary for the agent and the dashboard."""
    times = input_data.time.values
    spend = input_data.media_spend.sum(dim=[d for d in input_data.media_spend.dims
                                             if d != "media_channel"])
    return {
        "channels": [str(c) for c in input_data.media_channel.values],
        "n_geos": int(input_data.kpi.sizes["geo"]),
        "n_weeks": len(times),
        "start": str(times[0]),
        "end": str(times[-1]),
        "kpi_type": input_data.kpi_type,
        "total_kpi": float(input_data.kpi.sum()),
        "total_spend_by_channel": {
            str(c): round(float(v), 2) for c, v in zip(spend.media_channel.values, spend.values)
        },
        "controls": [str(c) for c in input_data.control_variable.values]
        if input_data.controls is not None else [],
    }


# ---------------------------------------------------------------- model


def default_priors(channels) -> dict:
    return {c: dict(DEFAULT_ROI_PRIOR) for c in channels}


def default_knots(input_data) -> int:
    """Roughly one baseline knot per month for geo data. Meridian's default (one
    per week) converges poorly with short chains; national data uses 1."""
    n_geos, n_times = input_data.kpi.sizes["geo"], input_data.kpi.sizes["time"]
    return max(1, n_times // 4) if n_geos > 1 else 1


def build_spec(input_data, priors: dict, max_lag: int, knots: int, holdout_fraction: float):
    """priors: {channel: {"mean": roi, "sd": roi_sd}} in plain ROI units."""
    channels = [str(c) for c in input_data.media_channel.values]
    roi_m = prior_distribution.lognormal_dist_from_mean_std(
        [priors[c]["mean"] for c in channels], [priors[c]["sd"] for c in channels]
    )
    return spec.ModelSpec(
        prior=prior_distribution.PriorDistribution(roi_m=roi_m),
        max_lag=max_lag,
        knots=knots,
        holdout_id=_holdout_id(input_data, holdout_fraction),
    )


def _holdout_id(input_data, holdout_fraction: float):
    """Hold out a random (seeded) share of geo-week cells.

    Not the last weeks: a geo model gets one baseline knot per week, so weeks
    missing from every geo can't be predicted and a chronological holdout
    scores the baseline's extrapolation rather than the media model.
    """
    if holdout_fraction <= 0:
        return None
    n_geos, n_times = input_data.kpi.sizes["geo"], input_data.kpi.sizes["time"]
    shape = (n_times,) if n_geos == 1 else (n_geos, n_times)
    return np.random.default_rng(0).random(shape) < holdout_fraction


def fit(input_data, model_spec, sampling: str = "quick", seed: int = 0):
    mmm = model.Meridian(input_data=input_data, model_spec=model_spec)
    mmm.sample_prior(N_PRIOR_DRAWS, seed=seed)
    mmm.sample_posterior(**SAMPLING[sampling], seed=seed)
    return mmm


def model_config(mmm) -> tuple[dict, dict]:
    """Recover ROI priors and settings from a (loaded) fitted model."""
    channels = [str(c) for c in mmm.input_data.media_channel.values]
    roi_m = mmm.model_spec.prior.roi_m
    means = np.broadcast_to(np.asarray(roi_m.mean()), len(channels))
    sds = np.broadcast_to(np.asarray(roi_m.stddev()), len(channels))
    priors = {c: {"mean": round(float(m), 3), "sd": round(float(s), 3)}
              for c, m, s in zip(channels, means, sds)}
    holdout = mmm.model_spec.holdout_id
    knots = mmm.model_spec.knots
    settings = {
        "max_lag": mmm.model_spec.max_lag,
        "knots": knots if isinstance(knots, int) else default_knots(mmm.input_data),
        "holdout_fraction": round(float(np.mean(holdout)), 3) if holdout is not None else 0.0,
    }
    return priors, settings


def use_kpi(mmm) -> bool:
    """Report in KPI units when there is no way to convert KPI to revenue."""
    data = mmm.input_data
    return data.kpi_type == "non_revenue" and data.revenue_per_kpi is None


# ---------------------------------------------------------------- results


def evaluate(mmm, benchmark_shares: dict | None) -> dict:
    """Fit quality, convergence, ROI and contribution shares for one fitted model."""
    an = analyzer.Analyzer(model_context=mmm.model_context, inference_data=mmm.inference_data)
    kpi_only = use_kpi(mmm)

    # Score at the level the model is fit: per geo, or national for national data.
    granularity = "national" if mmm.input_data.kpi.sizes["geo"] == 1 else "geo"
    accuracy = an.predictive_accuracy(use_kpi=kpi_only)["value"].sel(geo_granularity=granularity)
    if "evaluation_set" in accuracy.dims:
        accuracy = accuracy.sel(evaluation_set="Test")

    try:
        max_rhat = round(float(an.rhat_summary()["max_r_hat"].max()), 3)
    except ValueError:  # needs >= 2 draws per chain
        max_rhat = None

    summary = an.summary_metrics(use_kpi=kpi_only, confidence_level=0.9).sel(
        distribution="posterior"
    )
    channels = [str(c) for c in summary.channel.values if c != "All Channels"]
    roi = {
        c: {m: round(float(summary["roi"].sel(channel=c, metric=m)), 3)
            for m in ("mean", "ci_lo", "ci_hi")}
        for c in channels
    }
    contrib = {c: float(summary["pct_of_contribution"].sel(channel=c, metric="mean"))
               for c in channels}
    total = sum(contrib.values()) or 1.0
    shares = {c: round(v / total, 4) for c, v in contrib.items()}

    return {
        "r2": round(float(accuracy.sel(metric="R_Squared")), 4),
        "mape": round(float(accuracy.sel(metric="MAPE")), 4),
        "max_rhat": max_rhat,
        "roi": roi,
        "shares": shares,
        "gap": benchmark_gap(shares, benchmark_shares),
    }


def benchmark_gap(shares: dict, benchmark_shares: dict | None) -> float | None:
    """Mean absolute difference between model and benchmark contribution shares."""
    if not benchmark_shares:
        return None
    return round(float(np.mean([abs(s - benchmark_shares.get(c, 0.0))
                                for c, s in shares.items()])), 4)


def charts(mmm, shares: dict, benchmark_shares: dict | None) -> dict:
    """Vega-Lite specs (from Meridian's own visualizers) for the dashboard."""
    kpi_only = use_kpi(mmm)
    specs = {
        "fit": visualizer.ModelFit(mmm, use_kpi=kpi_only).plot_model_fit(),
        "roi": visualizer.MediaSummary(mmm, use_kpi=kpi_only).plot_roi_bar_chart(),
        "shares": shares_chart(shares, benchmark_shares),
    }
    try:
        specs["rhat"] = visualizer.ModelDiagnostics(mmm).plot_rhat_boxplot()
    except ValueError:
        pass
    return {name: json.loads(chart.to_json()) for name, chart in specs.items()}


def shares_chart(shares: dict, benchmark_shares: dict | None):
    rows = [{"channel": c, "source": "Model", "share": s} for c, s in shares.items()]
    rows += [{"channel": c, "source": "Benchmark", "share": s}
             for c, s in (benchmark_shares or {}).items()]
    return (
        alt.Chart(pd.DataFrame(rows))
        .mark_bar(cornerRadiusEnd=4, height={"band": 0.9})
        .encode(
            y=alt.Y("channel:N", title=None, scale=alt.Scale(paddingInner=0.3)),
            yOffset=alt.YOffset("source:N", sort=["Model", "Benchmark"]),
            x=alt.X("share:Q", axis=alt.Axis(format="%", grid=True, gridColor="#eeede9"),
                    title="Share of paid-media contribution"),
            color=alt.Color("source:N", sort=["Model", "Benchmark"],
                            scale=alt.Scale(domain=["Model", "Benchmark"],
                                            range=["#2a78d6", "#eb6834"]),
                            legend=alt.Legend(orient="top", title=None)),
            tooltip=["channel", "source", alt.Tooltip("share:Q", format=".1%")],
        )
        .properties(width="container", height=240)
        .configure_view(stroke=None)
        .configure_axis(domainColor="#d6d5d0", labelColor="#52514e", titleColor="#52514e")
    )


def write_reports(mmm, name: str) -> dict:
    """Meridian's model-results summary and health card as HTML files."""
    REPORTS_DIR.mkdir(exist_ok=True)
    summary_file, health_file = f"{name}_summary.html", f"{name}_health.html"
    summarizer.Summarizer(mmm, use_kpi=use_kpi(mmm)).output_model_results_summary(
        summary_file, str(REPORTS_DIR)
    )
    review = reviewer.ModelReviewer(
        model_context=mmm.model_context, inference_data=mmm.inference_data
    ).run()
    review.output_model_health_card(health_file, str(REPORTS_DIR))
    return {"summary": summary_file, "health": health_file}


def optimize_budget(mmm, budget: float | None, name: str) -> dict:
    """Fixed-budget optimization (defaults to the historical total spend)."""
    kpi_only = use_kpi(mmm)
    result = optimizer.BudgetOptimizer(mmm).optimize(
        fixed_budget=True, budget=budget, use_kpi=kpi_only
    )
    REPORTS_DIR.mkdir(exist_ok=True)
    report_file = f"{name}_optimization.html"
    result.output_optimization_summary(report_file, str(REPORTS_DIR))

    before, after = result.nonoptimized_data, result.optimized_data
    channels = [str(c) for c in after.channel.values]
    return {
        "budget": round(float(after.attrs["budget"]), 2),
        "roi_before": round(float(before.attrs["total_roi"]), 3),
        "roi_after": round(float(after.attrs["total_roi"]), 3),
        "outcome_before": round(float(before.attrs["total_incremental_outcome"]), 2),
        "outcome_after": round(float(after.attrs["total_incremental_outcome"]), 2),
        "spend": {
            c: {"current": round(float(before["spend"].sel(channel=c)), 2),
                "optimized": round(float(after["spend"].sel(channel=c)), 2)}
            for c in channels
        },
        "report": report_file,
    }


# ---------------------------------------------------------------- persistence


def save(mmm, name: str) -> str:
    MODELS_DIR.mkdir(exist_ok=True)
    path = MODELS_DIR / f"{name}.binpb"
    meridian_serde.save_meridian(mmm, str(path))
    return path.name


def load(name: str):
    return meridian_serde.load_meridian(str(MODELS_DIR / f"{name}.binpb"))


def list_saved() -> list[str]:
    return sorted(p.stem for p in MODELS_DIR.glob("*.binpb"))
