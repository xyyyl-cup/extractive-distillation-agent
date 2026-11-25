# -*- coding: utf-8 -*-
"""
Zero-build Aspen agent for the C3F8/C3F6 extractive-distillation workflow.

This agent does not copy an existing Aspen backup as the output carrier. It
renders an Aspen input deck from a structured spec, initializes a blank Aspen
simulation from that deck, writes a runnable BKP, and can launch a smoke-run
validation through the existing Aspen COM runner.
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import time
import traceback
import uuid
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SPEC_PATH = BASE_DIR / "zero_agent_flowsheet_spec.json"
DEFAULT_OUTPUT_INP = BASE_DIR / "generated" / "zero_agent_generated_ed_template.inp"
DEFAULT_OUTPUT_BKP = BASE_DIR / "generated" / "zero_agent_generated_ed_template.bkp"
DEFAULT_REPORT = BASE_DIR / "generated" / "zero_agent_generated_ed_template.autoverify.json"


def aspen_automation_pids():
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -eq 'AspenPlus.exe' -and $_.CommandLine -like '*-Automation*' } | "
                    "ForEach-Object { $procId=$_.ProcessId; $p=Get-Process -Id $procId -ErrorAction SilentlyContinue; "
                    "if($p -and -not $p.HasExited){ $procId } }"
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=10,
        )
    except Exception:
        return set()
    pids = set()
    for line in (completed.stdout or "").splitlines():
        try:
            pids.add(int(line.strip()))
        except Exception:
            pass
    return pids


def terminate_processes(pids):
    for pid in sorted(set(int(item) for item in pids if item)):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        except Exception:
            pass
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId={}\" "
                        "-ErrorAction SilentlyContinue; "
                        "if($p){{ Invoke-CimMethod -InputObject $p -MethodName Terminate "
                        "-Arguments @{{Reason=0}} | Out-Null }}"
                    ).format(pid),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except Exception:
            pass


def cleanup_new_aspen_processes(baseline_pids, settle_seconds=6):
    baseline_pids = set(int(item) for item in (baseline_pids or set()) if item)
    deadline = time.time() + max(1, int(settle_seconds or 1))
    quiet_checks = 0
    while time.time() < deadline and quiet_checks < 2:
        new_pids = aspen_automation_pids() - baseline_pids
        if new_pids:
            quiet_checks = 0
            terminate_processes(new_pids)
        else:
            quiet_checks += 1
        time.sleep(0.8)


ASPEN_TRANSIENT_COM_HRESULTS = {
    -2147023170,  # RPC server unavailable / remote procedure call failed.
    -2147417848,  # The object invoked has disconnected from its clients.
    -2147418111,  # Call was rejected by callee.
    -2147418105,  # Server threw an exception during automation.
}


def aspen_transient_com_error(exc):
    args = getattr(exc, "args", ()) or ()
    for item in (getattr(exc, "hresult", None), args[0] if args else None):
        try:
            if int(item) in ASPEN_TRANSIENT_COM_HRESULTS:
                return True
        except Exception:
            pass
    text = "{} {}".format(exc.__class__.__name__, exc).upper()
    return (
        "RPC" in text
        or "REMOTE PROCEDURE CALL" in text
        or "PYWINTYPES.COM_ERROR" in text
        or "CALL WAS REJECTED BY CALLEE" in text
        or "OBJECT INVOKED HAS DISCONNECTED" in text
    )


def co_initialize_if_available():
    try:
        import pythoncom
    except Exception:
        return None
    try:
        pythoncom.CoInitialize()
    except Exception:
        return None
    return pythoncom


def co_uninitialize_if_needed(pythoncom_module):
    if pythoncom_module is None:
        return
    try:
        pythoncom_module.CoUninitialize()
    except Exception:
        pass


def prepare_aspen_app(app, visible=False):
    try:
        app.Visible = bool(visible)
    except Exception:
        pass
    try:
        app.SuppressDialogs = 1
    except Exception:
        pass


def close_aspen_app(app):
    if app is None:
        return
    try:
        app.Close(False)
    except Exception:
        pass
    try:
        app.Quit()
    except Exception:
        pass


def open_aspen_document_with_retry(win32, prog_id, initializer, attempts=4, settle_seconds=8):
    baseline_aspen = aspen_automation_pids()
    last_error = None
    attempts = max(1, int(attempts or 1))
    for attempt in range(1, attempts + 1):
        app = None
        try:
            app = win32.DispatchEx(prog_id)
            prepare_aspen_app(app, visible=False)
            time.sleep(0.8 if attempt == 1 else 1.5)
            initializer(app)
            prepare_aspen_app(app, visible=False)
            return app
        except Exception as exc:
            last_error = exc
            close_aspen_app(app)
            cleanup_new_aspen_processes(baseline_aspen, settle_seconds=settle_seconds)
            if attempt >= attempts or not aspen_transient_com_error(exc):
                raise
            time.sleep(min(20, 3 * attempt))
    raise last_error


def ascii_path(path):
    try:
        str(path).encode("ascii")
        return True
    except Exception:
        return False


def writable_ascii_stage_root(reference_path):
    reference_path = Path(reference_path).resolve()
    candidates = []
    env_root = ""
    try:
        import os

        env_root = os.environ.get("ASPEN_COM_STAGE_DIR", "")
    except Exception:
        env_root = ""
    if env_root:
        candidates.append(Path(env_root))
    if reference_path.drive:
        candidates.append(Path(reference_path.drive + "\\temp\\aspen_com_stage"))
        candidates.append(Path(reference_path.drive + "\\aspen_com_stage"))
    candidates.extend([
        Path("D:/temp/aspen_com_stage"),
        Path("C:/temp/aspen_com_stage"),
        Path("C:/aspen_com_stage"),
        Path("D:/aspen_com_stage"),
    ])
    for candidate in candidates:
        if not ascii_path(candidate):
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ("write_probe_{}.tmp".format(uuid.uuid4().hex[:8]))
            probe.write_text("ok", encoding="ascii")
            if probe.exists():
                probe.unlink()
            return candidate
        except Exception:
            pass
    raise RuntimeError("No writable ASCII Aspen COM stage directory is available.")


def make_aspen_stage_dir(reference_path):
    root = writable_ascii_stage_root(reference_path)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    stage = root / ("stage_{}_{}".format(stamp, uuid.uuid4().hex[:10]))
    stage.mkdir(parents=True, exist_ok=True)
    return stage


def safe_stage_name(path, default_stem="aspen_case"):
    path = Path(path)
    stem = re.sub(r"[^0-9A-Za-z_.-]+", "_", path.stem).strip("._")
    if not stem:
        stem = default_stem
    suffix = path.suffix if ascii_path(path.suffix) and path.suffix else ".dat"
    return "{}{}".format(stem[:60], suffix)


def stage_input_file_for_aspen(source_path, stage_dir):
    source_path = Path(source_path).resolve()
    staged = Path(stage_dir) / safe_stage_name(source_path, default_stem="aspen_input")
    shutil.copy2(str(source_path), str(staged))
    return staged


def cleanup_aspen_stage_dir(stage_dir):
    if not stage_dir:
        return
    try:
        import os

        if os.environ.get("ASPEN_KEEP_COM_STAGE", "").strip() in ("1", "true", "TRUE", "yes", "YES"):
            return
    except Exception:
        pass
    try:
        shutil.rmtree(str(stage_dir), ignore_errors=True)
    except Exception:
        pass


DEFAULT_SPEC = {
    "name": "zero_build_closed_loop_ed_C3F8_C3F6",
    "property_method": "PRWS",
    "components": [
        {"id": "C3F6", "formula": "C3F6"},
        {"id": "C3F8", "formula": "C3F8"},
        {"id": "ETHYL-01", "formula": "C2H6O2"},
        {"id": "NMP", "formula": "C5H9NO-D2"},
        {"id": "DMSO", "formula": "C2H6OS"},
        {"id": "SULFOLAN", "formula": "C4H8O2S"},
        {"id": "PC", "formula": "C4H6O3-D1"},
        {"id": "GBL", "formula": "C4H6O2-D2"},
        {"id": "ADPN", "formula": "C6H8N2-D1"},
    ],
    "unifac_structures": {
        "C3F6": [[2955, 1], [1050, 1], [2960, 1], [2950, 1]],
        "C3F8": [[2960, 2], [2955, 1]],
    },
    "nrtl_pairs": [
        ["ETHYL-01", "NMP", [0.0, -387.1213000, 0.3000000000, 0.0, 0.0, 0.0, 50.0, 50.0]],
        ["NMP", "ETHYL-01", [0.0, 261.4792000, 0.3000000000, 0.0, 0.0, 0.0, 50.0, 50.0]],
        ["ETHYL-01", "DMSO", [0.0, -407.9908000, 0.3000000000, 0.0, 0.0, 0.0, 100.0, 100.0]],
        ["DMSO", "ETHYL-01", [0.0, 125.2802000, 0.3000000000, 0.0, 0.0, 0.0, 100.0, 100.0]],
    ],
    "streams": {
        "C1": {"temperature_c": 25.0, "pressure_bar": 1.0, "mole_flow": {"ETHYL-01": 30.0}},
        "C2": {
            "temperature_c": 197.651032,
            "pressure_bar": 1.0,
            "mole_flow": {"C3F6": 4.4404766e-6, "C3F8": 8.6621131e-9, "ETHYL-01": 29.9999856},
        },
        "F": {"temperature_c": 50.0, "pressure_bar": 1.0, "mole_flow": {"C3F6": 1.0, "C3F8": 9.0}},
    },
    "blocks": [
        {"id": "B1", "type": "RADFRAC", "in": ["C2", "S2"], "out": ["D", "W"]},
        {"id": "B2", "type": "RADFRAC", "in": ["W"], "out": ["S1", "S3"]},
        {"id": "B3", "type": "MIXER", "in": ["C1", "S3"], "out": ["C2"]},
        {"id": "B4", "type": "HEATER", "in": ["F"], "out": ["S2"]},
    ],
    "heater": {"B4": {"pressure_bar": 1.0, "vapor_fraction": 0.0}},
    "radfrac": {
        "B1": {
            "nstage": 70,
            "init_option": "STANDARD",
            "max_iterations": 200,
            "feeds": {"C2": 34, "S2": 52},
            "products": {"D": [1, "V"], "W": [70, "L"]},
            "pressure_bar": 1.0,
            "distillate_mole_flow": 8.974,
            "reflux_ratio": 14.0,
            "internal_sections": [[1, 2, 33], [2, 34, 51], [3, 52, 69]],
        },
        "B2": {
            "nstage": 5,
            "max_iterations": 25,
            "feeds": {"W": 2},
            "products": {"S1": [1, "V"], "S3": [5, "L"]},
            "pressure_bar": 1.0,
            "distillate_mole_flow": 1.2,
            "reflux_ratio": 3.0,
            "internal_sections": [[1, 2, 4]],
        },
    },
    "calculator": {
        "id": "C-1",
        "solvent_component": "ETHYL-01",
        "target_flow": 30.0,
        "recycle_stream": "S3",
        "makeup_stream": "C1",
    },
    "validation_role": "cold-start technical validation for the zero-built BKP; not a process optimum",
    "validation_source": "generated/cold_search2_results.json::COLD2_RR70_D8.8",
    "validation_case": {
        "case_id": "ZERO_AGENT_NMP_COLD_START_VALIDATION",
        "solvent_name": "NMP",
        "aspen_component_id": "NMP",
        "SF": 3.3387,
        "RR1": 70.0,
        "D1": 8.8,
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


def component_token(component_id):
    return component_id


def float_token(value):
    value = float(value)
    if value == 0:
        return "0."
    if abs(value) < 1.0e-4:
        return "{:.7E}".format(value)
    if value.is_integer():
        return "{}.".format(int(value))
    return "{:.10g}".format(value)


def bpval_token(value, index):
    value = float(value)
    if value == 0:
        return "0.0"
    if index == 2 and abs(value - 0.3) < 1.0e-12:
        return ".3000000000"
    if index in (6, 7):
        return "{:.8f}".format(value)
    if abs(value) > 1:
        return "{:.7f}".format(value)
    return "{:.10g}".format(value)


def continuation_join(items, indent="    ", continuation_indent="        ", separator=" / "):
    lines = []
    current = indent
    for index, item in enumerate(items):
        suffix = separator if index < len(items) - 1 else ""
        token = item + suffix
        if len(current.rstrip()) + 1 + len(token) > 74 and current.strip():
            lines.append(current.rstrip() + " &")
            current = continuation_indent + token
        else:
            current += token if current.strip() == "" else token
    lines.append(current.rstrip())
    return lines


def render_components(spec):
    lines = ["COMPONENTS "]
    entries = [
        "{} {}".format(
            component_token(item["id"]),
            item.get("db_name")
            or item.get("aspen_databank_id")
            or item.get("databank_id")
            or item.get("formula")
            or item["id"],
        )
        for item in spec["components"]
    ]
    for index, entry in enumerate(entries):
        suffix = " / " if index < len(entries) - 1 else ""
        lines.append("    {}{}".format(entry, suffix))
    return lines


def render_flowsheet(spec):
    lines = ["FLOWSHEET "]
    for block in spec["blocks"]:
        lines.append(
            "    BLOCK {id} IN={inputs} OUT={outputs} ".format(
                id=block["id"],
                inputs=" ".join(block["in"]),
                outputs=" ".join(block["out"]),
            )
        )
    return lines


def render_structures(spec):
    lines = ["STRUCTURES "]
    for component_id, groups in spec.get("unifac_structures", {}).items():
        group_tokens = ["{} {}".format(group, count) for group, count in groups]
        lines.append("    UNIFAC {} {} ".format(component_id, " / ".join(group_tokens)))
    return lines


def render_prop_data(spec):
    if not spec.get("nrtl_pairs"):
        return []
    lines = [
        "PROP-DATA NRTL-1",
        "    IN-UNITS MET PRESSURE=bar TEMPERATURE=C DELTA-T=C PDROP=bar  &",
        "        INVERSE-PRES='1/bar' SHORT-LENGTH=mm ",
        "    PROP-LIST NRTL ",
    ]
    for cid1, cid2, values in spec.get("nrtl_pairs", []):
        tokens = [component_token(cid1), component_token(cid2)] + [
            bpval_token(value, index) for index, value in enumerate(values)
        ]
        lines.append("    BPVAL {} {} {} {} {} {} {}  &".format(*tokens[:7]))
        lines.append("        {} {} {} ".format(*tokens[7:]))
    return lines


def render_stream(stream_id, stream):
    lines = [
        "STREAM {} ".format(stream_id),
        "    SUBSTREAM MIXED TEMP={} PRES={} ".format(
            float_token(stream["temperature_c"]),
            float_token(stream["pressure_bar"]),
        ),
    ]
    flows = [
        "{} {}".format(component_token(component_id), float_token(flow))
        for component_id, flow in stream["mole_flow"].items()
    ]
    lines.extend(continuation_join(flows, indent="    MOLE-FLOW ", continuation_indent="        "))
    return lines


def render_heater(block_id, spec):
    heater = spec["heater"][block_id]
    return [
        "BLOCK {} HEATER ".format(block_id),
        "    PARAM PRES={} VFRAC={} DPPARMOPT=NO ".format(
            float_token(heater["pressure_bar"]),
            float_token(heater["vapor_fraction"]),
        ),
    ]


def render_radfrac(block_id, spec):
    block = spec["radfrac"][block_id]
    lines = ["BLOCK {} RADFRAC ".format(block_id)]
    section_ids = ["CS-{}".format(section[0]) for section in block["internal_sections"]]
    lines.append("    SUBOBJECTS INTERNALS = {} ".format(" ".join(section_ids)))
    hydraulic_flag = "YES" if spec.get("enable_column_hydraulics", False) else "NO"
    hydraulic_report = "HYDRAULIC" if hydraulic_flag == "YES" else "NOHYDRAULIC"
    if block.get("init_option"):
        lines.append(
            "    PARAM NSTAGE={} ALGORITHM=STANDARD INIT-OPTION=STANDARD  &".format(block["nstage"])
        )
        lines.append("        HYDRAULIC={} MAXOL={} DAMPING=NONE ".format(hydraulic_flag, block["max_iterations"]))
    else:
        lines.append(
            "    PARAM NSTAGE={} ALGORITHM=STANDARD HYDRAULIC={} MAXOL={}  &".format(
                block["nstage"], hydraulic_flag, block["max_iterations"]
            )
        )
        lines.append("        DAMPING=NONE ")
    lines.append("    PARAM2 STATIC-DP=YES ")
    condenser = block.get("condenser") or "PARTIAL-V"
    lines.append("    COL-CONFIG CONDENSER={} CA-CONFIG=INT-1 ".format(condenser))
    lines.append(
        "    FEEDS {} ".format(
            " / ".join("{} {}".format(stream, stage) for stream, stage in block["feeds"].items())
        )
    )
    lines.append(
        "    PRODUCTS {} ".format(
            " / ".join(
                "{} {} {}".format(stream, product[0], product[1])
                for stream, product in block["products"].items()
            )
        )
    )
    lines.append("    P-SPEC 1 {} ".format(float_token(block["pressure_bar"])))
    lines.append(
        "    COL-SPECS MOLE-D={} MOLE-RR={} ".format(
            float_token(block["distillate_mole_flow"]),
            float_token(block["reflux_ratio"]),
        )
    )
    lines.append("    REPORT {} ".format(hydraulic_report))
    for section_id, stage1, stage2 in block["internal_sections"]:
        lines.append("    INTERNALS CS-{} STAGE1={} STAGE2={} P-UPDATE=NO ".format(section_id, stage1, stage2))
        lines.append("    TRAY-SIZE {} {} {} SIEVE ".format(section_id, stage1, stage2))
    return lines


def render_calculator(spec):
    calc = spec["calculator"]
    component = component_token(calc["solvent_component"])
    target = "{:.10g}".format(float(calc["target_flow"]))
    formula_mode = str(calc.get("formula_mode") or "").strip()
    if formula_mode == "bounded_nonnegative_makeup":
        fortran_lines = [
            "F     FLOW={}-MOLARCO ".format(target),
            "F     IF (FLOW .LT. 0.0) FLOW=0.0 ",
        ]
    else:
        fortran_lines = ["F     FLOW={}-MOLARCO ".format(target)]
    return [
        "EO-CONV-OPTI ",
        "",
        "CALCULATOR {} ".format(calc["id"]),
        "    DEFINE MOLARCO MOLE-FLOW STREAM={} SUBSTREAM=MIXED  &".format(calc["recycle_stream"]),
        "        COMPONENT={} UOM=\"kmol/hr\" ".format(component),
        "    DEFINE FLOW MOLE-FLOW STREAM={} SUBSTREAM=MIXED  &".format(calc["makeup_stream"]),
        "        COMPONENT={} UOM=\"kmol/hr\" ".format(component),
        *fortran_lines,
        "    READ-VARS MOLARCO ",
        "    WRITE-VARS FLOW ",
    ]


def render_input_deck(spec):
    property_keyword = spec.get("property_keyword") or spec["property_method"]
    block_map = {block.get("id"): block for block in spec.get("blocks", [])}
    block_ids = [
        component_token(block.get("id"))
        for block in spec.get("blocks", [])
        if block.get("id")
    ]
    stream_ids = []
    for stream_id in (spec.get("streams") or {}):
        token = component_token(stream_id)
        if token and token not in stream_ids:
            stream_ids.append(token)
    for block in spec.get("blocks", []):
        for stream_id in block.get("out") or []:
            token = component_token(stream_id)
            if token and token not in stream_ids:
                stream_ids.append(token)
    lines = [
        "TITLE 'Zero-build extractive distillation steady-state case'",
        "SUMMARY MM",
        "RUN-CONTROL MAX-TIME=240",
        "IN-UNITS MET PRESSURE=bar TEMPERATURE=C DELTA-T=C PDROP=bar  &",
        "        INVERSE-PRES='1/bar' SHORT-LENGTH=mm ",
        "OUT-UNITS MET",
        "",
        "DEF-STREAMS CONVEN ALL ",
        "",
        "MODEL-OPTION ",
        "",
        "DATABANKS 'APV110 PURE37' / 'APV110 AQUEOUS' / 'APV110 SOLIDS' &",
        "         / 'APV110 INORGANIC' / 'APESV110 AP-EOS' /  &",
        "        'NISTV110 NIST-TRC' / NOASPENPCD",
        "",
        "PROP-SOURCES 'APV110 PURE37' / 'APV110 AQUEOUS' /  &",
        "        'APV110 SOLIDS' / 'APV110 INORGANIC' / 'APESV110 AP-EOS' &",
        "         / 'NISTV110 NIST-TRC'",
        "",
    ]
    lines.extend(render_components(spec))
    lines.extend(["", "SOLVE ", "    RUN-MODE MODE=SIM ", ""])
    lines.extend(render_flowsheet(spec))
    lines.extend(["", "PROPERTIES {} ".format(spec["property_method"]), "    PROPERTIES {} ".format(property_keyword), ""])
    lines.extend(render_structures(spec))
    lines.extend([""])
    lines.extend(render_prop_data(spec))
    lines.extend([""])
    for stream_id, stream in spec["streams"].items():
        lines.extend(render_stream(stream_id, stream))
        lines.append("")
    if "B3" in block_map:
        mixer_maxit = int(spec.get("mixer_max_iterations") or 200)
        lines.extend(["BLOCK B3 MIXER ", "    PARAM MAXIT={} ".format(mixer_maxit), ""])
    for block_id in spec.get("heater", {}):
        lines.extend(render_heater(block_id, spec))
        lines.append("")
    for block_id in sorted(spec.get("radfrac", {})):
        lines.extend(render_radfrac(block_id, spec))
        lines.append("")
    if spec.get("calculator"):
        lines.extend(render_calculator(spec))
    if stream_ids:
        lines.extend(continuation_join(
            stream_ids,
            indent="STREAM-REPORT INCL-STREAMS=",
            continuation_indent="    ",
            separator=" ",
        ))
    if block_ids:
        lines.extend(continuation_join(
            block_ids,
            indent="BLOCK-REPORT INCL-BLOCKS=",
            continuation_indent="    ",
            separator=" ",
        ))
    lines.extend(["PROPERTY-REP PCES ", "END"])
    return "\n".join(lines) + "\n"


def write_input_deck(spec, output_inp):
    output_inp = Path(output_inp)
    output_inp.parent.mkdir(parents=True, exist_ok=True)
    output_inp.write_text(render_input_deck(spec), encoding="utf-8")
    return output_inp


def build_archive_from_input(input_deck, output_bkp, run_seed_case=True):
    import variable_map as vm
    import win32com.client as win32

    input_deck = Path(input_deck).resolve()
    output_bkp = Path(output_bkp).resolve()
    output_bkp.parent.mkdir(parents=True, exist_ok=True)
    pythoncom_module = co_initialize_if_available()
    baseline_aspen = aspen_automation_pids()
    app = None
    stage_dir = None
    try:
        stage_dir = make_aspen_stage_dir(output_bkp)
        staged_input_deck = stage_input_file_for_aspen(input_deck, stage_dir)
        staged_output_bkp = Path(stage_dir) / safe_stage_name(output_bkp, default_stem="aspen_output")
        if staged_output_bkp.suffix.lower() != ".bkp":
            staged_output_bkp = staged_output_bkp.with_suffix(".bkp")
        app = open_aspen_document_with_retry(
            win32,
            vm.ASPEN_PROG_ID,
            lambda active_app: active_app.InitFromFile(str(staged_input_deck), False),
            attempts=4,
            settle_seconds=8,
        )
        if run_seed_case:
            _run_aspen_simulation(app)
        app.WriteArchive2(str(staged_output_bkp), True)
        if not staged_output_bkp.exists():
            raise RuntimeError("Aspen WriteArchive2 did not create the expected staged BKP: {}".format(staged_output_bkp))
        shutil.copy2(str(staged_output_bkp), str(output_bkp))
    finally:
        close_aspen_app(app)
        cleanup_new_aspen_processes(baseline_aspen, settle_seconds=8)
        cleanup_aspen_stage_dir(stage_dir)
        co_uninitialize_if_needed(pythoncom_module)
    return output_bkp


def export_aspen_calculation_report(app, output_dir, stem="aspen_calculation_report"):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(stem or "aspen_calculation_report")).strip("._")
    if not safe_stem:
        safe_stem = "aspen_calculation_report"
    base_path = output_dir / safe_stem
    result = {
        "aspen_report_export_type": 21,
        "aspen_report_export_base": str(base_path.resolve()),
    }
    for old_path in output_dir.glob(base_path.name + "*"):
        if old_path.is_file() and old_path.suffix.lower() in {".aprrep", ".rep", ".rpt", ".txt", ".out"}:
            try:
                old_path.unlink()
            except Exception:
                pass
    try:
        app.Export(21, str(base_path.resolve()))
    except Exception as exc:
        result["aspen_report_export_warning"] = "{}: {}".format(exc.__class__.__name__, exc)
        return result
    candidates = [
        path for path in output_dir.glob(base_path.name + "*")
        if path.is_file() and path.stat().st_size > 0
    ]
    candidates.sort(key=lambda path: (
        {".aprrep": 0, ".rep": 1, ".rpt": 2, ".txt": 3, ".out": 4}.get(path.suffix.lower(), 9),
        str(path).lower(),
    ))
    if candidates:
        result["aspen_report_path"] = str(candidates[0].resolve())
        result["aspen_report_size_bytes"] = candidates[0].stat().st_size
        result["aspen_report_export_candidates"] = [str(path.resolve()) for path in candidates]
    else:
        result["aspen_report_export_warning"] = "Export(21) returned without a non-empty report file."
    return result


def verify_static_bkp(path, spec=None):
    import win32com.client as win32
    import variable_map as vm

    baseline_aspen = aspen_automation_pids()
    if spec:
        required_paths = [r"\Data\Blocks\{}".format(block.get("id")) for block in spec.get("blocks", []) if block.get("id")]
        required_paths.extend(r"\Data\Streams\{}".format(stream_id) for stream_id in (spec.get("streams") or {}))
        if spec.get("calculator"):
            required_paths.append(r"\Data\Flowsheeting Options\Calculator\{}".format((spec.get("calculator") or {}).get("id") or "C-1"))
    else:
        required_paths = [
            r"\Data\Blocks\B1",
            r"\Data\Blocks\B2",
            r"\Data\Blocks\B3",
            r"\Data\Blocks\B4",
            r"\Data\Streams\F",
            r"\Data\Streams\C1",
            r"\Data\Streams\C2",
            r"\Data\Flowsheeting Options\Calculator\C-1",
        ]
    pythoncom_module = co_initialize_if_available()
    app = None
    stage_dir = None
    try:
        stage_dir = make_aspen_stage_dir(path)
        staged_bkp = stage_input_file_for_aspen(path, stage_dir)
        app = open_aspen_document_with_retry(
            win32,
            vm.ASPEN_PROG_ID,
            lambda active_app: active_app.InitFromArchive2(str(staged_bkp))
            if hasattr(active_app, "InitFromArchive2")
            else active_app.InitFromArchive(str(staged_bkp)),
            attempts=4,
            settle_seconds=8,
        )
        found = {}
        for tree_path in required_paths:
            found[tree_path] = app.Tree.FindNode(tree_path) is not None
        return {"required_nodes": found, "passed": all(found.values())}
    finally:
        close_aspen_app(app)
        cleanup_new_aspen_processes(baseline_aspen, settle_seconds=8)
        cleanup_aspen_stage_dir(stage_dir)
        co_uninitialize_if_needed(pythoncom_module)


def _aspen_node_value(app, path):
    if not path:
        return None
    node = app.Tree.FindNode(str(path))
    if node is None:
        return None
    try:
        return node.Value
    except Exception:
        try:
            return node.value
        except Exception:
            return None


def _aspen_numeric_node(app, path):
    value = _aspen_node_value(app, path)
    try:
        return float(value)
    except Exception:
        return None


def _first_numeric_node(app, paths):
    for path in paths:
        value = _aspen_numeric_node(app, path)
        if value is not None:
            return value, path
    return None, ""


def _run_aspen_simulation(app):
    engine = None
    try:
        engine = app.Engine
    except Exception:
        engine = None
    if engine is not None:
        try:
            engine.Run2()
            return "Engine.Run2"
        except Exception:
            pass
    app.Run2()
    return "Application.Run2"


def _generic_required_output_probe(app, spec):
    components = [
        str(item.get("id") or "").strip()
        for item in (spec.get("components") or [])
        if isinstance(item, dict) and item.get("id")
    ]
    block_ids = [
        str(item.get("id") or "").strip()
        for item in (spec.get("blocks") or [])
        if isinstance(item, dict) and str(item.get("type") or "").upper() == "RADFRAC" and item.get("id")
    ]
    stream_ids = []
    for block in spec.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for stream_id in block.get("out") or []:
            token = str(stream_id or "").strip()
            if token and token not in stream_ids:
                stream_ids.append(token)
    duty_values = {}
    duty_paths = {}
    for block_id in block_ids:
        value, path = _first_numeric_node(app, [
            r"\Data\Blocks\{}\Output\REB_DUTY".format(block_id),
            r"\Data\Blocks\{}\Output\DUTY".format(block_id),
            r"\Data\Blocks\{}\Output\HEAT_RATE".format(block_id),
            r"\Data\Blocks\{}\Output\COND_DUTY".format(block_id),
        ])
        duty_values[block_id] = value
        duty_paths[block_id] = path
    stream_values = {}
    stream_paths = {}
    for stream_id in stream_ids:
        candidates = [
            r"\Data\Streams\{}\Output\TOT_FLOW".format(stream_id),
            r"\Data\Streams\{}\Output\MOLEFLOW".format(stream_id),
            r"\Data\Streams\{}\Output\MASSFLOW".format(stream_id),
        ]
        for comp_id in components:
            candidates.extend([
                r"\Data\Streams\{}\Output\MOLEFLOW\MIXED\{}".format(stream_id, comp_id),
                r"\Data\Streams\{}\Output\MOLEFRAC\MIXED\{}".format(stream_id, comp_id),
                r"\Data\Streams\{}\Output\MASSFLOW\MIXED\{}".format(stream_id, comp_id),
            ])
        value, path = _first_numeric_node(app, candidates)
        stream_values[stream_id] = value
        stream_paths[stream_id] = path
    def meaningful(value, allow_fraction=False):
        if value is None:
            return False
        try:
            value = float(value)
        except Exception:
            return False
        if allow_fraction and 0.0 < abs(value) <= 1.0:
            return True
        return abs(value) > 1e-12
    duty_signal = any(meaningful(value) for value in duty_values.values())
    stream_signal = any(meaningful(value, allow_fraction=True) for value in stream_values.values())
    return {
        "required_outputs_complete": bool(duty_signal and stream_signal),
        "duty_signal_available": bool(duty_signal),
        "stream_signal_available": bool(stream_signal),
        "duty_probe_values": duty_values,
        "duty_probe_paths": duty_paths,
        "stream_probe_values": stream_values,
        "stream_probe_paths": stream_paths,
    }


def scan_bkp_convergence_failures(path):
    path = Path(path)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    signals = []
    patterns = [
        ("tear_summary_compstat_nonzero", r"DSET\s+CONV-SUM\s+TEAR-SUMMARY\s+COMPSTAT\b[^\n\r]*\([^\)]*[1-9]"),
        ("wegstein_compstat_nonzero", r"DSET\s+CONVERGENCE\s+WEGSTEIN\b[^\n\r]*\s+COMPSTAT\b[^\n\r]*\([^\)]*[1-9]"),
        ("block_status_nonzero", r"DSET\s+CONVERGENCE\s+WEGSTEIN\b[^\n\r]*\s+BLKSTAT\b[^\n\r]*\([^\)]*[1-9]"),
    ]
    for label, pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            signals.append(label)
    upper = text.upper()
    if "IS NOT IN MASS BALANCE" in upper:
        signals.append("block_not_in_mass_balance")
    if "EXCEEDED MAXIMUM NUMBER OF ITERATIONS" in upper:
        signals.append("exceeded_maximum_iterations")
    return sorted(set(signals))


def run_generic_smoke_validation(generated_bkp, spec, output_dir, metric_reader=None):
    import variable_map as vm
    import win32com.client as win32

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    case = dict(spec.get("validation_case") or spec.get("default_smoke_case") or {})
    result = {
        "case_id": case.get("case_id") or "GENERIC_ZERO_BUILD_SMOKE",
        "solvent_name": case.get("solvent_name") or case.get("aspen_component_id") or "",
        "aspen_component_id": case.get("aspen_component_id") or "",
        "validation_mode": "generic_aspen_run2",
        "converged": False,
        "pass_or_fail": "FAIL",
        "error_message": "",
        "run_seconds": None,
    }
    app = None
    started = time.time()
    pythoncom_module = co_initialize_if_available()
    baseline_aspen = aspen_automation_pids()
    stage_dir = None
    try:
        stage_dir = make_aspen_stage_dir(generated_bkp)
        staged_bkp = stage_input_file_for_aspen(generated_bkp, stage_dir)
        app = open_aspen_document_with_retry(
            win32,
            vm.ASPEN_PROG_ID,
            lambda active_app: active_app.InitFromArchive2(str(staged_bkp))
            if hasattr(active_app, "InitFromArchive2")
            else active_app.InitFromArchive(str(staged_bkp)),
            attempts=4,
            settle_seconds=8,
        )
        result["run_method"] = _run_aspen_simulation(app)
        result["run_seconds"] = round(time.time() - started, 3)
        status_value = _aspen_node_value(app, getattr(vm, "CONVERGENCE_STATUS_PATH", ""))
        result["convergence_status"] = status_value
        if status_value in (None, ""):
            status_converged = True
        else:
            status_converged = str(status_value).strip().upper() in {str(item).upper() for item in vm.CONVERGENCE_SUCCESS_VALUES}
        qreb1, qreb1_path = _first_numeric_node(app, [
            getattr(vm, "QREB1_PATH", ""),
            r"\Data\Blocks\B1\Output\DUTY",
            r"\Data\Blocks\B1\Output\HEAT_RATE",
        ])
        qreb2, qreb2_path = _first_numeric_node(app, [
            getattr(vm, "QREB2_PATH", ""),
            r"\Data\Blocks\B2\Output\DUTY",
            r"\Data\Blocks\B2\Output\HEAT_RATE",
        ])
        result["Qreb_1"] = qreb1
        result["Qreb_2"] = qreb2
        result["Qreb_1_path"] = qreb1_path
        result["Qreb_2_path"] = qreb2_path
        result["total_reboiler_duty"] = (
            (qreb1 or 0.0) + (qreb2 or 0.0)
            if qreb1 is not None or qreb2 is not None
            else None
        )
        output_probe = _generic_required_output_probe(app, spec)
        result.update(output_probe)
        if callable(metric_reader):
            try:
                metrics = metric_reader(app, spec)
                if isinstance(metrics, dict):
                    result.update(metrics)
                    result["live_metric_reader_status"] = "ok"
            except Exception as exc:
                result["live_metric_reader_status"] = "failed"
                result["live_metric_reader_warning"] = "{}: {}".format(exc.__class__.__name__, exc)
        result.update(export_aspen_calculation_report(
            app,
            output_dir,
            "{}_aspen_report".format(Path(generated_bkp).stem),
        ))
        converged = bool(status_converged and output_probe.get("required_outputs_complete"))
        result["converged"] = bool(converged)
        result["pass_or_fail"] = "PASS" if converged else "FAIL"
        if status_converged and not output_probe.get("required_outputs_complete"):
            result["error_message"] = (
                "required_outputs_missing: Aspen Run2 returned no hard error, but required duty/stream outputs "
                "were missing or zero; this case is not trusted for optimization."
            )
        validated_bkp = output_dir / (Path(generated_bkp).stem + ".validated.bkp")
        try:
            app.WriteArchive2(str(validated_bkp.resolve()), True)
            result["validated_bkp"] = str(validated_bkp.resolve())
            failure_signals = scan_bkp_convergence_failures(validated_bkp)
            if failure_signals:
                result["aspen_convergence_failure_signals"] = failure_signals
                result["converged"] = False
                result["pass_or_fail"] = "FAIL"
                result["error_message"] = "Aspen archive contains convergence failure signals: {}".format(
                    ", ".join(failure_signals)
                )
        except Exception as exc:
            result["archive_write_warning"] = "{}: {}".format(exc.__class__.__name__, exc)
    except Exception as exc:
        result["run_seconds"] = round(time.time() - started, 3)
        result["error_message"] = "{}: {}\n{}".format(exc.__class__.__name__, exc, traceback.format_exc(limit=3))
    finally:
        close_aspen_app(app)
        cleanup_new_aspen_processes(baseline_aspen, settle_seconds=8)
        cleanup_aspen_stage_dir(stage_dir)
        co_uninitialize_if_needed(pythoncom_module)
    return result


def run_validation_case(generated_bkp, spec, output_dir):
    if not spec.get("legacy_variable_map_validation"):
        return run_generic_smoke_validation(generated_bkp, spec, output_dir)
    from aspen_runner import AspenCaseRunner
    from run_screening import read_solvent_ids
    import variable_map as vm

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    case = dict(spec["validation_case"])
    runner = AspenCaseRunner(template_bkp=generated_bkp, case_work_dir=output_dir / "case_files", visible=False)
    solvent_ids = read_solvent_ids(vm.SOLVENTS_XLSX, None)
    return runner.run_case(case, solvent_ids)


def topology_metadata(spec):
    blocks = {str(block.get("id") or ""): block for block in spec.get("blocks", []) if isinstance(block, dict)}
    calc = spec.get("calculator") or {}
    mode = str(spec.get("process_topology_mode") or "").strip()
    b3 = blocks.get("B3") or {}
    b3_inputs = {str(item).upper() for item in (b3.get("in") or [])}
    b3_outputs = {str(item).upper() for item in (b3.get("out") or [])}
    has_recycle_mixer = "B3" in blocks and "S3" in b3_inputs and "C2" in b3_outputs
    closed_loop = bool(
        "closed" in mode.lower()
        or (
            has_recycle_mixer
            and (not calc or str(calc.get("recycle_stream") or "").upper() == "S3")
        )
    )
    if not mode:
        mode = "closed_loop_solvent_recycle" if closed_loop else "open_loop_or_unclassified"
    return {
        "process_topology_mode": mode,
        "closed_loop_recycle": closed_loop,
        "has_recycle_mixer": has_recycle_mixer,
        "has_recycle_calculator": bool(calc),
        "recycle_stream": calc.get("recycle_stream") or ("S3" if has_recycle_mixer else ""),
        "makeup_stream": calc.get("makeup_stream") or ("C1" if has_recycle_mixer else ""),
    }


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_args():
    parser = argparse.ArgumentParser(description="Zero-build Aspen agent for closed-loop ED model generation.")
    parser.add_argument("--write-default-spec", action="store_true")
    parser.add_argument("--spec", default=str(DEFAULT_SPEC_PATH))
    parser.add_argument("--output-inp", default=str(DEFAULT_OUTPUT_INP))
    parser.add_argument("--output-bkp", default=str(DEFAULT_OUTPUT_BKP))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--smoke-run", action="store_true")
    parser.add_argument("--no-seed-run", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.write_default_spec:
        write_default_spec(args.spec)

    spec = load_spec(args.spec)
    input_deck = write_input_deck(spec, args.output_inp)
    output_bkp = build_archive_from_input(input_deck, args.output_bkp, run_seed_case=not args.no_seed_run)
    report = {
        "agent": "zero_build_aspen_agent",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "generation_mode": "blank Aspen InitFromFile(input deck) -> optional seed Run2 -> WriteArchive2",
        "template_bkp_used_as_output_carrier": False,
        "input_deck": str(Path(input_deck).resolve()),
        "generated_bkp": str(Path(output_bkp).resolve()),
        "spec": str(Path(args.spec).resolve()),
        "validation_role": spec.get("validation_role", ""),
        "validation_source": spec.get("validation_source", ""),
        "topology": topology_metadata(spec),
        "input_deck_sha256": sha256_file(input_deck),
        "generated_bkp_sha256": sha256_file(output_bkp),
        "static_verification": verify_static_bkp(output_bkp, spec),
    }
    if args.smoke_run:
        report["smoke_result"] = run_validation_case(output_bkp, spec, Path(output_bkp).parent / "zero_agent_validation")

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
