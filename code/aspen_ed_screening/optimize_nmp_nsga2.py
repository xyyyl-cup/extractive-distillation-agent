# -*- coding: utf-8 -*-
"""
NSGA-II optimizer for the closed-loop NMP extractive-distillation Aspen model.

This script optimizes all first-version operating and structural variables:
SF, RR1, D1, RR2, D2, N1, NF1, NS1, N2, and NF2.

Aspen Tree paths are still read only through variable_map.py via
aspen_runner.AspenCaseRunner.
"""

import argparse
import gc
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

import pandas as pd

try:
    import variable_map as vm
    import tac_model
    from aspen_runner import AspenCaseRunner, safe_float
    from run_screening import read_solvent_ids
except ImportError:  # pragma: no cover
    from . import variable_map as vm
    from . import tac_model
    from .aspen_runner import AspenCaseRunner, safe_float
    from .run_screening import read_solvent_ids


CONTINUOUS_VARIABLES = ("SF", "RR1", "D1", "RR2", "D2")
INTEGER_VARIABLES = ("N1", "NF1", "NS1", "N2", "NF2")
VARIABLES = CONTINUOUS_VARIABLES + INTEGER_VARIABLES

DEFAULT_RANGES = OrderedDict(
    [
        ("SF", (3.0, 4.1)),
        ("RR1", (11.0, 17.0)),
        ("D1", (8.972, 8.990)),
        ("RR2", (2.2, 4.8)),
        ("D2", (1.08, 1.30)),
        ("N1", (55, 90)),
        ("NF1", (35, 75)),
        ("NS1", (20, 55)),
        ("N2", (4, 12)),
        ("NF2", (2, 8)),
    ]
)

BASE_GENOME = OrderedDict(
    [
        ("SF", 3.3387),
        ("RR1", 12.1788),
        ("D1", 8.980945),
        ("RR2", 2.7604),
        ("D2", 1.222392),
        ("N1", 70),
        ("NF1", 52),
        ("NS1", 34),
        ("N2", 5),
        ("NF2", 2),
    ]
)

RESULT_COLUMNS = [
    "case_id",
    "generation",
    "source",
    "from_cache",
    "solvent_name",
    "aspen_component_id",
    "SF",
    "RR1",
    "D1",
    "RR2",
    "D2",
    "N1",
    "NF1",
    "NS1",
    "N2",
    "NF2",
    "converged",
    "feasible",
    "constraint_violation",
    "rank",
    "crowding",
    "x_C3F8_top1",
    "recovery_C3F8",
    "x_C3F6_top2",
    "recovery_C3F6",
    "solvent_loss_top1",
    "solvent_loss_top2",
    "solvent_loss_total",
    "Qreb_1",
    "Qreb_2",
    "Qcond_1",
    "Qcond_2",
    "total_reboiler_duty",
    "total_reboiler_duty_kW",
    "B1_diameter_m",
    "B2_diameter_m",
    "B1_height_m",
    "B2_height_m",
    "B1_top_temp",
    "B1_bottom_temp",
    "B1_reboiler_outlet_temp",
    "B2_top_temp",
    "B2_bottom_temp",
    "B2_reboiler_outlet_temp",
    "feed_heater_duty",
    "total_condenser_duty_kW",
    "B1_reboiler_area_m2",
    "B2_reboiler_area_m2",
    "B1_condenser_area_m2",
    "B2_condenser_area_m2",
    "feed_exchanger_area_m2",
    "heat_exchanger_area_m2",
    "B1_column_shell_cost",
    "B2_column_shell_cost",
    "column_capital_cost",
    "heat_exchanger_capital_cost",
    "total_capital_cost",
    "annualized_capital_cost",
    "reboiler_energy_cost",
    "condenser_energy_cost",
    "feed_exchanger_energy_cost",
    "energy_cost",
    "solvent_molecular_weight_kg_per_kmol",
    "solvent_price_usd_per_kg",
    "solvent_makeup_kg_per_year",
    "solvent_makeup_cost",
    "operating_cost",
    "TAC",
    "tac_error",
    "obj_recovery_loss",
    "obj_reboiler_duty",
    "obj_tac",
    "obj_energy_cost",
    "obj_solvent_loss",
    "decision_score",
    "pass_or_fail",
    "score",
    "error_message",
]


def as_bool(value):
    if value is True:
        return True
    if value is False or value is None:
        return False
    text = str(value).strip().upper()
    return text in ("TRUE", "1", "YES", "Y", "PASS", "OK")


def finite_float(value):
    value = safe_float(value)
    if value is None or not math.isfinite(value):
        return None
    return value


def crowding_value(value):
    try:
        value = float(value)
    except Exception:
        return 0.0
    if math.isnan(value):
        return 0.0
    return value


def parse_range(text, default, integer=False):
    if text is None:
        low, high = default
    else:
        parts = [item.strip() for item in str(text).split(",") if item.strip()]
        if len(parts) != 2:
            raise ValueError("Range must be min,max; got {!r}".format(text))
        low, high = float(parts[0]), float(parts[1])
    if high <= low:
        raise ValueError("Range max must be greater than min; got {!r}".format(text))
    if integer:
        low, high = int(round(low)), int(round(high))
        if high <= low:
            raise ValueError("Integer range is too narrow; got {!r}".format(text))
    return low, high


def clip(value, low, high):
    return min(max(value, low), high)


def rounded_continuous(name, value):
    if name in ("D1", "D2"):
        return round(float(value), 6)
    return round(float(value), 4)


def genome_key(genome):
    return tuple(genome[name] for name in VARIABLES)


def parse_ranges(args):
    return OrderedDict(
        [
            ("SF", parse_range(args.sf_range, DEFAULT_RANGES["SF"])),
            ("RR1", parse_range(args.rr1_range, DEFAULT_RANGES["RR1"])),
            ("D1", parse_range(args.d1_range, DEFAULT_RANGES["D1"])),
            ("RR2", parse_range(args.rr2_range, DEFAULT_RANGES["RR2"])),
            ("D2", parse_range(args.d2_range, DEFAULT_RANGES["D2"])),
            ("N1", parse_range(args.n1_range, DEFAULT_RANGES["N1"], integer=True)),
            ("NF1", parse_range(args.nf1_range, DEFAULT_RANGES["NF1"], integer=True)),
            ("NS1", parse_range(args.ns1_range, DEFAULT_RANGES["NS1"], integer=True)),
            ("N2", parse_range(args.n2_range, DEFAULT_RANGES["N2"], integer=True)),
            ("NF2", parse_range(args.nf2_range, DEFAULT_RANGES["NF2"], integer=True)),
        ]
    )


def repair_genome(raw, ranges, enforce_solvent_above_feed=True):
    genome = OrderedDict()
    for name in CONTINUOUS_VARIABLES:
        low, high = ranges[name]
        genome[name] = rounded_continuous(name, clip(float(raw[name]), low, high))

    for name in INTEGER_VARIABLES:
        low, high = ranges[name]
        genome[name] = int(round(clip(float(raw[name]), low, high)))

    genome["N1"] = max(3, genome["N1"])
    genome["N2"] = max(3, genome["N2"])

    genome["NF1"] = int(clip(genome["NF1"], 2, max(2, genome["N1"] - 1)))
    genome["NS1"] = int(clip(genome["NS1"], 2, max(2, genome["N1"] - 1)))
    if enforce_solvent_above_feed and genome["NF1"] > 2:
        genome["NS1"] = int(clip(genome["NS1"], 2, genome["NF1"] - 1))
    genome["NF2"] = int(clip(genome["NF2"], 2, max(2, genome["N2"] - 1)))
    return genome


def random_genome(rng, ranges, enforce_solvent_above_feed=True):
    raw = OrderedDict()
    for name in CONTINUOUS_VARIABLES:
        low, high = ranges[name]
        raw[name] = rng.uniform(low, high)
    for name in INTEGER_VARIABLES:
        low, high = ranges[name]
        raw[name] = rng.randint(int(low), int(high))
    return repair_genome(raw, ranges, enforce_solvent_above_feed)


def row_to_genome(row, ranges, enforce_solvent_above_feed=True):
    raw = OrderedDict()
    for name in VARIABLES:
        value = row.get(name)
        if value is None or str(value).strip() == "":
            value = BASE_GENOME[name]
        try:
            if value != value:
                value = BASE_GENOME[name]
        except Exception:
            pass
        raw[name] = value
    return repair_genome(raw, ranges, enforce_solvent_above_feed)


def load_seed_genomes(path, sheet, seed_count, ranges, enforce_solvent_above_feed):
    if not path:
        return []
    seed_path = Path(path)
    if not seed_path.exists():
        return []
    try:
        df = pd.read_excel(str(seed_path), sheet_name=sheet)
    except Exception:
        df = pd.read_excel(str(seed_path))
    if df.empty:
        return []
    if "decision_score" in df.columns:
        df = df.sort_values("decision_score", ascending=False)
    elif "total_reboiler_duty" in df.columns:
        df = df.sort_values("total_reboiler_duty", ascending=True)
    genomes = []
    seen = set()
    for _, row in df.iterrows():
        if any(name not in row for name in VARIABLES):
            continue
        genome = row_to_genome(row.to_dict(), ranges, enforce_solvent_above_feed)
        key = genome_key(genome)
        if key not in seen:
            genomes.append(genome)
            seen.add(key)
        if len(genomes) >= seed_count:
            break
    return genomes


def initial_population(args, ranges, rng):
    population = []
    seen = set()

    seed_genomes = load_seed_genomes(
        args.seed_from,
        args.seed_sheet,
        args.seed_count,
        ranges,
        not args.allow_solvent_below_feed,
    )
    base = repair_genome(BASE_GENOME, ranges, not args.allow_solvent_below_feed)
    for genome in [base] + seed_genomes:
        key = genome_key(genome)
        if key not in seen:
            population.append({"genome": genome, "source": "seed"})
            seen.add(key)
        if len(population) >= args.population_size:
            return population

    attempts = 0
    while len(population) < args.population_size:
        genome = random_genome(rng, ranges, not args.allow_solvent_below_feed)
        key = genome_key(genome)
        attempts += 1
        if key in seen and attempts < args.population_size * 100:
            continue
        population.append({"genome": genome, "source": "random"})
        seen.add(key)
    return population


def genome_to_case(genome, case_id, args):
    case = OrderedDict()
    case["case_id"] = case_id
    case["solvent_name"] = args.solvent_name
    case["aspen_component_id"] = args.aspen_component_id
    for name in VARIABLES:
        case[name] = genome[name]
    return case


def solvent_loss_total(record):
    loss_1 = finite_float(record.get("solvent_loss_top1")) or 0.0
    loss_2 = finite_float(record.get("solvent_loss_top2")) or 0.0
    return loss_1 + loss_2


def evaluate_constraints(record, purity_target, max_duty):
    physical_tolerance = 1.0e-6
    violation = 0.0
    if not as_bool(record.get("converged")):
        violation += 1.0

    purity = finite_float(record.get("x_C3F8_top1"))
    if purity is None:
        violation += 1.0
    elif purity < -physical_tolerance or purity > 1.0 + physical_tolerance:
        violation += 1.0
    elif purity < purity_target:
        violation += max(0.0, (purity_target - purity) * 1.0e5)

    recovery = finite_float(record.get("recovery_C3F8"))
    if recovery is None:
        violation += 1.0
    elif recovery < -physical_tolerance or recovery > 1.0 + physical_tolerance:
        violation += 1.0

    for name in ("Qreb_1", "Qreb_2"):
        reboiler_duty = finite_float(record.get(name))
        if reboiler_duty is None or reboiler_duty <= 0.0:
            violation += 1.0

    duty = finite_float(record.get("total_reboiler_duty"))
    if duty is None or duty <= 0.0:
        violation += 1.0
    elif max_duty is not None and duty > max_duty:
        violation += max(0.0, duty / max_duty - 1.0)

    tac = finite_float(record.get("TAC"))
    if tac is None or tac <= 0.0:
        violation += 1.0

    for name in ("solvent_loss_top1", "solvent_loss_top2", "solvent_loss_total"):
        solvent_loss = finite_float(record.get(name))
        if solvent_loss is not None and solvent_loss < -physical_tolerance:
            violation += 1.0

    return violation


def add_objectives(record, purity_target, max_duty, solvent_economics=None):
    record = dict(record)
    duty = finite_float(record.get("total_reboiler_duty"))
    recovery = finite_float(record.get("recovery_C3F8"))
    loss = solvent_loss_total(record)
    record["solvent_loss_total"] = loss
    if as_bool(record.get("converged")):
        record.update(tac_model.calculate_tac(record, solvent_economics))
    else:
        record["TAC"] = None
        record["tac_error"] = "Skipped TAC because Aspen did not converge."
    tac = finite_float(record.get("TAC"))
    energy_cost = finite_float(record.get("energy_cost"))
    violation = evaluate_constraints(record, purity_target, max_duty)

    record["total_reboiler_duty_kW"] = None if duty is None else duty * vm.CAL_PER_SEC_TO_KW
    record["constraint_violation"] = violation
    record["feasible"] = violation <= 1.0e-12
    record["obj_recovery_loss"] = None if recovery is None else 1.0 - recovery
    record["obj_reboiler_duty"] = duty
    record["obj_tac"] = tac
    record["obj_energy_cost"] = energy_cost
    record["obj_solvent_loss"] = loss
    record["rank"] = None
    record["crowding"] = 0.0
    record["decision_score"] = None
    return record


def objective_values(record):
    return [
        finite_float(record.get("obj_recovery_loss")),
        finite_float(record.get("obj_tac")),
        finite_float(record.get("obj_energy_cost")),
    ]


def dominates(a, b):
    a_feasible = bool(a.get("feasible"))
    b_feasible = bool(b.get("feasible"))
    if a_feasible and not b_feasible:
        return True
    if b_feasible and not a_feasible:
        return False

    if not a_feasible and not b_feasible:
        a_violation = finite_float(a.get("constraint_violation")) or float("inf")
        b_violation = finite_float(b.get("constraint_violation")) or float("inf")
        return a_violation < b_violation

    a_values = objective_values(a)
    b_values = objective_values(b)
    if any(value is None for value in a_values + b_values):
        return False
    no_worse = all(a_value <= b_value for a_value, b_value in zip(a_values, b_values))
    strictly_better = any(a_value < b_value for a_value, b_value in zip(a_values, b_values))
    return no_worse and strictly_better


def non_dominated_sort(records):
    dominates_list = {i: [] for i in range(len(records))}
    dominated_count = {i: 0 for i in range(len(records))}
    fronts = [[]]

    for i, row_i in enumerate(records):
        for j, row_j in enumerate(records):
            if i == j:
                continue
            if dominates(row_i, row_j):
                dominates_list[i].append(j)
            elif dominates(row_j, row_i):
                dominated_count[i] += 1
        if dominated_count[i] == 0:
            records[i]["rank"] = 0
            fronts[0].append(i)

    current = 0
    while fronts[current]:
        next_front = []
        for i in fronts[current]:
            for j in dominates_list[i]:
                dominated_count[j] -= 1
                if dominated_count[j] == 0:
                    records[j]["rank"] = current + 1
                    next_front.append(j)
        current += 1
        fronts.append(next_front)

    if not fronts[-1]:
        fronts.pop()
    return fronts


def assign_crowding(records, front):
    if not front:
        return
    if len(front) <= 2:
        for idx in front:
            records[idx]["crowding"] = float("inf")
        return

    for idx in front:
        records[idx]["crowding"] = 0.0

    objective_names = ["obj_recovery_loss", "obj_tac", "obj_energy_cost"]
    for objective in objective_names:
        ordered = sorted(
            front,
            key=lambda idx: (
                finite_float(records[idx].get(objective)) is None,
                finite_float(records[idx].get(objective)) or float("inf"),
            ),
        )
        records[ordered[0]]["crowding"] = float("inf")
        records[ordered[-1]]["crowding"] = float("inf")
        low = finite_float(records[ordered[0]].get(objective))
        high = finite_float(records[ordered[-1]].get(objective))
        if low is None or high is None or abs(high - low) < 1.0e-30:
            continue
        for pos in range(1, len(ordered) - 1):
            prev_value = finite_float(records[ordered[pos - 1]].get(objective))
            next_value = finite_float(records[ordered[pos + 1]].get(objective))
            if prev_value is None or next_value is None:
                continue
            records[ordered[pos]]["crowding"] += (next_value - prev_value) / (high - low)


def rank_population(records):
    rows = [dict(row) for row in records]
    fronts = non_dominated_sort(rows)
    for front in fronts:
        assign_crowding(rows, front)
    return rows, fronts


def tournament(population, rng):
    a, b = rng.sample(population, 2)
    rank_a = a.get("rank")
    rank_b = b.get("rank")
    if rank_a != rank_b:
        sort_rank_a = rank_a if rank_a is not None else 10**9
        sort_rank_b = rank_b if rank_b is not None else 10**9
        return a if sort_rank_a < sort_rank_b else b
    crowd_a = crowding_value(a.get("crowding"))
    crowd_b = crowding_value(b.get("crowding"))
    if crowd_a != crowd_b:
        return a if crowd_a > crowd_b else b
    return a if rng.random() < 0.5 else b


def crossover_and_mutate(parent_a, parent_b, ranges, args, rng):
    child = OrderedDict()
    genome_a = parent_a["genome"]
    genome_b = parent_b["genome"]

    for name in CONTINUOUS_VARIABLES:
        low, high = ranges[name]
        if rng.random() < args.crossover_probability:
            alpha = rng.uniform(-0.25, 1.25)
            value = alpha * genome_a[name] + (1.0 - alpha) * genome_b[name]
        else:
            value = genome_a[name] if rng.random() < 0.5 else genome_b[name]
        if rng.random() < args.mutation_probability:
            value += rng.gauss(0.0, args.continuous_mutation_sigma * (high - low))
        child[name] = value

    for name in INTEGER_VARIABLES:
        low, high = ranges[name]
        if rng.random() < args.crossover_probability:
            value = genome_a[name] if rng.random() < 0.5 else genome_b[name]
        else:
            value = round((genome_a[name] + genome_b[name]) / 2.0)
        if rng.random() < args.integer_mutation_probability:
            span = max(1, int(round((high - low) * args.integer_mutation_sigma)))
            value += rng.randint(-span, span)
        child[name] = value

    return repair_genome(child, ranges, not args.allow_solvent_below_feed)


def select_next_population(records, population_size):
    ranked, fronts = rank_population(records)
    selected = []
    for front in fronts:
        front_rows = [ranked[idx] for idx in front]
        if len(selected) + len(front_rows) <= population_size:
            selected.extend(front_rows)
        else:
            front_rows = sorted(
                front_rows,
                key=lambda row: (
                    -crowding_value(row.get("crowding")),
                    genome_key(row["genome"]),
                ),
            )
            selected.extend(front_rows[: population_size - len(selected)])
            break
    return selected, ranked


def case_from_record(record):
    case = OrderedDict()
    for column in vm.CASE_COLUMNS:
        case[column] = record.get(column)
    return case


def has_current_tac_cache(record):
    if not as_bool(record.get("converged")):
        return True
    required = (
        "B1_diameter_m",
        "B2_diameter_m",
        "B1_height_m",
        "B2_height_m",
        "feed_heater_duty",
    )
    return all(finite_float(record.get(name)) is not None for name in required)


def existing_evaluations(
    output_path,
    ranges,
    enforce_solvent_above_feed,
    purity_target,
    max_duty,
    solvent_economics,
):
    path = Path(output_path)
    if not path.exists():
        return []
    try:
        df = pd.read_excel(str(path), sheet_name="all_evaluations")
    except Exception:
        return []
    rows = []
    for _, row in df.iterrows():
        record = row.to_dict()
        if not has_current_tac_cache(record):
            continue
        genome = row_to_genome(record, ranges, enforce_solvent_above_feed)
        record["genome"] = genome
        record = add_objectives(record, purity_target, max_duty, solvent_economics)
        rows.append(record)
    return rows


def default_checkpoint_path(output_path):
    output_path = Path(output_path)
    return output_path.with_suffix(".checkpoint.jsonl")


def clean_json_value(value):
    if value is None:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): clean_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json_value(item) for item in value]
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return clean_json_value(value.item())
    return str(value)


def checkpoint_payload(record):
    payload = {column: clean_json_value(record.get(column)) for column in RESULT_COLUMNS}
    genome = record.get("genome") or {name: record.get(name) for name in VARIABLES}
    payload["genome"] = clean_json_value(genome)
    payload["_checkpoint_version"] = 1
    return payload


def append_checkpoint(checkpoint_path, record):
    if not checkpoint_path:
        return
    checkpoint_path = Path(checkpoint_path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(checkpoint_payload(record), ensure_ascii=False, allow_nan=False)
    with checkpoint_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def checkpoint_evaluations(
    checkpoint_path,
    ranges,
    enforce_solvent_above_feed,
    purity_target,
    max_duty,
    solvent_economics,
):
    checkpoint_path = Path(checkpoint_path)
    if not checkpoint_path.exists():
        return []
    rows = []
    bad_lines = 0
    with checkpoint_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            record = {column: payload.get(column) for column in RESULT_COLUMNS if column in payload}
            genome_data = payload.get("genome") or {name: payload.get(name) for name in VARIABLES}
            try:
                genome = row_to_genome(genome_data, ranges, enforce_solvent_above_feed)
            except Exception:
                bad_lines += 1
                continue
            record["genome"] = genome
            if not has_current_tac_cache(record):
                continue
            record = add_objectives(record, purity_target, max_duty, solvent_economics)
            rows.append(record)
    if bad_lines:
        print("Skipped {} unreadable checkpoint line(s).".format(bad_lines))
    return rows


def load_existing_evaluations(
    output_path,
    checkpoint_path,
    ranges,
    enforce_solvent_above_feed,
    purity_target,
    max_duty,
    solvent_economics,
):
    checkpoint_rows = (
        checkpoint_evaluations(
            checkpoint_path,
            ranges,
            enforce_solvent_above_feed,
            purity_target,
            max_duty,
            solvent_economics,
        )
        if checkpoint_path
        else []
    )
    if checkpoint_rows:
        return checkpoint_rows, str(checkpoint_path)
    workbook_rows = existing_evaluations(
        output_path,
        ranges,
        enforce_solvent_above_feed,
        purity_target,
        max_duty,
        solvent_economics,
    )
    return workbook_rows, str(output_path) if workbook_rows else ""


def case_number(case_id, prefix):
    text = str(case_id or "")
    if prefix and text.startswith(prefix):
        text = text[len(prefix) :]
    try:
        return int(text)
    except Exception:
        return None


def max_case_number(records, prefix):
    values = [case_number(record.get("case_id"), prefix) for record in records]
    values = [value for value in values if value is not None]
    return max(values) if values else 0


def format_case_id(prefix, number):
    return "{}{:06d}".format(prefix, number)


def replay_record(individual, generation, context):
    records = context.get("replay_records") or []
    index = context.get("replay_index", 0)
    if index >= len(records):
        return None

    expected = records[index]
    expected_genome = expected.get("genome")
    expected_generation_value = finite_float(expected.get("generation"))
    expected_generation = int(expected_generation_value) if expected_generation_value is not None else -1
    if genome_key(expected_genome) == genome_key(individual["genome"]) and expected_generation == generation:
        context["replay_index"] = index + 1
        record = dict(expected)
        record["genome"] = individual["genome"]
        record["_replayed"] = True
        return record

    raise ValueError(
        "Checkpoint does not match this run at record {}. "
        "Use the same seed/ranges/population settings, or start with a new output/checkpoint.".format(index + 1)
    )


def aspen_process_ids():
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq AspenPlus.exe", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception:
        return set()

    pids = set()
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("INFO:"):
            continue
        parts = [part.strip().strip('"') for part in line.split(",")]
        if len(parts) >= 2:
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def kill_process_tree(pid):
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass


def kill_aspen_pids(pids):
    for pid in sorted(set(pids)):
        kill_process_tree(pid)


def failed_case_result(case, message):
    result = OrderedDict((column, None) for column in vm.RESULT_COLUMNS)
    for key, value in case.items():
        result[key] = value
    result["converged"] = False
    result["pass_or_fail"] = "FAIL"
    result["error_message"] = message
    result["solvent_loss_total"] = 0.0
    return result


def run_case_in_subprocess(case, solvent_ids, args):
    timeout = getattr(args, "case_timeout_seconds", None)
    if timeout is None or timeout <= 0:
        timeout = getattr(vm, "RUN_TIMEOUT_SECONDS", 3600)

    worker = Path(__file__).with_name("aspen_case_worker.py")
    payload = {
        "case": clean_json_value(case),
        "solvent_ids": list(solvent_ids),
        "template": str(Path(args.template)),
        "case_work_dir": str(Path(args.case_work_dir)),
        "visible": bool(args.visible),
    }

    tmp_dir = Path(args.output).parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    input_path = None
    output_path = None
    before_pids = aspen_process_ids()
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".case.json",
            prefix="aspen_case_",
            dir=str(tmp_dir),
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            input_path = Path(handle.name)

        output_path = input_path.with_suffix(".result.json")
        command = [
            sys.executable,
            str(worker),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ]
        proc = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc.pid)
            time.sleep(2)
            after_pids = aspen_process_ids()
            kill_aspen_pids(after_pids - before_pids)
            return failed_case_result(
                case,
                "Aspen worker exceeded outer timeout of {} seconds and was terminated.".format(timeout),
            )

        if proc.returncode != 0:
            return failed_case_result(
                case,
                "Aspen worker exited with return code {}.".format(proc.returncode),
            )
        if output_path is None or not output_path.exists():
            return failed_case_result(case, "Aspen worker did not write a result file.")

        with output_path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=OrderedDict)
    except Exception as exc:
        return failed_case_result(case, "Aspen worker failed before completion: {}".format(exc))
    finally:
        for path in (input_path, output_path):
            if path and path.exists():
                try:
                    path.unlink()
                except Exception:
                    pass


def evaluate_individual(individual, generation, source, context):
    key = genome_key(individual["genome"])
    replayed = replay_record(individual, generation, context)
    if replayed is not None:
        return replayed

    context["case_counter"] += 1
    case_id = format_case_id(context["args"].case_prefix, context["case_counter"])
    case = genome_to_case(individual["genome"], case_id, context["args"])

    if key in context["cache"]:
        cached = dict(context["cache"][key])
        cached.update(case)
        cached["case_id"] = case_id
        cached["generation"] = generation
        cached["source"] = source
        cached["from_cache"] = True
        cached["genome"] = individual["genome"]
        return cached

    if context["runner"] is not None:
        if context["args"].reuse_aspen_session:
            result = context["runner"].run_case_in_session(
                case,
                context["solvent_ids"],
                session_id=context["session_id"],
            )
            restart_interval = max(0, int(context["args"].session_restart_interval))
            if (
                restart_interval > 0
                and context["runner"].has_open_session
                and context["runner"].session_case_count >= restart_interval
            ):
                context["runner"].close_session()
        else:
            result = context["runner"].run_case(case, context["solvent_ids"])
    else:
        result = run_case_in_subprocess(case, context["solvent_ids"], context["args"])
    record = OrderedDict()
    record.update(case)
    record.update(result)
    record["generation"] = generation
    record["source"] = source
    record["from_cache"] = False
    record = add_objectives(
        record,
        context["args"].purity_target,
        context["max_duty"],
        context["solvent_economics"],
    )
    record["genome"] = individual["genome"]
    context["cache"][key] = dict(record)
    return record


def normalize_benefit(values):
    finite = [value for value in values if value is not None and math.isfinite(value)]
    if not finite:
        return [None for _ in values]
    low, high = min(finite), max(finite)
    if abs(high - low) < 1.0e-30:
        return [1.0 if value is not None else None for value in values]
    return [None if value is None else (value - low) / (high - low) for value in values]


def normalize_cost(values):
    finite = [value for value in values if value is not None and math.isfinite(value)]
    if not finite:
        return [None for _ in values]
    low, high = min(finite), max(finite)
    if abs(high - low) < 1.0e-30:
        return [1.0 if value is not None else None for value in values]
    return [None if value is None else (high - value) / (high - low) for value in values]


def add_decision_scores(records, args):
    rows = [dict(row) for row in records]
    feasible_indices = [idx for idx, row in enumerate(rows) if row.get("feasible")]
    if not feasible_indices:
        return rows

    recoveries = [finite_float(rows[idx].get("recovery_C3F8")) for idx in feasible_indices]
    tacs = [finite_float(rows[idx].get("TAC")) for idx in feasible_indices]
    energy_costs = [finite_float(rows[idx].get("energy_cost")) for idx in feasible_indices]

    rec_norm = normalize_benefit(recoveries)
    tac_norm = normalize_cost(tacs)
    energy_norm = normalize_cost(energy_costs)
    total_weight = args.w_recovery + args.w_tac + args.w_energy
    if total_weight <= 0:
        total_weight = 1.0

    for local_idx, row_idx in enumerate(feasible_indices):
        rows[row_idx]["decision_score"] = (
            args.w_recovery * rec_norm[local_idx]
            + args.w_tac * tac_norm[local_idx]
            + args.w_energy * energy_norm[local_idx]
        ) / total_weight
    return rows


def strip_runtime_columns(records):
    rows = []
    for record in records:
        row = {column: record.get(column) for column in RESULT_COLUMNS}
        rows.append(row)
    return rows


def pareto_feasible(records):
    feasible = [dict(row) for row in records if row.get("feasible")]
    if not feasible:
        return []
    ranked, _ = rank_population(feasible)
    pareto_rows = [row for row in ranked if row.get("rank") == 0 and row.get("feasible")]
    unique = []
    seen = set()
    for row in pareto_rows:
        key = (
            genome_key(row["genome"]),
            round(finite_float(row.get("recovery_C3F8")) or 0.0, 12),
            round(finite_float(row.get("TAC")) or 0.0, 8),
            round(finite_float(row.get("energy_cost")) or 0.0, 8),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def write_workbook(output_path, all_records, population, history, args, ranges):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    all_ranked, _ = rank_population(all_records) if all_records else ([], [])
    all_ranked = add_decision_scores(all_ranked, args)
    decision_rank = sorted(
        [row for row in all_ranked if row.get("feasible")],
        key=lambda row: (
            -(finite_float(row.get("decision_score")) or -1.0),
            finite_float(row.get("TAC")) or float("inf"),
        ),
    )
    pareto = pareto_feasible(all_ranked)
    pareto = sorted(
        pareto,
        key=lambda row: (
            -(finite_float(row.get("decision_score")) or -1.0),
            finite_float(row.get("TAC")) or float("inf"),
        ),
    )

    final_population = add_decision_scores(population, args)

    settings_rows = [
        {"setting": "population_size", "value": args.population_size},
        {"setting": "generations", "value": args.generations},
        {"setting": "purity_target", "value": args.purity_target},
        {"setting": "max_duty_cal_per_sec", "value": args.max_duty},
        {"setting": "solvent", "value": args.aspen_component_id},
        {"setting": "objectives", "value": "maximize recovery_C3F8; minimize TAC; minimize energy_cost"},
        {"setting": "tac_model", "value": "preliminary configurable model in tac_model.py"},
        {
            "setting": "checkpoint",
            "value": str(Path(args.checkpoint) if args.checkpoint else default_checkpoint_path(output_path))
            if not args.no_checkpoint
            else "disabled",
        },
        {"setting": "workbook_write_interval", "value": args.workbook_write_interval},
        {"setting": "case_timeout_seconds", "value": args.case_timeout_seconds},
        {
            "setting": "aspen_execution_mode",
            "value": (
                "direct_com_reused_session"
                if args.direct_com and args.reuse_aspen_session
                else "direct_com_per_case"
                if args.direct_com
                else "subprocess_watchdog"
            ),
        },
        {"setting": "session_restart_interval", "value": args.session_restart_interval},
    ]
    for name, bounds in ranges.items():
        settings_rows.append({"setting": name + "_range", "value": "{},{}".format(bounds[0], bounds[1])})

    tmp_path = output_path.with_name(
        "{}.tmp.{}.xlsx".format(output_path.stem, os.getpid())
    )
    try:
        with pd.ExcelWriter(str(tmp_path), engine="openpyxl") as writer:
            pd.DataFrame(strip_runtime_columns(all_ranked), columns=RESULT_COLUMNS).to_excel(
                writer, sheet_name="all_evaluations", index=False
            )
            pd.DataFrame(strip_runtime_columns(pareto), columns=RESULT_COLUMNS).to_excel(
                writer, sheet_name="pareto", index=False
            )
            pd.DataFrame(strip_runtime_columns(decision_rank), columns=RESULT_COLUMNS).to_excel(
                writer, sheet_name="decision_rank", index=False
            )
            pd.DataFrame(strip_runtime_columns(final_population), columns=RESULT_COLUMNS).to_excel(
                writer, sheet_name="final_population", index=False
            )
            pd.DataFrame(history).to_excel(writer, sheet_name="history", index=False)
            pd.DataFrame(settings_rows).to_excel(writer, sheet_name="settings", index=False)

        for attempt in range(6):
            try:
                os.replace(str(tmp_path), str(output_path))
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.5)
    except Exception as exc:
        print("WARNING: could not update workbook {}: {}".format(output_path, exc))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def write_dry_run_cases(output_path, population, args):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, item in enumerate(population, start=1):
        rows.append(genome_to_case(item["genome"], format_case_id(args.case_prefix, index), args))
    with pd.ExcelWriter(str(output_path), engine="openpyxl") as writer:
        pd.DataFrame(rows, columns=vm.CASE_COLUMNS).to_excel(writer, sheet_name="dry_run_cases", index=False)


def should_write_workbook(args, all_records):
    interval = getattr(args, "workbook_write_interval", 20)
    try:
        interval = int(interval)
    except Exception:
        interval = 20
    if interval <= 1:
        return True
    return len(all_records) % interval == 0


def generation_summary(generation, all_records, population):
    feasible_all = [row for row in all_records if row.get("feasible")]
    feasible_pop = [row for row in population if row.get("feasible")]
    pareto_all = pareto_feasible(all_records)

    def best_value(rows, column, maximize=False):
        values = [finite_float(row.get(column)) for row in rows]
        values = [value for value in values if value is not None]
        if not values:
            return None
        return max(values) if maximize else min(values)

    return {
        "generation": generation,
        "evaluations": len(all_records),
        "population_feasible": len(feasible_pop),
        "all_feasible": len(feasible_all),
        "pareto_count": len(pareto_all),
        "best_recovery_C3F8": best_value(feasible_all, "recovery_C3F8", maximize=True),
        "min_total_reboiler_duty": best_value(feasible_all, "total_reboiler_duty"),
        "min_TAC": best_value(feasible_all, "TAC"),
        "min_energy_cost": best_value(feasible_all, "energy_cost"),
        "min_solvent_loss_total": best_value(feasible_all, "solvent_loss_total"),
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Run all-variable NMP NSGA-II optimization with Aspen Plus.")
    parser.add_argument("--template", default=str(vm.TEMPLATE_BKP))
    parser.add_argument("--solvents", default=str(vm.SOLVENTS_XLSX))
    parser.add_argument("--output", default=str(vm.BASE_DIR / "outputs" / "optimization" / "nmp_nsga2_optimization.xlsx"))
    parser.add_argument("--case-work-dir", default=str(vm.CASE_WORK_DIR / "nmp_nsga2"))
    parser.add_argument(
        "--checkpoint",
        default=None,
        help="JSONL checkpoint path. Defaults to <output>.checkpoint.jsonl.",
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="Disable per-case JSONL checkpointing.",
    )
    parser.add_argument("--population-size", type=int, default=24)
    parser.add_argument("--generations", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260512)
    parser.add_argument("--case-prefix", default="NMPNS")
    parser.add_argument("--solvent-name", default="N-METHYL-2-PYRROLIDONE")
    parser.add_argument("--aspen-component-id", default="NMP")
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Reuse previous workbook records as an evaluation cache.")
    parser.add_argument("--dry-run", action="store_true", help="Only write the initial population, do not run Aspen.")
    parser.add_argument(
        "--seed-from",
        default=str(vm.BASE_DIR / "outputs" / "legacy_optimization" / "nmp_combined_pareto_analysis.xlsx"),
    )
    parser.add_argument("--seed-sheet", default="decision_rank")
    parser.add_argument("--seed-count", type=int, default=8)
    parser.add_argument("--purity-target", type=float, default=vm.C3F8_PURITY_TARGET or 0.999)
    parser.add_argument(
        "--max-duty",
        type=float,
        default=1.0e7,
        help="Maximum accepted total reboiler duty in cal/sec. Use <=0 to disable.",
    )
    parser.add_argument("--sf-range", default=None)
    parser.add_argument("--rr1-range", default=None)
    parser.add_argument("--d1-range", default=None)
    parser.add_argument("--rr2-range", default=None)
    parser.add_argument("--d2-range", default=None)
    parser.add_argument("--n1-range", default=None)
    parser.add_argument("--nf1-range", default=None)
    parser.add_argument("--ns1-range", default=None)
    parser.add_argument("--n2-range", default=None)
    parser.add_argument("--nf2-range", default=None)
    parser.add_argument(
        "--allow-solvent-below-feed",
        action="store_true",
        help="Do not force NS1 to be above NF1.",
    )
    parser.add_argument("--crossover-probability", type=float, default=0.9)
    parser.add_argument("--mutation-probability", type=float, default=0.25)
    parser.add_argument("--integer-mutation-probability", type=float, default=0.25)
    parser.add_argument("--continuous-mutation-sigma", type=float, default=0.08)
    parser.add_argument("--integer-mutation-sigma", type=float, default=0.12)
    parser.add_argument("--w-recovery", type=float, default=1.0)
    parser.add_argument("--w-tac", type=float, default=1.0)
    parser.add_argument("--w-energy", type=float, default=0.5)
    parser.add_argument(
        "--workbook-write-interval",
        type=int,
        default=20,
        help="Write the Excel workbook every N completed records; checkpoint is still written every case.",
    )
    parser.add_argument(
        "--case-timeout-seconds",
        type=int,
        default=900,
        help="Outer watchdog timeout for one Aspen case when using subprocess mode. Use <=0 for variable_map.RUN_TIMEOUT_SECONDS.",
    )
    parser.add_argument(
        "--direct-com",
        action="store_true",
        help="Run Aspen COM in the optimizer process. Faster to start, but a blocked COM call cannot be killed safely.",
    )
    parser.add_argument(
        "--reuse-aspen-session",
        action="store_true",
        help="Reuse one Aspen session across cases instead of reopening the model for every evaluation.",
    )
    parser.add_argument(
        "--session-restart-interval",
        type=int,
        default=25,
        help="When reusing one Aspen session, reopen the template after every N real Aspen evaluations. Use <=0 to disable.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.reuse_aspen_session and not args.direct_com:
        args.direct_com = True
    if args.population_size < 4:
        raise ValueError("--population-size must be at least 4.")
    if args.generations < 0:
        raise ValueError("--generations must be at least 0.")

    ranges = parse_ranges(args)
    rng = random.Random(args.seed)
    output_path = Path(args.output)
    checkpoint_path = None if args.no_checkpoint else Path(args.checkpoint or default_checkpoint_path(output_path))
    max_duty = None if args.max_duty is not None and args.max_duty <= 0 else args.max_duty
    solvent_economics = tac_model.load_solvent_economics(args.solvents)

    population = initial_population(args, ranges, rng)
    if args.dry_run:
        write_dry_run_cases(output_path, population, args)
        print("Dry run wrote {} initial cases to {}".format(len(population), output_path.resolve()))
        return

    existing_records = []
    resume_source = ""
    if args.resume:
        existing_records, resume_source = load_existing_evaluations(
            output_path,
            checkpoint_path,
            ranges,
            not args.allow_solvent_below_feed,
            args.purity_target,
            max_duty,
            solvent_economics,
        )
        if existing_records:
            print("Loaded {} existing records from {}".format(len(existing_records), resume_source))
        else:
            print("No existing records found for resume.")

    cache = {}
    for record in existing_records:
        if all(name in record for name in VARIABLES):
            genome = row_to_genome(record, ranges, not args.allow_solvent_below_feed)
            record["genome"] = genome
            cache[genome_key(genome)] = dict(record)

    cases_df = pd.DataFrame([genome_to_case(item["genome"], "SOLVENT_IDS", args) for item in population])
    solvent_ids = read_solvent_ids(args.solvents, cases_df)
    runner = None
    if args.direct_com:
        runner = AspenCaseRunner(
            template_bkp=args.template,
            case_work_dir=args.case_work_dir,
            visible=args.visible,
        )

    context = {
        "args": args,
        "runner": runner,
        "solvent_ids": solvent_ids,
        "session_id": "{}_persistent".format(args.case_prefix),
        "cache": cache,
        "case_counter": max_case_number(existing_records, args.case_prefix),
        "max_duty": max_duty,
        "solvent_economics": solvent_economics,
        "checkpoint_path": checkpoint_path,
        "replay_records": existing_records,
        "replay_index": 0,
    }

    all_records = list(existing_records)
    history = []
    if checkpoint_path:
        print("Checkpoint: {}".format(Path(checkpoint_path).resolve()))

    print("Evaluating initial population: {} individuals".format(len(population)))
    evaluated_population = []
    initial_added_records = False
    for item in population:
        record = evaluate_individual(item, 0, item.get("source", "initial"), context)
        evaluated_population.append(record)
        if not record.get("_replayed"):
            initial_added_records = True
            all_records.append(record)
            append_checkpoint(checkpoint_path, record)
            if should_write_workbook(args, all_records):
                write_workbook(output_path, all_records, evaluated_population, history, args, ranges)
                gc.collect()
        print(
            "  {} feasible={} recovery={} TAC={}".format(
                record.get("case_id"),
                record.get("feasible"),
                record.get("recovery_C3F8"),
                record.get("TAC"),
            )
        )

    population, ranked_initial = select_next_population(evaluated_population, args.population_size)
    all_records = [dict(row) for row in all_records]
    if initial_added_records or not existing_records:
        history.append(generation_summary(0, all_records, population))
        write_workbook(output_path, all_records, population, history, args, ranges)

    for generation in range(1, args.generations + 1):
        ranked_population, _ = rank_population(population)
        offspring = []
        while len(offspring) < args.population_size:
            parent_a = tournament(ranked_population, rng)
            parent_b = tournament(ranked_population, rng)
            child_genome = crossover_and_mutate(parent_a, parent_b, ranges, args, rng)
            offspring.append({"genome": child_genome, "source": "offspring"})

        print("Generation {}/{}: evaluating {} offspring".format(generation, args.generations, len(offspring)))
        evaluated_offspring = []
        generation_added_records = False
        for item in offspring:
            record = evaluate_individual(item, generation, "offspring", context)
            evaluated_offspring.append(record)
            if not record.get("_replayed"):
                generation_added_records = True
                all_records.append(record)
                append_checkpoint(checkpoint_path, record)
                if should_write_workbook(args, all_records):
                    write_workbook(output_path, all_records, population + evaluated_offspring, history, args, ranges)
                    gc.collect()
            print(
                "  {} feasible={} recovery={} TAC={}".format(
                    record.get("case_id"),
                    record.get("feasible"),
                    record.get("recovery_C3F8"),
                    record.get("TAC"),
                )
            )

        population, _ = select_next_population(population + evaluated_offspring, args.population_size)
        if generation_added_records:
            history.append(generation_summary(generation, all_records, population))
            write_workbook(output_path, all_records, population, history, args, ranges)

    print("Finished NSGA-II optimization.")
    print("Wrote {}".format(output_path.resolve()))
    if runner is not None:
        runner.close_session()


if __name__ == "__main__":
    main()
