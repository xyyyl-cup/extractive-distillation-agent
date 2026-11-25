# -*- coding: utf-8 -*-
"""
Batch runner for Aspen Plus extractive-distillation screening.
"""

import argparse
from collections import OrderedDict
from pathlib import Path

import pandas as pd

try:
    import variable_map as vm
    from aspen_runner import AspenCaseRunner
except ImportError:  # pragma: no cover
    from . import variable_map as vm
    from .aspen_runner import AspenCaseRunner


def validate_columns(df, columns, file_name):
    missing = [column for column in columns if column not in df.columns]
    if missing:
        raise ValueError("{} missing columns: {}".format(file_name, ", ".join(missing)))


def normalize_case_id(value):
    if value is None:
        return ""
    try:
        if value != value:
            return ""
    except Exception:
        pass
    return str(value).strip()


def read_solvent_ids(solvents_path, cases_df):
    path = Path(solvents_path)
    if path.exists():
        solvents_df = pd.read_excel(str(path))
        validate_columns(solvents_df, vm.SOLVENT_COLUMNS, str(path))
        return [str(item).strip() for item in solvents_df["aspen_component_id"].dropna().tolist()]
    return [str(item).strip() for item in cases_df["aspen_component_id"].dropna().unique().tolist()]


def result_records_from_file(results_path):
    path = Path(results_path)
    records = OrderedDict()
    if not path.exists():
        return records
    df = pd.read_excel(str(path))
    for _, row in df.iterrows():
        record = row.to_dict()
        case_id = normalize_case_id(record.get("case_id"))
        if case_id:
            records[case_id] = record
    return records


def sort_results_df(df):
    for column in vm.RESULT_COLUMNS:
        if column not in df.columns:
            df[column] = None
    df = df[vm.RESULT_COLUMNS].copy()
    if df.empty:
        return df

    pass_rank = df["pass_or_fail"].map({"PASS": 0, "FAIL": 1}).fillna(2)
    score_sort = df["score"].map(safe_score_for_sort)
    df["_pass_rank"] = pass_rank
    df["_score_sort"] = score_sort
    df = df.sort_values(["_pass_rank", "_score_sort", "case_id"])
    return df[vm.RESULT_COLUMNS]


def safe_score_for_sort(value):
    if value is None:
        return float("inf")
    try:
        if value != value:
            return float("inf")
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return float("inf")


def write_results(records, results_path):
    df = pd.DataFrame(list(records.values()))
    if df.empty:
        df = pd.DataFrame(columns=vm.RESULT_COLUMNS)
    df = sort_results_df(df)
    df.to_excel(str(results_path), index=False, engine="openpyxl")


def parse_args():
    parser = argparse.ArgumentParser(description="Run Aspen ED screening cases.")
    parser.add_argument("--template", default=str(vm.TEMPLATE_BKP), help="Aspen .bkp template")
    parser.add_argument("--solvents", default=str(vm.SOLVENTS_XLSX), help="solvents.xlsx")
    parser.add_argument("--cases", default=str(vm.CASES_XLSX), help="cases.xlsx")
    parser.add_argument("--results", default=str(vm.RESULTS_XLSX), help="results.xlsx")
    parser.add_argument("--case-work-dir", default=str(vm.CASE_WORK_DIR), help="Folder for per-case .bkp copies")
    parser.add_argument("--visible", action="store_true", help="Show Aspen while running")
    parser.add_argument("--resume", action="store_true", help="Skip case_id values already present in results.xlsx")
    return parser.parse_args()


def main():
    args = parse_args()
    cases_path = Path(args.cases)
    results_path = Path(args.results)

    cases_df = pd.read_excel(str(cases_path))
    validate_columns(cases_df, vm.CASE_COLUMNS, str(cases_path))
    if cases_df.empty:
        print("No cases found in {}".format(cases_path))
        write_results(OrderedDict(), results_path)
        return

    solvent_ids = read_solvent_ids(args.solvents, cases_df)
    records = result_records_from_file(results_path)
    runner = AspenCaseRunner(
        template_bkp=args.template,
        case_work_dir=args.case_work_dir,
        visible=args.visible,
    )

    total = len(cases_df)
    for index, case in enumerate(cases_df.to_dict(orient="records"), start=1):
        case_id = normalize_case_id(case.get("case_id"))
        if not case_id:
            case_id = "C{:06d}".format(index)
            case["case_id"] = case_id

        if args.resume and case_id in records:
            print("[{}/{}] Skipping completed case {}".format(index, total, case_id))
            continue

        print("[{}/{}] Running case {} ({})".format(
            index, total, case_id, normalize_case_id(case.get("solvent_name"))
        ))
        result = runner.run_case(case, solvent_ids)
        records[case_id] = result
        write_results(records, results_path)

        print(
            "    converged={}, pass_or_fail={}, score={}".format(
                result.get("converged"),
                result.get("pass_or_fail"),
                result.get("score"),
            )
        )
        if result.get("error_message"):
            print("    error: {}".format(result.get("error_message")))

    write_results(records, results_path)
    print("Finished. Results written to {}".format(results_path))


if __name__ == "__main__":
    main()
