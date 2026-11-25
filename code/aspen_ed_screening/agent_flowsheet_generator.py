# -*- coding: utf-8 -*-
"""
Template-driven Aspen flowsheet generator for the C3F8/C3F6 ED workflow.

The generator starts from a validated Aspen backup skeleton, then rebuilds the
flowsheet topology and key operating specifications from a compact JSON spec.
This is intentionally conservative: Aspen backup files contain many internal
metadata sections, so the skeleton is used only as the Aspen-compatible carrier.
"""

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_BKP = BASE_DIR / "ED_template.bkp"
DEFAULT_SPEC_PATH = BASE_DIR / "ed_agent_flowsheet_spec.json"
DEFAULT_OUTPUT_BKP = BASE_DIR / "generated" / "agent_generated_ed_template.bkp"


DEFAULT_SPEC = {
    "name": "closed_loop_extractive_distillation_C3F8_C3F6",
    "property_method": "PRWS",
    "components": [
        "C3F6",
        "C3F8",
        "ETHYL-01",
        "NMP",
        "DMSO",
        "SULFOLAN",
        "PC",
        "GBL",
        "ADPN",
    ],
    "blocks": [
        {"id": "B1", "type": "RADFRAC", "model": "RadFrac", "library_name": "RadFrac"},
        {"id": "B2", "type": "RADFRAC", "model": "RadFrac", "library_name": "RadFrac"},
        {"id": "B3", "type": "MIXER", "model": "Mixer", "library_name": "Mixer"},
        {"id": "B4", "type": "HEATER", "model": "Heater", "library_name": "Heater"},
    ],
    "connections": [
        {
            "block": "B1",
            "inputs": [{"stream": "C2", "port": "M0-1"}, {"stream": "S2", "port": "M0-1"}],
            "outputs": [{"stream": "D", "port": "M0-1"}, {"stream": "W", "port": "M2-3"}],
        },
        {
            "block": "B2",
            "inputs": [{"stream": "W", "port": "M0-1"}],
            "outputs": [{"stream": "S1", "port": "M0-1"}, {"stream": "S3", "port": "M2-3"}],
        },
        {
            "block": "B3",
            "inputs": [{"stream": "C1", "port": "M0-1"}, {"stream": "S3", "port": "M0-1"}],
            "outputs": [{"stream": "C2", "port": "M0-1"}],
        },
        {
            "block": "B4",
            "inputs": [{"stream": "F", "port": "M0-1"}],
            "outputs": [{"stream": "S2", "port": "M0-1"}],
        },
    ],
    "radfrac": {
        "B1": {
            "nstage": 70,
            "feeds": {"C2": 34, "S2": 52},
            "products": {"D": {"stage": 1, "phase": "V"}, "W": {"stage": 70, "phase": "L"}},
        },
        "B2": {
            "nstage": 5,
            "feeds": {"W": 2},
            "products": {"S1": {"stage": 1, "phase": "V"}, "S3": {"stage": 5, "phase": "L"}},
        },
    },
    "heater": {"B4": {"pressure_bar": 1.0, "vapor_fraction": 0.0}},
    "recycle": {
        "fresh_makeup_stream": "C1",
        "recycle_stream": "S3",
        "actual_solvent_stream": "C2",
        "calculator": "C-1",
        "fortran_template": "FLOW={target_flow}-MOLARCO",
    },
    "default_smoke_case": {
        "case_id": "AGENT_NMP_SMOKE",
        "solvent_name": "NMP",
        "aspen_component_id": "NMP",
        "SF": 3.3387,
        "RR1": 12.1788,
        "D1": 8.980945,
        "RR2": 2.7604,
        "D2": 1.222392,
        "N1": 70,
        "NF1": 52,
        "NS1": 34,
        "N2": 5,
        "NF2": 2,
    },
}


def load_spec(path):
    path = Path(path)
    if not path.exists():
        return DEFAULT_SPEC
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_default_spec(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_SPEC, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def block_library_name(block):
    return block.get("library_name") or block.get("model") or block["id"]


def render_block_library(spec):
    lines = [str(len(spec["blocks"]))]
    for block in spec["blocks"]:
        lines.extend(
            [
                ">VERSION 0",
                block["id"],
                block_library_name(block),
                "Built-In",
                block["type"],
            ]
        )
    return "\n".join(lines)


def render_port(stream):
    if isinstance(stream, dict):
        return "{} {}".format(stream["stream"], stream.get("port", "M0-1"))
    return "{} M0-1".format(stream)


def render_port_list(streams):
    return "( " + " ".join(render_port(stream) for stream in streams) + " )"


def wrap_aspen_record(record, width=77):
    """Wrap long Aspen backup records using the template's continuation style."""
    lines = []
    cursor = 0
    while len(record) - cursor > width:
        hard_end = cursor + width
        if hard_end < len(record) and record[hard_end] == " ":
            split_at = hard_end
            segment = record[cursor:split_at]
            for keyword in (" IN", " OUT"):
                if segment.endswith(keyword):
                    split_at = cursor + segment.rfind(keyword) + 1
                    break
        else:
            space_at = record.rfind(" ", cursor, hard_end)
            split_at = space_at + 1 if space_at >= cursor else hard_end
        lines.append(record[cursor:split_at])
        cursor = split_at
    lines.append(record[cursor:])
    return "\n".join(lines)


def render_flowsheet_global(spec):
    block_by_id = {block["id"]: block for block in spec["blocks"]}
    parts = ["FLOWSHEET GLOBAL ? \\"]
    for index, connection in enumerate(spec["connections"]):
        block = block_by_id[connection["block"]]
        tail = "\\ \\" if index < len(spec["connections"]) - 1 else "\\"
        parts.append(
            ' BLOCK BLKID = {id} BLKTYPE = {typ} MDLTYPE = "{model}" IN = {ins} OUT = {outs} {tail}'.format(
                id=block["id"],
                typ=block["type"],
                model=block["model"],
                ins=render_port_list(connection["inputs"]),
                outs=render_port_list(connection["outputs"]),
                tail=tail,
            )
        )
    return wrap_aspen_record("".join(parts) + " ? PROPERTIES MAIN ?")


def replace_once(pattern, replacement, text, label, flags=0):
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise ValueError("Could not patch {}".format(label))
    return new_text


def patch_property_method(text, method):
    text = replace_once(r"GBASEOPSET\s*=\s*\S+", "GBASEOPSET = {}".format(method), text, "property base option")
    text = replace_once(r"GOPSETNAME\s*=\s*\S+", "GOPSETNAME = {}".format(method), text, "property option name")
    return text


def patch_radfrac_specs(text, spec):
    for block_id, values in spec.get("radfrac", {}).items():
        text = replace_once(
            r"(\? BLOCK RADFRAC {0} \?.*?PARAM NSTAGE = )\d+".format(block_id),
            r"\g<1>{}".format(values["nstage"]),
            text,
            "{} NSTAGE".format(block_id),
            flags=re.S,
        )
        for stream, stage in values.get("feeds", {}).items():
            text = replace_once(
                r"(FEED-SID\s*=\s*{}\s+FEED-STAGE\s*=\s*)\d+".format(re.escape(stream)),
                r"\g<1>{}".format(stage),
                text,
                "{} feed {}".format(block_id, stream),
            )
        for stream, product in values.get("products", {}).items():
            text = replace_once(
                r"(PROD-STREAM\s*=\s*{}\s+PROD-STAGE\s*=\s*)\d+".format(re.escape(stream)),
                r"\g<1>{}".format(product["stage"]),
                text,
                "{} product {}".format(block_id, stream),
            )
    return text


def generate_bkp(spec, source_bkp, output_bkp):
    source_bkp = Path(source_bkp)
    output_bkp = Path(output_bkp)
    # Aspen backup files are mostly text but may contain non-UTF-8 UI/internal
    # payloads. latin-1 keeps a one-byte-to-one-codepoint mapping, so replacing
    # ASCII control sections does not corrupt the untouched binary-like tail.
    text = source_bkp.read_text(encoding="latin-1")

    text = replace_once(
        r"(?s)\n\d+\n>VERSION 0\nB1\nRadFrac\nBuilt-In\nRADFRAC.*?\? SETUP MAIN \?",
        "\n{}\n? SETUP MAIN ?".format(render_block_library(spec)),
        text,
        "block library",
    )
    text = replace_once(
        r"(?s)FLOWSHEET GLOBAL \?.*?\? PROPERTIES MAIN \?",
        render_flowsheet_global(spec),
        text,
        "flowsheet topology",
    )
    text = patch_property_method(text, spec["property_method"])
    text = patch_radfrac_specs(text, spec)

    output_bkp.parent.mkdir(parents=True, exist_ok=True)
    output_bkp.write_text(text, encoding="latin-1")
    return output_bkp


def verify_generated_bkp(path, spec):
    text = Path(path).read_text(encoding="latin-1")
    checks = {
        "bkp_marker_injected": "AGENT-GENERATED-FLOWSHEET" in text,
        "property_method": spec["property_method"] in text,
        "blocks": {},
        "connections": {},
    }
    for block in spec["blocks"]:
        checks["blocks"][block["id"]] = (
            "BLKID = {0}".format(block["id"]) in text and "BLKTYPE = {0}".format(block["type"]) in text
        )
    for connection in spec["connections"]:
        block = connection["block"]
        ports = connection["inputs"] + connection["outputs"]
        checks["connections"][block] = all(render_port(stream) in text for stream in ports)
    checks["passed"] = all(checks["blocks"].values()) and all(checks["connections"].values())
    return checks


def compare_bkp_files(source_bkp, output_bkp):
    source_bytes = Path(source_bkp).read_bytes()
    output_bytes = Path(output_bkp).read_bytes()
    return {
        "byte_identical_to_source": source_bytes == output_bytes,
        "source_size_bytes": len(source_bytes),
        "output_size_bytes": len(output_bytes),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
    }


def run_smoke_case(generated_bkp, spec, output_dir):
    from aspen_runner import AspenCaseRunner
    from run_screening import read_solvent_ids
    import variable_map as vm

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    case = dict(spec["default_smoke_case"])
    runner = AspenCaseRunner(template_bkp=generated_bkp, case_work_dir=output_dir / "case_files", visible=False)
    solvent_ids = read_solvent_ids(vm.SOLVENTS_XLSX, None)
    return runner.run_case(case, solvent_ids)


def parse_args():
    parser = argparse.ArgumentParser(description="Generate a closed-loop ED Aspen BKP from a compact spec.")
    parser.add_argument("--write-default-spec", action="store_true")
    parser.add_argument("--spec", default=str(DEFAULT_SPEC_PATH))
    parser.add_argument("--source-bkp", default=str(DEFAULT_SOURCE_BKP))
    parser.add_argument("--output-bkp", default=str(DEFAULT_OUTPUT_BKP))
    parser.add_argument("--report", default=str(DEFAULT_OUTPUT_BKP.with_suffix(".report.json")))
    parser.add_argument("--smoke-run", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.write_default_spec:
        write_default_spec(args.spec)

    spec = load_spec(args.spec)
    output_bkp = generate_bkp(spec, args.source_bkp, args.output_bkp)
    report = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "generated_bkp": str(Path(output_bkp).resolve()),
        "spec": str(Path(args.spec).resolve()),
        "source_bkp": str(Path(args.source_bkp).resolve()),
        "bkp_marker_policy": "no marker injected; Aspen BKP body is kept parser-clean",
        "verification": verify_generated_bkp(output_bkp, spec),
        "source_comparison": compare_bkp_files(args.source_bkp, output_bkp),
    }
    if args.smoke_run:
        report["smoke_result"] = run_smoke_case(output_bkp, spec, Path(output_bkp).parent)

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
