# -*- coding: utf-8 -*-
"""Run one Aspen case in an isolated process for optimizer watchdog control."""

import argparse
import json
import math
import traceback
from collections import OrderedDict

from aspen_runner import AspenCaseRunner


def clean_json_value(value):
    if isinstance(value, dict):
        return {str(key): clean_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json_value(item) for item in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return value


def parse_args():
    parser = argparse.ArgumentParser(description="Run a single Aspen optimization case.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    with open(args.input, "r", encoding="utf-8") as handle:
        payload = json.load(handle, object_pairs_hook=OrderedDict)

    case = payload["case"]
    solvent_ids = payload["solvent_ids"]
    runner = AspenCaseRunner(
        template_bkp=payload.get("template"),
        case_work_dir=payload.get("case_work_dir"),
        visible=bool(payload.get("visible")),
    )

    try:
        result = runner.run_case(case, solvent_ids)
    except Exception as exc:
        result = OrderedDict()
        result["converged"] = False
        result["pass_or_fail"] = "FAIL"
        result["error_message"] = "{}\n{}".format(exc, traceback.format_exc(limit=3))

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(clean_json_value(result), handle, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    main()
