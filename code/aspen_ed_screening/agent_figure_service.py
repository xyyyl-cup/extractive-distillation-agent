# -*- coding: utf-8 -*-
"""Generate and index local figures for one extractive-distillation system.

The service is intentionally read-only with respect to calculations: it only
reads existing report / optimizer artifacts and writes PNG/PDF/SVG figure files
plus a figure_manifest.json inside the selected system folder.
"""

import argparse
import contextlib
import csv
import datetime as dt
import json
import math
import os
import re
import time
from pathlib import Path


FIGURE_MANIFEST_NAME = "figure_manifest.json"
FIGURE_VERSION = 2


def read_json_default(path, default):
    try:
        with Path(path).open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except Exception:
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
    tmp.replace(path)


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def as_float(value, default=None):
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except Exception:
        return default
    if not math.isfinite(number):
        return default
    return number


def as_int(value, default=0):
    number = as_float(value, None)
    if number is None:
        return default
    return int(round(number))


def truthy(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "pass", "ok"}


def path_mtime(path):
    try:
        return Path(path).stat().st_mtime
    except Exception:
        return 0.0


def time_since_mtime(path):
    mtime = path_mtime(path)
    if not mtime:
        return float("inf")
    return max(0.0, time.time() - mtime)


def newer_than_outputs(sources, outputs):
    output_times = [path_mtime(path) for path in outputs if Path(path).exists()]
    if not output_times:
        return True
    newest_source = max([path_mtime(path) for path in sources if path] or [0.0])
    return newest_source > min(output_times)


def ensure_plotting():
    with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stderr(devnull):
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "figure.dpi": 160,
            "savefig.dpi": 220,
            "font.family": "DejaVu Sans",
            "axes.edgecolor": "#263241",
            "axes.labelcolor": "#263241",
            "axes.titleweight": "bold",
            "axes.titlesize": 11,
            "axes.labelsize": 9,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "legend.fontsize": 8,
            "grid.color": "#d9e2ec",
            "grid.linewidth": 0.6,
            "grid.alpha": 0.7,
        }
    )
    return plt


def system_output_dir(report, report_path):
    report_path = Path(report_path).resolve()
    raw = report.get("output_dir") or str(report_path.parent)
    try:
        path = Path(raw).resolve()
    except Exception:
        path = report_path.parent.resolve()
    return path


def figures_dir(report, report_path):
    path = system_output_dir(report, report_path) / "figures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_optimizer_report(report, report_path):
    progress = report.get("aspen_progress") or {}
    candidates = []
    for key in ("optimizer_report_json", "current_run_optimizer_report_json"):
        value = progress.get(key)
        if value:
            candidates.append(Path(value))
    output = progress.get("optimization_output_dir")
    if output:
        candidates.append(Path(output) / "generic_aspen_once_optimizer.report.json")
    root = system_output_dir(report, report_path)
    candidates.extend(root.glob("aspen_optimization_once*/generic_aspen_once_optimizer.report.json"))
    existing = [path.resolve() for path in candidates if path and Path(path).exists()]
    if not existing:
        return None
    return sorted(existing, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def optimizer_rows(optimizer_report):
    data = read_json_default(optimizer_report, {})
    rows = data.get("rows") or data.get("case_results") or []
    if not isinstance(rows, list):
        rows = []
    return data, [row for row in rows if isinstance(row, dict)]


def row_value(row, *keys):
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return ""


def numeric_rows(rows, required):
    clean = []
    for row in rows:
        item = dict(row)
        ok = True
        for key in required:
            value = as_float(row.get(key), None)
            if value is None:
                ok = False
                break
            item[key] = value
        if ok:
            clean.append(item)
    return clean


def save_multi_format(fig, base_path):
    base_path = Path(base_path)
    base_path.parent.mkdir(parents=True, exist_ok=True)
    png = base_path.with_suffix(".png")
    fig.savefig(png, bbox_inches="tight", facecolor="white")
    for suffix in (".pdf", ".svg"):
        try:
            fig.savefig(base_path.with_suffix(suffix), bbox_inches="tight", facecolor="white")
        except Exception:
            pass
    return png


def system_context(report, optimizer_data=None, rows=None):
    task = report.get("separation_task") or {}
    component_a = task.get("component_a") or ""
    component_b = task.get("component_b") or ""
    target = task.get("target_product") or ""
    selected_code = (
        report.get("selected_molecular_solvent_code")
        or task.get("molecular_selected_solvent_code")
        or task.get("selected_solvent_code")
        or ""
    )
    selected_label = selected_code
    progress = report.get("aspen_progress") or {}
    if progress.get("selected_molecular_solvent_label"):
        selected_label = progress.get("selected_molecular_solvent_label")
    topsis_id = ""
    optimized_label = ""
    optimized_code = ""
    if optimizer_data:
        topsis_id = optimizer_data.get("topsis_best_case_id") or (optimizer_data.get("multi_objective_optimization") or {}).get("topsis_best_case_id") or ""
    if rows:
        top_row = None
        if topsis_id:
            top_row = next((row for row in rows if str(row.get("case_id")) == str(topsis_id)), None)
        top_row = top_row or next((row for row in rows if row.get("aspen_component_id") or row.get("solvent_name")), None)
        if top_row:
            optimized_code = top_row.get("aspen_component_id") or top_row.get("solvent_code") or ""
            optimized_name = top_row.get("solvent_name") or ""
            optimized_label = " / ".join(part for part in (optimized_code, optimized_name) if part)
    if not optimized_label:
        optimized_code = progress.get("current_run_topsis_best_aspen_component_id") or progress.get("topsis_best_aspen_component_id") or ""
        optimized_name = progress.get("current_run_topsis_best_solvent_name") or progress.get("topsis_best_solvent_name") or ""
        optimized_label = " / ".join(part for part in (optimized_code, optimized_name) if part)
    return {
        "task_id": task.get("task_id") or "",
        "system_label": "{} / {}".format(component_a or "-", component_b or "-"),
        "target_product": target,
        "target_label": "{} / {} -> {}".format(component_a or "-", component_b or "-", target or "-"),
        "property_method": task.get("property_method") or "",
        "selected_solvent_code": selected_code,
        "selected_solvent_label": selected_label or selected_code,
        "optimized_solvent_code": optimized_code,
        "optimized_solvent_label": optimized_label or selected_label or selected_code,
        "topsis_best_case_id": topsis_id or progress.get("current_run_topsis_best_case_id") or progress.get("topsis_best_case_id") or "",
    }


def figure_item(fig_id, title, stage, kind, image_path, source_paths, description, data_points=0, keywords=None, status="ready", context=None):
    image_path = Path(image_path)
    context = context or {}
    return {
        "id": fig_id,
        "title": title,
        "stage": stage,
        "kind": kind,
        "status": status,
        "image_path": str(image_path.resolve()),
        "image_file": image_path.name,
        "source_paths": [str(Path(path).resolve()) for path in source_paths if path],
        "description": description,
        "data_points": int(data_points or 0),
        "keywords": keywords or [],
        "task_id": context.get("task_id") or "",
        "system_label": context.get("system_label") or "",
        "target_product": context.get("target_product") or "",
        "target_label": context.get("target_label") or "",
        "property_method": context.get("property_method") or "",
        "selected_solvent_code": context.get("selected_solvent_code") or "",
        "selected_solvent_label": context.get("selected_solvent_label") or "",
        "optimized_solvent_code": context.get("optimized_solvent_code") or "",
        "optimized_solvent_label": context.get("optimized_solvent_label") or "",
        "molecular_pair_label": context.get("molecular_pair_label") or "",
        "component_label": context.get("component_label") or "",
        "solvent_label": context.get("solvent_label") or "",
        "topsis_best_case_id": context.get("topsis_best_case_id") or "",
        "updated_at": now_iso(),
        "version": FIGURE_VERSION,
    }


def plot_aspen_pareto(fig_dir, optimizer_report, data, rows, context=None):
    plt = ensure_plotting()
    y_key = None
    for key in ("carbon_emission_ton_per_year", "objective_carbon_emission", "total_reboiler_duty", "objective_energy", "Qreb_1", "Qreb_2"):
        if any(as_float(row.get(key), None) is not None for row in rows):
            y_key = key
            break
    if not y_key:
        return None
    candidates = []
    for row in rows:
        tac = as_float(row.get("TAC"), None)
        y_value = as_float(row.get(y_key), None)
        if tac is None or y_value is None:
            continue
        if not (truthy(row.get("multi_objective_feasible")) or truthy(row.get("feasible")) or str(row.get("pass_or_fail", "")).upper() == "PASS"):
            continue
        item = dict(row)
        item["_tac"] = tac
        item["_y"] = y_value
        candidates.append(item)
    if len(candidates) < 2:
        return None
    pareto = [row for row in candidates if truthy(row.get("pareto_front"))]
    topsis_id = data.get("topsis_best_case_id") or (data.get("multi_objective_optimization") or {}).get("topsis_best_case_id")
    topsis = next((row for row in candidates if str(row.get("case_id")) == str(topsis_id)), None)
    fig, ax = plt.subplots(figsize=(6.2, 4.4))
    ax.scatter([r["_tac"] for r in candidates], [r["_y"] for r in candidates], s=16, c="#8fa7bf", alpha=0.45, label="Feasible cases")
    if pareto:
        ax.scatter([r["_tac"] for r in pareto], [r["_y"] for r in pareto], s=30, c="#2266aa", edgecolors="#18324a", linewidths=0.4, label="Pareto front")
    if topsis:
        ax.scatter([topsis["_tac"]], [topsis["_y"]], marker="*", s=160, c="#d64f3f", edgecolors="#7f1d1d", linewidths=0.6, label="TOPSIS best")
        ax.annotate(str(topsis.get("case_id") or "TOPSIS"), (topsis["_tac"], topsis["_y"]), xytext=(6, 6), textcoords="offset points", fontsize=7)
    ax.set_title("Aspen multi-objective trade-off")
    ax.set_xlabel("TAC")
    ax.set_ylabel("CO2 ton/year" if y_key in {"carbon_emission_ton_per_year", "objective_carbon_emission"} else y_key)
    ax.grid(True)
    ax.legend(frameon=False, loc="best")
    png = save_multi_format(fig, fig_dir / "aspen_pareto_front")
    plt.close(fig)
    return figure_item(
        "aspen_pareto_front",
        "Aspen Pareto 前沿图",
        "aspen_optimization",
        "scatter",
        png,
        [optimizer_report],
        "显示可行优化点、Pareto 前沿和当前 TOPSIS 最优点的位置。",
        data_points=len(candidates),
        keywords=["pareto", "front", "topsis", "多目标", "前沿", "最优"],
        context=context,
    )


def plot_topsis_ranking(fig_dir, optimizer_report, data, rows, context=None):
    plt = ensure_plotting()
    ranked = []
    for row in rows:
        rank = as_float(row.get("topsis_rank"), None)
        score = as_float(row.get("topsis_score"), None)
        if rank is None or score is None:
            continue
        ranked.append((rank, score, row))
    ranked = sorted(ranked, key=lambda item: item[0])[:12]
    if not ranked:
        return None
    labels = [str(item[2].get("case_id") or int(item[0]))[-8:] for item in ranked]
    scores = [item[1] for item in ranked]
    colors = ["#d64f3f" if idx == 0 else "#2f73b8" for idx, _ in enumerate(ranked)]
    fig, ax = plt.subplots(figsize=(7.0, 4.0))
    bars = ax.bar(range(len(scores)), scores, color=colors, edgecolor="#203040", linewidth=0.4)
    ax.set_title("TOPSIS ranking")
    ax.set_ylabel("TOPSIS score")
    ax.set_xticks(range(len(scores)))
    ax.set_xticklabels(labels, rotation=35, ha="right")
    ax.grid(True, axis="y")
    for bar, score in zip(bars, scores):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), "{:.3f}".format(score), ha="center", va="bottom", fontsize=7)
    png = save_multi_format(fig, fig_dir / "aspen_topsis_ranking")
    plt.close(fig)
    return figure_item(
        "aspen_topsis_ranking",
        "TOPSIS 排名图",
        "aspen_optimization",
        "bar",
        png,
        [optimizer_report],
        "列出当前 TOPSIS 得分最高的优化点，用于解释为什么推荐该 case。",
        data_points=len(ranked),
        keywords=["topsis", "ranking", "排名", "推荐", "最优"],
        context=context,
    )


def plot_convergence(fig_dir, optimizer_report, data, rows, context=None):
    plt = ensure_plotting()
    by_gen = {}
    for row in rows:
        gen = as_int(row.get("generation"), None)
        if gen is None:
            continue
        item = by_gen.setdefault(gen, {"total": 0, "pass": 0, "feasible": 0, "pareto": 0, "best_tac": None})
        item["total"] += 1
        if str(row.get("pass_or_fail") or "").upper() == "PASS":
            item["pass"] += 1
        if truthy(row.get("multi_objective_feasible")) or truthy(row.get("feasible")):
            item["feasible"] += 1
        if truthy(row.get("pareto_front")):
            item["pareto"] += 1
        tac = as_float(row.get("TAC"), None)
        if tac is not None:
            item["best_tac"] = tac if item["best_tac"] is None else min(item["best_tac"], tac)
    if not by_gen:
        return None
    generations = sorted(by_gen)
    cumulative_total = []
    cumulative_pass = []
    cumulative_feasible = []
    cumulative_pareto = []
    total = passed = feasible = pareto = 0
    best_tac = []
    current_best = None
    for gen in generations:
        row = by_gen[gen]
        total += row["total"]
        passed += row["pass"]
        feasible += row["feasible"]
        pareto += row["pareto"]
        if row["best_tac"] is not None:
            current_best = row["best_tac"] if current_best is None else min(current_best, row["best_tac"])
        cumulative_total.append(total)
        cumulative_pass.append(passed)
        cumulative_feasible.append(feasible)
        cumulative_pareto.append(pareto)
        best_tac.append(current_best if current_best is not None else float("nan"))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.0, 3.8))
    ax1.plot(generations, cumulative_total, color="#6b7f95", label="Cases", linewidth=1.8)
    ax1.plot(generations, cumulative_pass, color="#1b8a5a", label="PASS", linewidth=1.8)
    ax1.plot(generations, cumulative_feasible, color="#2f73b8", label="Feasible", linewidth=1.8)
    ax1.plot(generations, cumulative_pareto, color="#d48a20", label="Pareto", linewidth=1.8)
    ax1.set_title("Cumulative optimization evidence")
    ax1.set_xlabel("Generation")
    ax1.set_ylabel("Count")
    ax1.grid(True)
    ax1.legend(frameon=False)
    ax2.plot(generations, best_tac, color="#d64f3f", linewidth=1.8)
    ax2.set_title("Best TAC by generation")
    ax2.set_xlabel("Generation")
    ax2.set_ylabel("Best TAC")
    ax2.grid(True)
    png = save_multi_format(fig, fig_dir / "aspen_convergence")
    plt.close(fig)
    return figure_item(
        "aspen_convergence",
        "Aspen 收敛过程图",
        "aspen_optimization",
        "trend",
        png,
        [optimizer_report],
        "展示 case、PASS、可行解、Pareto 点随代数积累，以及最低 TAC 的改善过程。",
        data_points=len(rows),
        keywords=["convergence", "generation", "收敛", "代数", "进度", "case", "PASS"],
        context=context,
    )


def plot_convergence_diagnostics(fig_dir, optimizer_report, data, rows, context=None):
    plt = ensure_plotting()
    convergence = data.get("convergence") or {}
    history = convergence.get("history") or []
    if not isinstance(history, list):
        history = []
    snapshots = []
    for item in history:
        if not isinstance(item, dict):
            continue
        generation = as_int(item.get("generation"), None)
        score = as_float(item.get("topsis_best_score"), None)
        objectives = item.get("best_objectives") or {}
        energy = as_float(objectives.get("objective_energy"), None)
        solvent = as_float(objectives.get("objective_solvent_circulation"), None)
        stages = as_float(objectives.get("objective_column_stages"), None)
        if generation is None or score is None:
            continue
        snapshots.append(
            {
                "generation": generation,
                "score": score,
                "energy": energy,
                "solvent": solvent,
                "stages": stages,
                "pareto": as_int(item.get("pareto_front_case_count"), 0),
                "feasible": as_int(item.get("feasible_case_count"), 0),
                "stagnant": as_int(item.get("stagnant_generations"), 0),
                "improved": truthy(item.get("improved")),
            }
        )
    if len(snapshots) < 2:
        return None
    snapshots = sorted(snapshots, key=lambda item: item["generation"])
    generations = [item["generation"] for item in snapshots]
    scores = [item["score"] for item in snapshots]
    deltas = [0.0]
    for prev, current in zip(scores, scores[1:]):
        deltas.append(current - prev)
    score_epsilon = as_float(convergence.get("score_epsilon"), 0.002) or 0.002
    patience = as_int(convergence.get("patience"), 0)
    min_generations = as_int(convergence.get("min_generations"), 0)

    def normalized_series(key):
        values = [item.get(key) for item in snapshots]
        valid = [value for value in values if value is not None and value > 0]
        if not valid:
            return None
        base = valid[0]
        return [value / base if value is not None and base else float("nan") for value in values]

    energy_norm = normalized_series("energy")
    solvent_norm = normalized_series("solvent")
    stages_norm = normalized_series("stages")

    fig, axes = plt.subplots(2, 2, figsize=(9.4, 6.4), constrained_layout=True)
    ax = axes[0][0]
    ax.plot(generations, scores, marker="o", color="#1f6fb2", linewidth=1.8)
    ax.set_title("Best TOPSIS score", fontsize=10)
    ax.set_ylabel("Score")
    ax.grid(True)
    ax.text(
        0.02,
        0.04,
        "stop rule: min {} gen, stagnant {}/{}".format(min_generations, snapshots[-1]["stagnant"], patience),
        transform=ax.transAxes,
        fontsize=7,
        color="#405266",
    )

    ax = axes[0][1]
    colors = ["#1b8a5a" if value >= score_epsilon else "#d48a20" for value in deltas]
    colors[0] = "#9aaabd"
    ax.bar(generations, deltas, color=colors, edgecolor="#263241", linewidth=0.3)
    ax.axhline(score_epsilon, color="#b3261e", linestyle="--", linewidth=1.0, label="score epsilon")
    ax.axhline(0, color="#263241", linewidth=0.6)
    ax.set_title("Per-generation TOPSIS improvement", fontsize=10)
    ax.set_ylabel("Delta score")
    ax.grid(True, axis="y")
    ax.legend(frameon=False)

    ax = axes[1][0]
    plotted = False
    if energy_norm:
        ax.plot(generations, energy_norm, marker="o", color="#d64f3f", linewidth=1.6, label="Energy/TAC")
        plotted = True
    if solvent_norm:
        ax.plot(generations, solvent_norm, marker="o", color="#2f73b8", linewidth=1.6, label="Solvent circulation")
        plotted = True
    if stages_norm:
        ax.plot(generations, stages_norm, marker="o", color="#6b7f95", linewidth=1.6, label="Column stages")
        plotted = True
    ax.set_title("Normalized best objectives", fontsize=10)
    ax.set_xlabel("Generation")
    ax.set_ylabel("Relative value")
    ax.grid(True)
    if plotted:
        ax.legend(frameon=False)

    ax = axes[1][1]
    stagnant = [item["stagnant"] for item in snapshots]
    pareto = [item["pareto"] for item in snapshots]
    ax.bar(generations, stagnant, color="#d48a20", alpha=0.75, label="Stagnant generations")
    if patience:
        ax.axhline(patience, color="#b3261e", linestyle="--", linewidth=1.0, label="Patience")
    ax.set_title("Stopping counter and Pareto evidence", fontsize=10)
    ax.set_xlabel("Generation")
    ax.set_ylabel("Stagnant count")
    ax.grid(True, axis="y")
    ax2 = ax.twinx()
    ax2.plot(generations, pareto, marker="o", color="#1f6fb2", linewidth=1.6, label="Pareto count")
    ax2.set_ylabel("Pareto count")
    lines, labels = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines + lines2, labels + labels2, frameon=False, loc="best")

    png = save_multi_format(fig, fig_dir / "aspen_convergence_diagnostics")
    plt.close(fig)
    return figure_item(
        "aspen_convergence_diagnostics",
        "多目标收敛诊断图",
        "aspen_optimization",
        "trend",
        png,
        [optimizer_report],
        "展示 TOPSIS 最优得分、逐代改进量、收敛阈值、停滞计数和 Pareto 数量；这里的“误差”对应优化改进量/收敛阈值，不是实验测量误差。",
        data_points=len(snapshots),
        keywords=["convergence", "diagnostics", "epsilon", "delta", "improvement", "stagnation", "error", "收敛", "诊断", "误差", "改进量", "停滞", "阈值"],
        context=context,
    )


def plot_variable_distribution(fig_dir, optimizer_report, data, rows, context=None):
    plt = ensure_plotting()
    candidates = []
    for row in rows:
        tac = as_float(row.get("TAC"), None)
        if tac is None:
            continue
        item = dict(row)
        item["_tac"] = tac
        candidates.append(item)
    if len(candidates) < 3:
        return None
    panels = [
        ("SF", "S/F"),
        ("RR1", "RR1"),
        ("RR2", "RR2"),
        ("N1", "N1 stages"),
        ("N2", "N2 stages"),
    ]
    panels = [(key, label) for key, label in panels if any(as_float(row.get(key), None) is not None for row in candidates)][:4]
    if not panels:
        return None
    fig, axes = plt.subplots(2, 2, figsize=(8.2, 6.0))
    axes = list(axes.ravel())
    for ax, (key, label) in zip(axes, panels):
        x = []
        y = []
        colors = []
        for row in candidates:
            value = as_float(row.get(key), None)
            if value is None:
                continue
            x.append(value)
            y.append(row["_tac"])
            colors.append("#2266aa" if truthy(row.get("pareto_front")) else "#9aaabd")
        ax.scatter(x, y, s=18, c=colors, alpha=0.7, edgecolors="none")
        ax.set_title("{} vs TAC".format(label))
        ax.set_xlabel(label)
        ax.set_ylabel("TAC")
        ax.grid(True)
    for ax in axes[len(panels) :]:
        ax.axis("off")
    png = save_multi_format(fig, fig_dir / "aspen_variable_distribution")
    plt.close(fig)
    return figure_item(
        "aspen_variable_distribution",
        "优化变量分布图",
        "aspen_optimization",
        "scatter_matrix",
        png,
        [optimizer_report],
        "展示关键优化变量与 TAC 的关系，Pareto 点用深蓝色突出。",
        data_points=len(candidates),
        keywords=["variable", "bounds", "range", "参数", "变量", "范围", "SF", "RR", "N1", "N2"],
        context=context,
    )


def collect_nci_figures(report, report_path, context=None):
    paths = report.get("research_task_package_paths") or {}
    figures = []
    seen = {}
    source_paths = []
    result_csv = paths.get("nci_vmd_results_csv")
    if result_csv and Path(result_csv).exists():
        source_paths.append(result_csv)
        try:
            with Path(result_csv).open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    png = row.get("render_png") or row.get("png") or ""
                    if png and Path(png).exists():
                        seen[str(Path(png).resolve())] = row
        except Exception:
            pass
    nci_dir = paths.get("nci_vmd_dir")
    if nci_dir and Path(nci_dir).exists():
        for png in list(Path(nci_dir).rglob("nci.png"))[:12]:
            seen.setdefault(str(png.resolve()), {})
    for index, png in enumerate(sorted(seen)[:12], start=1):
        path = Path(png)
        row = seen.get(png) or {}
        job_id = row.get("job_id") or path.parent.name
        component = row.get("component") or job_id.split("_")[0].upper()
        solvent_code = row.get("solvent_code") or ""
        solvent_name = row.get("solvent_name") or ""
        if not solvent_code:
            parts = job_id.split("_")
            if len(parts) >= 2:
                solvent_code = parts[1].upper()
        solvent_label = " / ".join(part for part in (solvent_code, solvent_name) if part)
        pair_label = "{} + {}".format(component or "component", solvent_label or job_id)
        nci_context = dict(context or {})
        nci_context.update({
            "molecular_pair_label": pair_label,
            "component_label": component,
            "solvent_label": solvent_label or solvent_code,
            "optimized_solvent_label": "",
            "optimized_solvent_code": "",
            "topsis_best_case_id": "",
        })
        title = "NCI/VMD 分子相互作用图：{}".format(pair_label)
        figures.append(
            figure_item(
                "nci_vmd_{:02d}".format(index),
                title,
                "molecular_evidence",
                "molecular_image",
                path,
                source_paths,
                "已有 NCI/VMD 渲染图，可用于查看候选萃取剂和组分间的弱相互作用证据。",
                data_points=1,
                keywords=["nci", "vmd", "molecular", "分子", "相互作用", "Gaussian", component, solvent_code, solvent_name, job_id],
                context=nci_context,
            )
        )
    return figures


def load_existing_manifest(fig_dir):
    manifest_path = Path(fig_dir) / FIGURE_MANIFEST_NAME
    data = read_json_default(manifest_path, {})
    if not isinstance(data, dict):
        return {}
    return data


def build_manifest(report_path, report=None, force=False, kinds=None, auto_refresh_seconds=120):
    report_path = Path(report_path).resolve()
    if report is None:
        report = read_json_default(report_path, {})
    fig_dir = figures_dir(report, report_path)
    manifest_path = fig_dir / FIGURE_MANIFEST_NAME
    existing = load_existing_manifest(fig_dir)
    if (
        not force
        and not kinds
        and existing.get("figures")
        and manifest_path.exists()
        and time_since_mtime(manifest_path) < float(auto_refresh_seconds or 0)
    ):
        return existing
    requested = set(kinds or [])
    if requested:
        requested = {item.lower().strip() for item in requested if item}
    optimizer_report = find_optimizer_report(report, report_path)
    figures = []
    errors = []
    base_context = system_context(report)
    if optimizer_report:
        data, rows = optimizer_rows(optimizer_report)
        base_context = system_context(report, optimizer_data=data, rows=rows)
        outputs = [
            fig_dir / "aspen_pareto_front.png",
            fig_dir / "aspen_topsis_ranking.png",
            fig_dir / "aspen_convergence.png",
            fig_dir / "aspen_convergence_diagnostics.png",
            fig_dir / "aspen_variable_distribution.png",
        ]
        should_generate = force or newer_than_outputs([optimizer_report, report_path], outputs)
        if should_generate or requested:
            plotters = [
                ("pareto", plot_aspen_pareto),
                ("topsis", plot_topsis_ranking),
                ("convergence", plot_convergence),
                ("diagnostics", plot_convergence_diagnostics),
                ("variables", plot_variable_distribution),
            ]
            for kind, plotter in plotters:
                if requested and kind not in requested:
                    continue
                try:
                    item = plotter(fig_dir, optimizer_report, data, rows, context=base_context)
                    if item:
                        figures.append(item)
                except Exception as exc:
                    errors.append({"kind": kind, "error": "{}: {}".format(exc.__class__.__name__, exc)})
        else:
            previous = existing.get("figures") if isinstance(existing.get("figures"), list) else []
            figures.extend([item for item in previous if str(item.get("stage")) == "aspen_optimization"])
    else:
        errors.append({"kind": "aspen", "error": "optimizer report not found"})
    figures.extend(collect_nci_figures(report, report_path, context=base_context))
    by_id = {}
    for item in figures:
        if item and item.get("image_path") and Path(item["image_path"]).exists():
            by_id[item["id"]] = item
    figures = list(by_id.values())
    task = report.get("separation_task") or {}
    manifest = {
        "manifest_version": FIGURE_VERSION,
        "generated_at": now_iso(),
        "report_path": str(report_path),
        "figures_dir": str(fig_dir.resolve()),
        "task": {
            "task_id": task.get("task_id") or "",
            "component_a": task.get("component_a") or "",
            "component_b": task.get("component_b") or "",
            "target_product": task.get("target_product") or "",
            "selected_solvent_code": task.get("selected_solvent_code") or task.get("molecular_selected_solvent_code") or "",
        },
        "context": base_context,
        "optimizer_report_json": str(optimizer_report) if optimizer_report else "",
        "figure_count": len(figures),
        "figures": figures,
        "errors": errors,
    }
    write_json(manifest_path, manifest)
    return manifest


def figure_matches(item, query):
    query = str(query or "").strip().lower()
    if not query:
        return True
    text = " ".join(
        str(part or "")
        for part in [
            item.get("id"),
            item.get("title"),
            item.get("stage"),
            item.get("kind"),
            item.get("description"),
            item.get("system_label"),
            item.get("target_label"),
            item.get("selected_solvent_code"),
            item.get("selected_solvent_label"),
            item.get("optimized_solvent_code"),
            item.get("optimized_solvent_label"),
            item.get("molecular_pair_label"),
            item.get("component_label"),
            item.get("solvent_label"),
            item.get("topsis_best_case_id"),
            " ".join(item.get("keywords") or []),
        ]
    ).lower()
    aliases = {
        "图": "",
        "图片": "",
        "画": "",
        "看": "",
        "给我": "",
        "当前": "",
        "这个": "",
    }
    for old, new in aliases.items():
        query = query.replace(old, new)
    tokens = [token for token in re.split(r"\s+|,|，|。|/|\\|：|:", query) if token]
    if not tokens:
        return True
    return any(token in text for token in tokens)


def select_figures(manifest, query="", limit=3):
    figures = manifest.get("figures") or []
    if not isinstance(figures, list):
        return []
    query_text = str(query or "")
    priority = []
    q = query_text.lower()
    def has_token(token):
        return bool(re.search(r"(^|[^a-z0-9]){}([^a-z0-9]|$)".format(re.escape(token.lower())), q, re.I))
    nci_requested = bool(re.search(r"nci|vmd|分子|相互作用|gaussian", q, re.I))
    aspen_plot_requested = bool(re.search(r"pareto|前沿|多目标|topsis|排名|排行|推荐|最优|收敛|代数|进度|误差|epsilon|阈值|改进量|停滞|诊断|变量|参数|范围|边界|sf|rr|n1|n2|bound|range", q, re.I))
    diagnostic_requested = bool(re.search(r"误差|epsilon|阈值|改进量|停滞|诊断|delta|improvement|stagn", q, re.I))
    if diagnostic_requested:
        priority.append("aspen_convergence_diagnostics")
    if re.search(r"pareto|前沿|多目标", q, re.I):
        priority.append("aspen_pareto_front")
    if re.search(r"topsis|排名|排行|推荐|最优", q, re.I):
        priority.append("aspen_topsis_ranking")
    if re.search(r"收敛|代数|进度|conver", q, re.I):
        priority.append("aspen_convergence")
        priority.append("aspen_convergence_diagnostics")
    if re.search(r"变量|参数|范围|边界|sf|rr|n1|n2|bound|range", q, re.I):
        priority.append("aspen_variable_distribution")
    restricted_nci_ids = None
    if nci_requested:
        nci_items = [item for item in figures if str(item.get("id", "")).startswith("nci_vmd_")]
        solvent_filters = []
        if has_token("ec") or re.search(r"ethylene\s+carbonate|碳酸乙烯", q, re.I):
            solvent_filters.append("ec")
        if has_token("pg") or re.search(r"propylene\s+glycol|1,2-propanediol|丙二醇", q, re.I):
            solvent_filters.append("pg")
        if solvent_filters:
            filtered = []
            for item in nci_items:
                haystack = " ".join(
                    str(item.get(key) or "")
                    for key in ("molecular_pair_label", "solvent_label", "title")
                ).lower()
                if any(re.search(r"(^|[^a-z0-9]){}([^a-z0-9]|$)".format(re.escape(token)), haystack, re.I) for token in solvent_filters):
                    filtered.append(item)
            nci_items = filtered or nci_items
            restricted_nci_ids = {item.get("id") for item in nci_items}
        priority.extend([item.get("id") for item in nci_items])
    selected = []
    by_id = {item.get("id"): item for item in figures}
    for fig_id in priority:
        item = by_id.get(fig_id)
        if item and item not in selected:
            selected.append(item)
    if selected and (aspen_plot_requested or nci_requested):
        return selected[:limit]
    for item in figures:
        if len(selected) >= limit:
            break
        if item in selected:
            continue
        if nci_requested and not aspen_plot_requested and not str(item.get("id", "")).startswith("nci_vmd_"):
            continue
        if restricted_nci_ids is not None and str(item.get("id", "")).startswith("nci_vmd_") and item.get("id") not in restricted_nci_ids:
            continue
        if figure_matches(item, query_text):
            selected.append(item)
    if not selected and figures:
        selected = figures[:limit]
    return selected[:limit]


def make_dialogue_figure_reply(manifest, query="", limit=3):
    selected = select_figures(manifest, query=query, limit=limit)
    if not selected:
        return {
            "reply": "我还没有可展示的图。当前会先检查 Aspen optimizer report 和 NCI/VMD 输出；如果结果文件还没形成，就不会硬画。",
            "figures": [],
        }
    names = "、".join(item.get("title") or item.get("id") for item in selected)
    context = manifest.get("context") or {}
    target = context.get("target_label") or ""
    pairs = [item.get("molecular_pair_label") for item in selected if item.get("molecular_pair_label")]
    pairs = list(dict.fromkeys(pairs))[:4]
    solvent = context.get("optimized_solvent_label") or context.get("selected_solvent_label") or ""
    identity_parts = [target]
    if pairs:
        identity_parts.append("分子对 {}".format("、".join(pairs)))
    elif solvent:
        identity_parts.append("萃取剂 {}".format(solvent))
    identity = "；".join(part for part in identity_parts if part)
    reply = "可以，我调出了 {}。{}。这些图都来自当前体系文件夹的真实结果文件，路径已写入 figure_manifest.json。".format(
        names,
        identity or "图卡已带体系和萃取剂身份",
    )
    return {"reply": reply, "figures": selected}


def main():
    parser = argparse.ArgumentParser(description="Generate figure manifest for a system report.")
    parser.add_argument("report_path")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--query", default="")
    args = parser.parse_args()
    manifest = build_manifest(args.report_path, force=args.force)
    result = make_dialogue_figure_reply(manifest, args.query)
    print(json.dumps({"manifest": manifest, **result}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
