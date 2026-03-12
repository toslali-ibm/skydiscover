"""
Analyze search effort and cost metrics across frameworks for BLIS router experiments.

Extracts per-iteration timing from log files, population/diversity metrics from
AdaEvolve iteration stats, and checkpoint metadata. Produces:
  1. Console summary table (effort per framework)
  2. CSV: effort_analysis.csv
  3. Plots: iteration duration boxplot, convergence curves, effort-vs-improvement scatter

Usage:
    python benchmarks/blis_router/scripts/analyze_effort.py <results_dir>

Outputs saved to <results_dir>/plots/ alongside existing plot_results.py outputs.
"""

import json
import re
import statistics
import sys
from pathlib import Path

try:
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
except ImportError:
    plt = None

# ───────────────────────────────────────────────────────────────────────────
# Data extraction helpers
# ───────────────────────────────────────────────────────────────────────────

TIMING_RE = re.compile(r"Iteration\s+(\d+):.*completed in\s+([\d.]+)s")
# Matches any "Iteration N:" line with a leading timestamp (covers REJECTED, merge, accepted)
ITER_TIMESTAMP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}),\d+\s+.*Iteration\s+(\d+):"
)
# Matches "Metrics: combined_score=X" lines (one per iteration in all frameworks)
METRICS_RE = re.compile(r"Iteration\s+(\d+):.*completed in")
SCORE_RE = re.compile(r"combined_score=([-\d.]+)")


def _parse_datetime(s: str):
    """Parse '2026-03-04 19:54:43' into a datetime."""
    from datetime import datetime
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def extract_iteration_times_from_log(fw_dir: Path) -> list[float]:
    """Extract per-iteration durations from log files.

    Primary method: parse explicit 'completed in Xs' lines (all frameworks).
    Fallback: compute durations from consecutive iteration timestamps. This
    captures iterations that frameworks like GEPA reject internally without
    logging 'completed in'.
    """
    log_files = list((fw_dir / "logs").glob("*.log")) if (fw_dir / "logs").is_dir() else []
    run_log = fw_dir / "run.log"
    if run_log.exists():
        log_files.append(run_log)

    # Collect explicit "completed in" times
    explicit_times: list[float] = []
    # Collect all iteration timestamps for fallback
    iter_timestamps: list[tuple[int, float]] = []  # (iter_num, unix_ts)

    for lf in log_files:
        for line in lf.read_text(errors="replace").splitlines():
            m = TIMING_RE.search(line)
            if m:
                explicit_times.append(float(m.group(2)))

            m2 = ITER_TIMESTAMP_RE.search(line)
            if m2:
                try:
                    ts = _parse_datetime(m2.group(1))
                    iter_num = int(m2.group(2))
                    iter_timestamps.append((iter_num, ts.timestamp()))
                except ValueError:
                    pass

    # Deduplicate timestamps: keep first occurrence per iteration number
    seen_iters: dict[int, float] = {}
    for iter_num, ts in iter_timestamps:
        if iter_num not in seen_iters:
            seen_iters[iter_num] = ts

    if not seen_iters:
        return explicit_times

    # Compute timestamp-derived times from consecutive iterations
    sorted_iters = sorted(seen_iters.items())
    ts_derived_times: list[float] = []
    for i in range(1, len(sorted_iters)):
        delta = sorted_iters[i][1] - sorted_iters[i - 1][1]
        if 0 < delta < 600:  # sanity: skip gaps > 10 min
            ts_derived_times.append(delta)

    # Use whichever method captured more iterations (more complete picture)
    if len(ts_derived_times) > len(explicit_times):
        return ts_derived_times
    return explicit_times


_GEPA_REJECTED_RE = re.compile(
    r"Iteration\s+(\d+):\s+REJECTED child \(child_score=([-\d.]+)"
)
_GEPA_MERGE_RE = re.compile(
    r"Iteration\s+(\d+):\s+Attempting merge.*scores:\s*([-\d.]+)"
)


def _extract_score_trajectory_from_logs(fw_dir: Path) -> list[float] | None:
    """Extract best-score-so-far trajectory from log files.

    Parses multiple iteration patterns across all frameworks:
    - "Iteration N: Program ... completed in Xs" + "Metrics: combined_score=X"
    - GEPA "Iteration N: REJECTED child (child_score=X ...)"
    - GEPA "Iteration N: Attempting merge ... scores: X, Y"

    Returns a list of best_score_so_far values, one per iteration (sorted by
    iteration number). This is more accurate than checkpoint-based trajectories
    because it captures every iteration, not just the surviving population.
    """
    log_files = list((fw_dir / "logs").glob("*.log")) if (fw_dir / "logs").is_dir() else []
    run_log = fw_dir / "run.log"
    if run_log.exists():
        log_files.append(run_log)

    # Collect (iteration_number, score) pairs
    iter_scores: dict[int, float] = {}
    for lf in log_files:
        lines = lf.read_text(errors="replace").splitlines()
        for i, line in enumerate(lines):
            # Pattern 1: "Iteration N: Program ... completed in Xs" + Metrics line
            m = TIMING_RE.search(line)
            if m:
                iter_num = int(m.group(1))
                for j in range(i, min(i + 3, len(lines))):
                    sm = SCORE_RE.search(lines[j])
                    if sm:
                        score = float(sm.group(1))
                        if iter_num not in iter_scores or score > iter_scores[iter_num]:
                            iter_scores[iter_num] = score
                        break
                continue

            # Pattern 2: GEPA "REJECTED child (child_score=X ...)"
            m = _GEPA_REJECTED_RE.search(line)
            if m:
                iter_num = int(m.group(1))
                score = float(m.group(2))
                if iter_num not in iter_scores or score > iter_scores[iter_num]:
                    iter_scores[iter_num] = score
                continue

            # Pattern 3: GEPA "Attempting merge ... scores: X, Y"
            m = _GEPA_MERGE_RE.search(line)
            if m:
                iter_num = int(m.group(1))
                score = float(m.group(2))
                if iter_num not in iter_scores or score > iter_scores[iter_num]:
                    iter_scores[iter_num] = score
                continue

    if not iter_scores:
        return None

    # Build best-so-far trajectory sorted by iteration
    sorted_iters = sorted(iter_scores.items())
    best_so_far = float("-inf")
    trajectory = []
    for _iter_num, score in sorted_iters:
        if score > best_so_far:
            best_so_far = score
        trajectory.append(best_so_far)

    return trajectory


def load_adaevolve_iteration_stats(fw_dir: Path) -> list[dict]:
    """Load AdaEvolve JSONL iteration stats if present."""
    jsonl_files = list(fw_dir.glob("adaevolve_iteration_stats_*.jsonl"))
    if not jsonl_files:
        return []
    # Take the latest one
    jsonl_file = sorted(jsonl_files)[-1]
    entries = []
    for line in jsonl_file.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def load_checkpoint_programs(fw_dir: Path) -> list[dict]:
    """Load all program JSONs from the latest checkpoint."""
    cp_dir = fw_dir / "checkpoints"
    if not cp_dir.is_dir():
        return []
    checkpoints = sorted(cp_dir.iterdir())
    if not checkpoints:
        return []
    latest = checkpoints[-1]
    programs_dir = latest / "programs"
    if not programs_dir.is_dir():
        return []
    programs = []
    for pf in programs_dir.glob("*.json"):
        try:
            programs.append(json.loads(pf.read_text()))
        except json.JSONDecodeError:
            pass
    return programs


def load_best_result(fw_dir: Path) -> dict | None:
    """Load best result from best/ dir, falling back to latest checkpoint."""
    best_info = fw_dir / "best" / "best_program_info.json"
    if best_info.exists():
        return json.loads(best_info.read_text())
    # Fallback: check latest checkpoint for best_program_info.json
    cp_dir = fw_dir / "checkpoints"
    if cp_dir.is_dir():
        checkpoints = sorted(cp_dir.iterdir())
        if checkpoints:
            cp_best = checkpoints[-1] / "best_program_info.json"
            if cp_best.exists():
                return json.loads(cp_best.read_text())
    return None


def load_baseline(results_dir: Path) -> dict | None:
    for fw_dir in sorted(results_dir.iterdir()):
        if not fw_dir.is_dir():
            continue
        bf = fw_dir / "baseline_metrics.json"
        if bf.exists():
            return json.loads(bf.read_text())
    return None


def load_adaevolve_metadata(fw_dir: Path) -> dict | None:
    """Load adaevolve_metadata.json from latest checkpoint."""
    cp_dir = fw_dir / "checkpoints"
    if not cp_dir.is_dir():
        return None
    checkpoints = sorted(cp_dir.iterdir())
    if not checkpoints:
        return None
    meta_file = checkpoints[-1] / "adaevolve_metadata.json"
    if meta_file.exists():
        return json.loads(meta_file.read_text())
    return None


# ───────────────────────────────────────────────────────────────────────────
# Per-framework analysis
# ───────────────────────────────────────────────────────────────────────────


def analyze_framework(fw_dir: Path) -> dict:
    """Extract all effort/cost metrics for a single framework."""
    name = fw_dir.name
    result: dict = {"framework": name}

    # 1. Best score
    best = load_best_result(fw_dir)
    if best:
        metrics = best.get("metrics", best)
        result["best_score"] = metrics.get("combined_score")
        result["best_iteration"] = best.get("iteration")
        result["best_generation"] = best.get("generation")

    # 2. Iteration timing from logs
    times = extract_iteration_times_from_log(fw_dir)
    if times:
        result["num_iterations"] = len(times)
        result["total_wall_time_s"] = sum(times)
        result["avg_iter_time_s"] = statistics.mean(times)
        result["median_iter_time_s"] = statistics.median(times)
        result["min_iter_time_s"] = min(times)
        result["max_iter_time_s"] = max(times)
        result["stddev_iter_time_s"] = statistics.stdev(times) if len(times) > 1 else 0.0
        result["iter_times"] = times  # keep raw for plotting

    # 3. Population size from checkpoint
    programs = load_checkpoint_programs(fw_dir)
    if programs:
        result["final_population_size"] = len(programs)
        # Unique scores → score diversity
        scores = [
            p.get("metrics", {}).get("combined_score")
            for p in programs
            if p.get("metrics", {}).get("combined_score") is not None
        ]
        if scores:
            result["unique_scores"] = len(set(round(s, 4) for s in scores))
            result["score_diversity_ratio"] = result["unique_scores"] / len(scores)
            result["population_best"] = max(scores)
            result["population_worst"] = min(scores)
            result["population_median"] = statistics.median(scores)
        # Generation depth
        gens = [p.get("generation", 0) for p in programs]
        result["max_generation"] = max(gens)
        result["avg_generation"] = statistics.mean(gens)

    # 4. AdaEvolve-specific: island stats, productivity, diversity
    ada_stats = load_adaevolve_iteration_stats(fw_dir)
    if ada_stats:
        result["has_iteration_stats"] = True
        last = ada_stats[-1]
        g = last.get("global", {})
        result["num_islands"] = g.get("num_islands")
        result["total_programs_evaluated"] = g.get("total_programs")
        result["global_productivity"] = g.get("global_productivity")

        # Per-island summary
        islands = last.get("islands", [])
        if islands:
            result["island_productivities"] = [
                round(isl.get("productivity", 0), 3) for isl in islands
            ]
            result["island_populations"] = [isl.get("population_size", 0) for isl in islands]
            result["island_improvements"] = [isl.get("improvement_count", 0) for isl in islands]
            result["island_evaluations"] = [isl.get("total_evaluations", 0) for isl in islands]
            result["island_intensities"] = [
                round(isl.get("search_intensity", 0), 3) for isl in islands
            ]

        # Paradigm breakthrough info
        paradigm = last.get("paradigm_breakthrough", {})
        if paradigm:
            result["is_stagnating"] = paradigm.get("is_stagnating")
            result["num_active_paradigms"] = paradigm.get("num_active_paradigms")

        # Score trajectory from iteration stats
        score_trajectory = []
        for entry in ada_stats:
            g = entry.get("global", {})
            score_trajectory.append(g.get("global_best_score"))
        result["score_trajectory"] = score_trajectory
    else:
        result["has_iteration_stats"] = False

    # 5. AdaEvolve metadata
    ada_meta = load_adaevolve_metadata(fw_dir)
    if ada_meta:
        result["num_islands"] = result.get("num_islands") or ada_meta.get("num_islands")
        result["diversity_strategy"] = ada_meta.get("diversity_strategy_type")
        result["use_adaptive_search"] = ada_meta.get("use_adaptive_search")
        result["use_ucb_selection"] = ada_meta.get("use_ucb_selection")
        result["use_paradigm_breakthrough"] = ada_meta.get("use_paradigm_breakthrough")
        result["use_dynamic_islands"] = ada_meta.get("use_dynamic_islands")

    # 6. Convergence: build score trajectory from log files (most accurate)
    #    Falls back to JSONL stats (AdaEvolve) or checkpoint programs
    if "score_trajectory" not in result:
        log_trajectory = _extract_score_trajectory_from_logs(fw_dir)
        if log_trajectory:
            result["score_trajectory"] = log_trajectory
        elif programs:
            # Last resort: from checkpoint programs (less accurate — only shows population, not iterations)
            sorted_progs = sorted(programs, key=lambda p: p.get("iteration_found", 0))
            best_so_far = float("-inf")
            trajectory = []
            for p in sorted_progs:
                s = p.get("metrics", {}).get("combined_score")
                if s is not None and s > best_so_far:
                    best_so_far = s
                trajectory.append(best_so_far if best_so_far > float("-inf") else None)
            result["score_trajectory"] = trajectory

    return result


# ───────────────────────────────────────────────────────────────────────────
# Output: table + CSV
# ───────────────────────────────────────────────────────────────────────────


def print_effort_table(analyses: list[dict], baseline: dict | None):
    print(f"\n{'Framework':<18} {'Iters':>6} {'Wall(m)':>8} {'Avg(s)':>7} {'Med(s)':>7} "
          f"{'Pop':>5} {'Uniq':>5} {'MaxGen':>7} {'BestIter':>9} {'Score':>10} {'Improv%':>8}")
    print("-" * 110)

    for a in analyses:
        fw = a["framework"]
        iters = a.get("num_iterations", "?")
        wall_m = f"{a['total_wall_time_s'] / 60:.1f}" if "total_wall_time_s" in a else "?"
        avg_s = f"{a['avg_iter_time_s']:.1f}" if "avg_iter_time_s" in a else "?"
        med_s = f"{a['median_iter_time_s']:.1f}" if "median_iter_time_s" in a else "?"
        pop = a.get("final_population_size", "?")
        uniq = a.get("unique_scores", "?")
        maxgen = a.get("max_generation", "?")
        best_it = a.get("best_iteration", "?")
        score = a.get("best_score")
        score_s = f"{score:.1f}" if score is not None else "?"
        # The evaluator's combined_score is already a percentage improvement
        # vs baseline (0% = same, +X% = better). Display it directly.
        if score is not None:
            improv = f"{score:+.1f}%"
        else:
            improv = "?"
        print(f"{fw:<18} {str(iters):>6} {wall_m:>8} {avg_s:>7} {med_s:>7} "
              f"{str(pop):>5} {str(uniq):>5} {str(maxgen):>7} {str(best_it):>9} {score_s:>10} {improv:>8}")

    # AdaEvolve-specific island details
    ada_analyses = [a for a in analyses if a.get("has_iteration_stats")]
    if ada_analyses:
        print("\n--- AdaEvolve Island Details ---")
        for a in ada_analyses:
            fw = a["framework"]
            print(f"\n  {fw}:")
            print(f"    Islands: {a.get('num_islands', '?')}")
            print(f"    Global productivity: {a.get('global_productivity', '?')}")
            print(f"    Total programs evaluated: {a.get('total_programs_evaluated', '?')}")
            if "island_productivities" in a:
                for i, (prod, pop, imp, ev, intens) in enumerate(zip(
                    a["island_productivities"],
                    a.get("island_populations", []),
                    a.get("island_improvements", []),
                    a.get("island_evaluations", []),
                    a.get("island_intensities", []),
                )):
                    print(f"    Island {i}: pop={pop}, improvements={imp}/{ev}, "
                          f"productivity={prod}, intensity={intens}")
            if a.get("diversity_strategy"):
                print(f"    Diversity strategy: {a['diversity_strategy']}")
            if a.get("is_stagnating") is not None:
                print(f"    Stagnating: {a['is_stagnating']}")
            if a.get("num_active_paradigms") is not None:
                print(f"    Active paradigms: {a['num_active_paradigms']}")


def save_effort_csv(analyses: list[dict], out_path: Path):
    cols = [
        "framework", "num_iterations", "total_wall_time_s", "avg_iter_time_s",
        "median_iter_time_s", "min_iter_time_s", "max_iter_time_s", "stddev_iter_time_s",
        "final_population_size", "unique_scores", "score_diversity_ratio",
        "max_generation", "avg_generation", "best_iteration", "best_generation",
        "best_score", "num_islands", "total_programs_evaluated", "global_productivity",
    ]
    with open(out_path, "w") as f:
        f.write(",".join(cols) + "\n")
        for a in analyses:
            vals = [str(a.get(c, "")) for c in cols]
            f.write(",".join(vals) + "\n")
    print(f"\nEffort CSV saved to {out_path}")


# ───────────────────────────────────────────────────────────────────────────
# Plots
# ───────────────────────────────────────────────────────────────────────────

FRAMEWORK_COLORS = {
    "adaevolve": "#4C72B0",
    "evox": "#C44E52",
    "openevolve": "#55A868",
    "gepa_native": "#DD8452",
    "topk": "#8172B2",
    "best_of_n": "#CCB974",
    "beam_search": "#64B5CD",
}
DEFAULT_COLOR = "#8172B2"


def _fw_color(name: str) -> str:
    return FRAMEWORK_COLORS.get(name, DEFAULT_COLOR)


def plot_iteration_duration_boxplot(analyses: list[dict], out_dir: Path):
    """Box plot of iteration durations across frameworks."""
    data = []
    labels = []
    colors = []
    for a in analyses:
        times = a.get("iter_times")
        if times:
            data.append(times)
            labels.append(a["framework"])
            colors.append(_fw_color(a["framework"]))

    if not data:
        return

    fig, ax = plt.subplots(figsize=(max(7, len(labels) * 1.4), 5))
    bp = ax.boxplot(data, tick_labels=labels, patch_artist=True, widths=0.5)
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)

    # Annotate median + mean
    for i, (d, label) in enumerate(zip(data, labels)):
        med = statistics.median(d)
        mean = statistics.mean(d)
        ax.text(i + 1, max(d) * 1.02, f"avg={mean:.1f}s\nmed={med:.1f}s",
                ha="center", va="bottom", fontsize=7.5)

    ax.set_ylabel("Iteration Duration (seconds)")
    ax.set_title("BLIS Router: Iteration Duration by Framework")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "iteration_duration_boxplot.png", dpi=150)
    plt.close(fig)
    print(f"  Saved {out_dir / 'iteration_duration_boxplot.png'}")


def plot_convergence_curves(analyses: list[dict], baseline: dict | None, out_dir: Path):
    """Line plot: best score over iterations for each framework."""
    fig, ax = plt.subplots(figsize=(9, 5.5))

    # Baseline is at 0% improvement (the evaluator's score IS % improvement vs baseline)
    ax.axhline(y=0, color="#999999", linestyle="--",
                linewidth=1.5, label="baseline (0%)", zorder=1)

    for a in analyses:
        traj = a.get("score_trajectory")
        if not traj:
            continue
        # Filter None values
        valid = [(i, v) for i, v in enumerate(traj) if v is not None]
        if not valid:
            continue
        iters, scores = zip(*valid)
        ax.plot(iters, scores, marker=".", markersize=3, linewidth=1.5,
                color=_fw_color(a["framework"]), label=a["framework"], zorder=2)

    ax.set_xlabel("Iteration")
    ax.set_ylabel("Best Score (higher = better)")
    ax.set_title("BLIS Router: Convergence Curves")
    ax.legend(loc="lower right")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "convergence_curves.png", dpi=150)
    plt.close(fig)
    print(f"  Saved {out_dir / 'convergence_curves.png'}")


def _time_to_best(a: dict) -> float | None:
    """Estimate wall-clock time to find the best solution (seconds).

    Uses the sum of the first `best_iteration` iteration durations if available,
    otherwise falls back to best_iteration * avg_iter_time.
    """
    best_iter = a.get("best_iteration")
    if best_iter is None:
        return None
    times = a.get("iter_times", [])
    if times and best_iter <= len(times):
        return sum(times[:best_iter])
    avg = a.get("avg_iter_time_s")
    if avg is not None:
        return best_iter * avg
    return None


def plot_effort_vs_improvement(analyses: list[dict], baseline: dict | None, out_dir: Path):
    """Scatter: total wall time vs score improvement, bubble size = population.

    Each point shows total wall time (x) vs final improvement (y).
    Annotated with best iteration and time-to-best for context.
    """
    fig, ax = plt.subplots(figsize=(9, 5.5))

    for a in analyses:
        score = a.get("best_score")
        wall = a.get("total_wall_time_s")
        if score is None or wall is None:
            continue
        # The evaluator's combined_score is already a percentage improvement
        # vs baseline (0% = same, +X% = better). Use it directly.
        pct_improv = score
        pop = a.get("final_population_size", 10)
        size = max(40, min(300, pop * 8))

        # Main point: total wall time
        ax.scatter(wall / 60, pct_improv, s=size, color=_fw_color(a["framework"]),
                   edgecolors="white", linewidth=1, zorder=3, alpha=0.85)

        # Annotate with framework name + time-to-best
        ttb = _time_to_best(a)
        best_iter = a.get("best_iteration", "?")
        if ttb is not None:
            label = f"{a['framework']}\n(best @ iter {best_iter}, {ttb/60:.0f}m)"
        else:
            label = a["framework"]
        ax.annotate(label, (wall / 60, pct_improv),
                    textcoords="offset points", xytext=(8, 4), fontsize=7.5)

        # Draw arrow from time-to-best to total wall time (shows wasted compute)
        if ttb is not None and ttb < wall * 0.9:
            ax.annotate("", xy=(wall / 60, pct_improv), xytext=(ttb / 60, pct_improv),
                        arrowprops=dict(arrowstyle="->", color=_fw_color(a["framework"]),
                                       alpha=0.3, lw=1.5))
            ax.scatter(ttb / 60, pct_improv, s=30, color=_fw_color(a["framework"]),
                       marker="d", edgecolors="white", linewidth=0.5, zorder=4, alpha=0.7)

    ax.axhline(y=0, color="#999999", linestyle="--", linewidth=0.8, zorder=1)
    ax.set_xlabel("Wall Time (minutes)  [diamond = time-to-best, circle = total]")
    ax.set_ylabel("Score Improvement vs Baseline (%)")
    ax.set_title("BLIS Router: Effort vs Improvement (bubble size = population)")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "effort_vs_improvement.png", dpi=150)
    plt.close(fig)
    print(f"  Saved {out_dir / 'effort_vs_improvement.png'}")


def plot_efficiency_bar(analyses: list[dict], baseline: dict | None, out_dir: Path):
    """Grouped bar chart: efficiency by total wall time vs time-to-best.

    Shows two bars per framework:
    - Total: % improvement / total wall time (given fixed iteration budget)
    - Time-to-best: % improvement / time until best was found (convergence speed)
    """
    names = []
    eff_total = []
    eff_ttb = []

    for a in analyses:
        score = a.get("best_score")
        wall = a.get("total_wall_time_s")
        ttb = _time_to_best(a)
        if score is None or wall is None or wall == 0:
            continue
        # The evaluator's combined_score is already a percentage improvement.
        pct_improv = score
        names.append(a["framework"])
        eff_total.append(pct_improv / (wall / 60))
        eff_ttb.append(pct_improv / (ttb / 60) if ttb and ttb > 0 else 0)

    if not names:
        return

    x = range(len(names))
    width = 0.35
    fig, ax = plt.subplots(figsize=(max(8, len(names) * 2), 5.5))
    bars1 = ax.bar([i - width / 2 for i in x], eff_total, width,
                   label="Total wall time", color="#4C72B0", edgecolor="white")
    bars2 = ax.bar([i + width / 2 for i in x], eff_ttb, width,
                   label="Time to best", color="#55A868", edgecolor="white")

    for bar, eff in zip(bars1, eff_total):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                f"{eff:.2f}", ha="center", va="bottom", fontsize=8, fontweight="bold",
                color="#4C72B0")
    for bar, eff in zip(bars2, eff_ttb):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                f"{eff:.2f}", ha="center", va="bottom", fontsize=8, fontweight="bold",
                color="#55A868")

    ax.set_xticks(list(x))
    ax.set_xticklabels(names)
    ax.set_ylabel("Improvement per Minute (%/min)")
    ax.set_title("BLIS Router: Search Efficiency (%/min by total time vs time-to-best)")
    ax.legend()
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "search_efficiency.png", dpi=150)
    plt.close(fig)
    print(f"  Saved {out_dir / 'search_efficiency.png'}")


# ───────────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_effort.py <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    if not results_dir.is_dir():
        print(f"Directory not found: {results_dir}")
        sys.exit(1)

    # Find framework directories
    fw_dirs = sorted(
        p for p in results_dir.iterdir()
        if p.is_dir() and (p / "best").is_dir() or (p / "checkpoints").is_dir() or (p / "logs").is_dir()
    )

    if not fw_dirs:
        print("No framework results found.")
        sys.exit(1)

    baseline = load_baseline(results_dir)
    if baseline:
        print(f"Baseline: combined_score={baseline.get('combined_score', 'N/A')}")

    # Analyze each framework
    analyses = [analyze_framework(d) for d in fw_dirs]

    # Print table
    print_effort_table(analyses, baseline)

    # Save CSV
    save_effort_csv(analyses, results_dir / "effort_analysis.csv")

    # Save detailed JSON
    # Strip non-serializable data for JSON
    json_analyses = []
    for a in analyses:
        ja = {k: v for k, v in a.items() if k != "iter_times"}
        json_analyses.append(ja)
    json_path = results_dir / "effort_analysis.json"
    with open(json_path, "w") as f:
        json.dump(json_analyses, f, indent=2, default=str)
    print(f"Effort JSON saved to {json_path}")

    # Plots
    if plt is None:
        print("\nSkipping plots (matplotlib not installed).")
        return

    out_dir = results_dir / "plots"
    out_dir.mkdir(exist_ok=True)
    print(f"\nGenerating effort/cost plots...")
    plot_iteration_duration_boxplot(analyses, out_dir)
    plot_convergence_curves(analyses, baseline, out_dir)
    plot_effort_vs_improvement(analyses, baseline, out_dir)
    plot_efficiency_bar(analyses, baseline, out_dir)
    print(f"\nAll effort plots saved to {out_dir}/")


if __name__ == "__main__":
    main()
