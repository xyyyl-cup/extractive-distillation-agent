# -*- coding: utf-8 -*-
"""
Generate cases.xlsx from solvents.xlsx and simple grid-search settings.
"""

import argparse
import itertools
from pathlib import Path

import pandas as pd

try:
    import variable_map as vm
except ImportError:  # pragma: no cover
    from . import variable_map as vm


def split_float_values(values):
    result = []
    for item in values or []:
        for part in str(item).split(","):
            text = part.strip()
            if text:
                result.append(float(text))
    return result


def linspace(low, high, points):
    low = float(low)
    high = float(high)
    points = int(points)
    if points <= 0:
        raise ValueError("points must be positive")
    if points == 1:
        return [low]
    step = (high - low) / float(points - 1)
    return [low + step * i for i in range(points)]


def validate_columns(df, columns, file_name):
    missing = [column for column in columns if column not in df.columns]
    if missing:
        raise ValueError("{} missing columns: {}".format(file_name, ", ".join(missing)))


def generate_cases(solvents_df, sf_points, rr1_values, d1_values, rr2_values, d2_values, stages):
    rows = []
    counter = 1
    for _, solvent in solvents_df.iterrows():
        sf_values = linspace(solvent["SF_min"], solvent["SF_max"], sf_points)
        for sf, rr1, d1, rr2, d2 in itertools.product(
            sf_values, rr1_values, d1_values, rr2_values, d2_values
        ):
            rows.append(
                {
                    "case_id": "C{:06d}".format(counter),
                    "solvent_name": solvent["solvent_name"],
                    "aspen_component_id": solvent["aspen_component_id"],
                    "SF": sf,
                    "RR1": rr1,
                    "D1": d1,
                    "RR2": rr2,
                    "D2": d2,
                    "N1": stages.get("N1"),
                    "NF1": stages.get("NF1"),
                    "NS1": stages.get("NS1"),
                    "N2": stages.get("N2"),
                    "NF2": stages.get("NF2"),
                }
            )
            counter += 1
    return pd.DataFrame(rows, columns=vm.CASE_COLUMNS)


def parse_args():
    parser = argparse.ArgumentParser(description="Generate cases.xlsx for Aspen ED screening.")
    parser.add_argument("--solvents", default=str(vm.SOLVENTS_XLSX), help="Input solvents.xlsx")
    parser.add_argument("--output", default=str(vm.CASES_XLSX), help="Output cases.xlsx")
    parser.add_argument("--sf-points", type=int, default=3, help="Number of SF grid points per solvent")
    parser.add_argument("--rr1", nargs="+", required=True, help="RR1 values, space or comma separated")
    parser.add_argument("--d1", nargs="+", required=True, help="D1 values, space or comma separated")
    parser.add_argument("--rr2", nargs="+", required=True, help="RR2 values, space or comma separated")
    parser.add_argument("--d2", nargs="+", required=True, help="D2 values, space or comma separated")
    parser.add_argument("--n1", type=float, default=None)
    parser.add_argument("--nf1", type=float, default=None)
    parser.add_argument("--ns1", type=float, default=None)
    parser.add_argument("--n2", type=float, default=None)
    parser.add_argument("--nf2", type=float, default=None)
    return parser.parse_args()


def main():
    args = parse_args()
    solvents_path = Path(args.solvents)
    output_path = Path(args.output)

    solvents_df = pd.read_excel(str(solvents_path))
    validate_columns(solvents_df, vm.SOLVENT_COLUMNS, str(solvents_path))
    if solvents_df.empty:
        raise ValueError("solvents.xlsx has no solvent rows.")

    stages = {
        "N1": args.n1,
        "NF1": args.nf1,
        "NS1": args.ns1,
        "N2": args.n2,
        "NF2": args.nf2,
    }
    cases_df = generate_cases(
        solvents_df,
        args.sf_points,
        split_float_values(args.rr1),
        split_float_values(args.d1),
        split_float_values(args.rr2),
        split_float_values(args.d2),
        stages,
    )
    cases_df.to_excel(str(output_path), index=False, engine="openpyxl")
    print("Wrote {} cases to {}".format(len(cases_df), output_path))


if __name__ == "__main__":
    main()
