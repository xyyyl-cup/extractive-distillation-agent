# -*- coding: utf-8 -*-
"""
Aspen quick VLE/flash prescreen for extractive-distillation entrainers.

This script is launched by the binary-separation Agent before Gaussian/GROMACS.
It computes K_light/K_heavy for the base binary and for candidate solvent S/F
points, then writes relative_volatility_kvalue_results.csv. Molecular evidence
is allowed only after these Aspen K-value rows show Ialpha > threshold.
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback
from pathlib import Path

import full_agent_dashboard as agent
import thermo_evidence_gate
import variable_map as vm


def component_id_token(value):
    return agent.component_token(value)


def databank_token(value):
    text = str(value or "").strip().upper()
    return re.sub(r"[^A-Z0-9_.-]+", "", text)


def float_token(value):
    try:
        value = float(value)
    except Exception:
        value = 0.0
    if abs(value) >= 1:
        return "{:.8g}".format(value)
    return "{:.10g}".format(value)


def short_text(value, limit=800):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) <= limit:
        return text
    return text[:limit] + "... [truncated]"


def databank_id_for_component(component):
    value = (
        component.get("db_name")
        or component.get("aspen_component_id")
        or component.get("id")
        or component.get("name")
    )
    return databank_token(value)


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


def render_nrtl_prop_data(nrtl_pairs):
    if not nrtl_pairs:
        return []
    lines = [
        "PROP-DATA NRTL-1",
        "    IN-UNITS MET PRESSURE=bar TEMPERATURE=C DELTA-T=C PDROP=bar  &",
        "        INVERSE-PRES='1/bar' SHORT-LENGTH=mm",
        "    PROP-LIST NRTL",
    ]
    for first, second, values in nrtl_pairs:
        tokens = [component_id_token(first), component_id_token(second)] + [float_token(value) for value in values]
        lines.append("    BPVAL {} {} {} {} {} {} {}  &".format(*tokens[:7]))
        lines.append("        {} {} {}".format(*tokens[7:]))
    return lines


def deck_lines(case):
    components = case["components"]
    flows = case["flows"]
    pressure_bar = float(case.get("pressure_bar") or 1.01325)
    temperature_c = float(case.get("temperature_c") or 25.0)
    property_method = case.get("property_method") or "NRTL"
    total_flow = sum(float(flow or 0) for flow in flows.values())
    if total_flow <= 0:
        total_flow = 1.0
    lines = [
        "TITLE 'ED relative volatility TP flash'",
        "SUMMARY MM",
        "STREAM-REPORT  PROPERTIES = KSET",
        "PROP-SET KSET KMX PHASE = V L",
        "RUN-CONTROL MAX-TIME=120",
        "IN-UNITS MET PRESSURE=bar TEMPERATURE=C DELTA-T=C PDROP=bar  &",
        "        INVERSE-PRES='1/bar' SHORT-LENGTH=mm ",
        "OUT-UNITS MET",
        "DATABANKS 'APV110 PURE37' / 'APV110 AQUEOUS' / 'APV110 SOLIDS' &",
        "         / 'APV110 INORGANIC' / 'APESV110 AP-EOS' /  &",
        "        'NISTV110 NIST-TRC' / NOASPENPCD",
        "PROP-SOURCES 'APV110 PURE37' / 'APV110 AQUEOUS' /  &",
        "        'APV110 SOLIDS' / 'APV110 INORGANIC' / 'APESV110 AP-EOS' &",
        "         / 'NISTV110 NIST-TRC'",
    ]
    component_tokens = []
    for index, component in enumerate(components):
        component_id = component_id_token(component["id"])
        db_id = databank_id_for_component(component)
        component_tokens.append("{} {}".format(component_id, db_id))
    lines.extend(continuation_join(component_tokens, indent="COMPONENTS ", continuation_indent="    "))
    lines.append("PROPERTIES {}".format(property_method))
    lines.extend(render_nrtl_prop_data(case.get("nrtl_pairs") or []))
    lines.extend([
        "FLOWSHEET",
        "   BLOCK B1 IN=F OUT=V L",
        "STREAM F TEMP={} PRES={} MOLE-FLOW={}".format(
            float_token(temperature_c),
            float_token(pressure_bar),
            float_token(total_flow),
        ),
    ])
    flow_tokens = [
        "{} {}".format(component_id_token(component_id), float_token(float(flow or 0) / total_flow))
        for component_id, flow in flows.items()
        if float(flow or 0) > 0
    ]
    lines.extend(continuation_join(flow_tokens, indent="   MOLE-FRAC ", continuation_indent="      "))
    lines.append("BLOCK B1 FLASH2")
    if str(case.get("flash_mode") or "vfrac").lower() == "tp":
        lines.append("   PARAM TEMP={} PRES={}".format(float_token(temperature_c), float_token(pressure_bar)))
    else:
        lines.append("   PARAM PRES={} VFRAC={}".format(
            float_token(pressure_bar),
            float_token(case.get("vapor_fraction") or 0.5),
        ))
    lines.extend([
        "STREAM-REPORT INCL-STREAMS=V L F",
        "BLOCK-REPORT INCL-BLOCKS=B1",
    ])
    return lines


def write_input_deck(path, case):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(deck_lines(case)) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def node_value(app, path):
    try:
        node = app.Tree.FindNode(path)
    except Exception:
        node = None
    if node is None:
        return None
    for attr in ("Value", "value"):
        try:
            return getattr(node, attr)
        except Exception:
            pass
    return None


def numeric_node(app, path):
    value = node_value(app, path)
    try:
        return float(value)
    except Exception:
        return None


def component_output_value(app, owner, variables, component_id):
    component = component_id_token(component_id)
    for variable in variables:
        candidates = [
            r"\Data\{}\Output\{}\MIXED\{}".format(owner, variable, component),
            r"\Data\{}\Output\{}\{}\MIXED".format(owner, variable, component),
            r"\Data\{}\Output\{}\{}".format(owner, variable, component),
            r"\Data\{}\Output\{}\SUBSTREAM\MIXED\{}".format(owner, variable, component),
        ]
        for path in candidates:
            value = numeric_node(app, path)
            if value is not None:
                return value, path
    return None, ""


def stream_mole_fraction(app, stream, component_id):
    return component_output_value(
        app,
        "Streams\\{}".format(stream),
        ["MOLEFRAC", "MOLE-FRAC", "MOLEFRAC_OUT", "MOLE-FRAC-OUT"],
        component_id,
    )


def block_component_value(app, variable_names, component_id):
    return component_output_value(
        app,
        "Blocks\\B1",
        variable_names,
        component_id,
    )


def stream_temperature_c(app, stream):
    for path in [
        r"\Data\Streams\{}\Output\TEMP_OUT\MIXED".format(stream),
        r"\Data\Streams\{}\Output\TEMP\MIXED".format(stream),
        r"\Data\Streams\{}\Output\TEMP_OUT".format(stream),
    ]:
        value = numeric_node(app, path)
        if value is not None:
            return value
    return None


def aspen_process_ids():
    try:
        completed = subprocess.run(
            ["wmic", "process", "where", "name='AspenPlus.exe'", "get", "ProcessId", "/value"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception:
        return set()
    text = short_text(completed.stdout, 4000)
    pids = set()
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().lower() != "processid":
            continue
        try:
            pids.add(int(value.strip()))
        except Exception:
            pass
    return pids


def cleanup_new_aspen_processes(preserve_pids=None):
    preserve = set()
    for pid in preserve_pids or []:
        try:
            preserve.add(int(pid))
        except Exception:
            pass
    for pid in sorted(aspen_process_ids() - preserve):
        try:
            subprocess.run(
                ["wmic", "process", "where", "processid={}".format(pid), "delete"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
        except Exception:
            pass


def latest_aspen_history(work_dir, started_at):
    try:
        candidates = [
            path for path in Path(work_dir).glob("*.his")
            if path.stat().st_mtime >= started_at - 5
        ]
    except Exception:
        candidates = []
    if not candidates:
        return "", ""
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    path = candidates[0]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        try:
            text = path.read_text(encoding="gbk", errors="replace")
        except Exception:
            text = ""
    lines = [line.rstrip() for line in text.splitlines()]
    interesting = []
    for line in lines:
        upper = line.upper()
        if any(token in upper for token in ["ERROR", "WARNING", "MISSING", "NOT IN A DATA BANK", "PARAMETER"]):
            interesting.append(line.strip())
    tail = "\n".join(interesting[-18:] or lines[-40:])
    return str(path.resolve()), short_text(tail, 1600)


def needs_ascii_aspen_runtime(path):
    text = str(path)
    return any(ord(ch) > 127 for ch in text) or len(text) >= 180


def prepare_ascii_aspen_runtime(input_deck):
    input_deck = Path(input_deck).resolve()
    if not needs_ascii_aspen_runtime(input_deck):
        return input_deck, None
    root = Path(tempfile.gettempdir()) / "ed_aspen_flash_runtime"
    token = "{}_{}_{}".format(os.getpid(), int(time.time() * 1000), agent.safe_file_token(input_deck.stem)[:32])
    runtime_dir = root / token
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_input = runtime_dir / input_deck.name
    shutil.copy2(str(input_deck), str(runtime_input))
    return runtime_input, runtime_dir


def copy_runtime_outputs(runtime_dir, target_dir):
    copied = []
    if not runtime_dir:
        return copied
    runtime_dir = Path(runtime_dir)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    for path in runtime_dir.glob("*"):
        if not path.is_file():
            continue
        dest = target_dir / path.name
        try:
            shutil.copy2(str(path), str(dest))
            copied.append(str(dest.resolve()))
        except Exception:
            pass
    return copied


def run_aspen_case(input_deck, timeout_hint_seconds=120):
    import win32com.client as win32

    app = None
    started = time.time()
    preserve_pids = aspen_process_ids()
    original_input_deck = Path(input_deck).resolve()
    runtime_input_deck, runtime_dir = prepare_ascii_aspen_runtime(original_input_deck)
    result = {
        "status": "failed",
        "run_seconds": None,
        "error_message": "",
        "preserve_aspen_pids": sorted(preserve_pids),
        "aspen_runtime_mode": "ascii_scratch" if runtime_dir else "native_path",
        "aspen_runtime_dir": str(runtime_dir.resolve()) if runtime_dir else "",
        "aspen_runtime_input_deck": str(runtime_input_deck.resolve()),
    }
    try:
        try:
            app = win32.DispatchEx(vm.ASPEN_PROG_ID)
        except Exception:
            app = win32.Dispatch(vm.ASPEN_PROG_ID)
        try:
            app.Visible = False
        except Exception:
            pass
        try:
            app.SuppressDialogs = 1
        except Exception:
            pass
        input_deck = runtime_input_deck
        app.InitFromFile(str(input_deck), False)
        engine = getattr(app, "Engine", None)
        if engine is not None:
            engine.Run2()
            while True:
                try:
                    running = bool(engine.IsRunning)
                except Exception:
                    break
                if not running:
                    break
                if time.time() - started > timeout_hint_seconds:
                    raise TimeoutError("Aspen flash exceeded {} seconds".format(timeout_hint_seconds))
                time.sleep(1.0)
        else:
            app.Run2()
        try:
            app.WriteArchive2(str(input_deck.with_suffix(".bkp")))
        except Exception:
            pass
        history_path, history_tail = latest_aspen_history(input_deck.parent, started)
        copied_outputs = copy_runtime_outputs(runtime_dir, original_input_deck.parent)
        result["run_seconds"] = round(time.time() - started, 3)
        result["status"] = "run_complete"
        result["aspen_history_path"] = history_path
        result["aspen_history_tail"] = history_tail
        result["aspen_runtime_copied_outputs"] = "; ".join(copied_outputs)
        result["app"] = app
        return result
    except Exception as exc:
        result["run_seconds"] = round(time.time() - started, 3)
        result["error_message"] = "{}: {}\n{}".format(exc.__class__.__name__, exc, traceback.format_exc(limit=4))
        history_path, history_tail = latest_aspen_history(runtime_input_deck.parent, started)
        result["aspen_history_path"] = history_path
        result["aspen_history_tail"] = history_tail
        if app is not None:
            close_app(app, preserve_pids=preserve_pids)
        copied_outputs = copy_runtime_outputs(runtime_dir, original_input_deck.parent)
        result["aspen_runtime_copied_outputs"] = "; ".join(copied_outputs)
        return result


def discover_aspen_engine():
    explicit = os.environ.get("ASPEN_ENGINE_EXE") or os.environ.get("ASPEN_EXE") or ""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend([
        Path(r"D:\ASPENV11\Aspen Plus V11.0\Engine\Xeq\aspen.exe"),
        Path(r"C:\Program Files\AspenTech\Aspen Plus V11.0\Engine\Xeq\aspen.exe"),
        Path(r"C:\Program Files (x86)\AspenTech\Aspen Plus V11.0\Engine\Xeq\aspen.exe"),
    ])
    for path in candidates:
        if path.exists():
            return path
    return Path("")


def aspen_setup_for_engine(engine_exe):
    if not engine_exe:
        return Path("")
    setup = Path(engine_exe).with_name("aspsetup.bat")
    return setup if setup.exists() else Path("")


def aspen_license_unavailable_text(text):
    upper = str(text or "").upper()
    return (
        "PROPERTIES LICENSE IS NOT AVAILABLE" in upper
        or "FATAL LICENSING ERROR" in upper
        or ("ASPEN" in upper and "LICENSE" in upper and "NOT AVAILABLE" in upper)
        or ("PROPERTY ENGINE" in upper and "LICENSE" in upper and "UNAVAILABLE" in upper)
    )


def aspen_probe_license_unavailable(probe):
    probe = probe or {}
    if str(probe.get("status") or "").strip().lower() == "license_unavailable":
        return True
    combined = "\n".join(
        str(probe.get(key) or "")
        for key in ("message", "stdout_tail", "stderr_tail")
    )
    return aspen_license_unavailable_text(combined)


def run_aspen_engine_probe(input_deck, run_id="rvprobe", timeout_seconds=90):
    input_deck = Path(input_deck).resolve()
    engine = discover_aspen_engine()
    if not engine:
        return {
            "status": "engine_executable_not_found",
            "message": "Could not find Aspen engine aspen.exe for diagnostic probe.",
        }
    setup = aspen_setup_for_engine(engine)
    work_dir = input_deck.parent
    run_token = agent.safe_file_token(run_id)[:24] or "rvprobe"
    probe_cmd = work_dir / "{}.cmd".format(run_token)
    script_lines = ["@echo off"]
    if setup:
        script_lines.append('call "{}"'.format(setup))
        script_lines.append("if errorlevel 1 exit /b %ERRORLEVEL%")
    script_lines.extend([
        'cd /d "{}"'.format(work_dir),
        '"{}" "{}" "{}" /log'.format(engine, input_deck.stem, run_token),
        "exit /b %ERRORLEVEL%",
    ])
    probe_cmd.write_text("\r\n".join(script_lines) + "\r\n", encoding="utf-8")
    try:
        completed = subprocess.run(
            ["cmd.exe", "/c", str(probe_cmd)],
            cwd=str(work_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "probe_timeout",
            "message": "Aspen engine diagnostic probe timed out after {} seconds.".format(timeout_seconds),
            "stdout_tail": short_text(getattr(exc, "stdout", "") or ""),
            "stderr_tail": short_text(getattr(exc, "stderr", "") or ""),
            "engine_exe": str(engine),
        }
    stdout_text = (completed.stdout or b"").decode("utf-8", errors="replace") if isinstance(completed.stdout, bytes) else (completed.stdout or "")
    stderr_text = (completed.stderr or b"").decode("utf-8", errors="replace") if isinstance(completed.stderr, bytes) else (completed.stderr or "")
    combined = "\n".join([stdout_text, stderr_text])
    status = "ok" if completed.returncode == 0 else "failed"
    if aspen_license_unavailable_text(combined):
        status = "license_unavailable"
    return {
        "status": status,
        "returncode": completed.returncode,
        "message": short_text(combined, 1000),
        "stdout_tail": short_text(stdout_text, 800),
        "stderr_tail": short_text(stderr_text, 800),
        "engine_exe": str(engine),
        "setup_bat": str(setup) if setup else "",
        "probe_run_id": run_token,
    }


def close_app(app, preserve_pids=None):
    # Aspen COM Close/Quit can hang after property-engine/license failures.
    # The agent records pre-existing AspenPlus PIDs and only removes the
    # automation processes opened by this quick flash run.
    try:
        del app
    except Exception:
        pass
    cleanup_new_aspen_processes(preserve_pids=preserve_pids)


def formula_for_mapping(mapping, fallback):
    return mapping.get("formula") or fallback or mapping.get("aspen_component_id") or mapping.get("code") or "C"


def component_record(mapping, fallback):
    component_id = mapping.get("aspen_component_id") or mapping.get("code") or fallback
    formula = formula_for_mapping(mapping, fallback)
    confidence = str(mapping.get("mapping_confidence") or mapping.get("confidence") or "").strip()
    role = mapping.get("role") or ""
    db_name = mapping.get("db_name") or mapping.get("aspen_component_id") or component_id
    if role == "candidate_solvent" and formula and formula != component_id:
        db_name = formula
    elif confidence in {"needs_aspen_probe", "unverified_aspen_alias", "pubchem_formula_preflight_needs_aspen_probe"} and formula and formula != component_id:
        db_name = formula
    return {
        "id": component_id,
        "aspen_component_id": mapping.get("aspen_component_id") or component_id,
        "db_name": db_name,
        "formula": formula,
        "name": mapping.get("name") or fallback or component_id,
    }


def clamp_composition(value):
    value = agent.safe_float(value)
    if value is None:
        return None
    return max(0.001, min(0.999, float(value)))


def composition_token(value):
    value = clamp_composition(value)
    if value is None:
        return "xunk"
    return ("x{:.4f}".format(value)).replace(".", "p")


def task_overhead_fraction(report, row=None):
    task = (report or {}).get("separation_task") or {}
    row = row or {}
    specified = clamp_composition(
        row.get("x_overhead_liquid_spec")
        or row.get("x_lk")
        or row.get("x_light_key")
        or row.get("x_overhead")
    )
    if specified is not None:
        return specified
    frac_a, frac_b = agent.parse_feed_fraction_pair(task)
    roles = agent.relative_volatility_role_summary(task, lookup_rows=(report or {}).get("online_component_info") or [])
    overhead_token = agent.component_token(row.get("overhead_component") or roles.get("alpha_overhead_component") or roles.get("alpha_overhead_token"))
    component_a_token = agent.component_token(task.get("component_a"))
    if overhead_token and overhead_token == component_a_token:
        return clamp_composition(frac_a)
    return clamp_composition(frac_b)


def feed_fractions_for_case(report, row):
    task = (report or {}).get("separation_task") or {}
    frac_a, frac_b = agent.parse_feed_fraction_pair(task)
    x_overhead = task_overhead_fraction(report, row)
    roles = agent.relative_volatility_role_summary(task, lookup_rows=(report or {}).get("online_component_info") or [])
    overhead_token = agent.component_token((row or {}).get("overhead_component") or roles.get("alpha_overhead_component") or roles.get("alpha_overhead_token"))
    component_a_token = agent.component_token(task.get("component_a"))
    component_b_token = agent.component_token(task.get("component_b"))
    if x_overhead is None or not overhead_token:
        return frac_a, frac_b
    if overhead_token == component_a_token:
        return x_overhead, 1.0 - x_overhead
    if overhead_token == component_b_token:
        return 1.0 - x_overhead, x_overhead
    return frac_a, frac_b


def build_case(report, row, solvent=None, base=False):
    task = report.get("separation_task") or {}
    frac_a, frac_b = feed_fractions_for_case(report, row)
    component_a = agent.aspen_identity_for_item(report, task.get("component_a", ""), role="feed_component")
    component_b = agent.aspen_identity_for_item(report, task.get("component_b", ""), role="feed_component")
    components = [
        component_record(component_a, task.get("component_a")),
        component_record(component_b, task.get("component_b")),
    ]
    feed_total = 10.0
    flows = {
        component_a.get("aspen_component_id"): feed_total * frac_a,
        component_b.get("aspen_component_id"): feed_total * frac_b,
    }
    solvent_mapping = None
    if not base and solvent:
        solvent_mapping = agent.aspen_identity_for_item(
            report,
            solvent.get("code") or row.get("solvent_code"),
            name=solvent.get("name") or row.get("solvent_name"),
            explicit_aspen_id=solvent.get("aspen_component_id") or row.get("aspen_component_id") or "",
            role="candidate_solvent",
        )
        solvent_record = component_record(solvent_mapping, solvent.get("code") or row.get("solvent_code"))
        if not solvent_record.get("id") or not solvent_record.get("db_name"):
            raise ValueError("Missing Aspen component identity for solvent {}".format(row.get("solvent_code") or ""))
        components.append(solvent_record)
        sf_ratio = float(row.get("sf_ratio") or 1.0)
        flows[solvent_record.get("id")] = feed_total * sf_ratio
    pressure_mpa = agent.safe_float(task.get("feed_pressure_mpa"), 0.101325) or 0.101325
    temperature_c = agent.safe_float(row.get("temperature_basis_c"), None)
    if temperature_c is None:
        temperature_c = agent.safe_float(task.get("feed_temperature_c"), 25.0) or 25.0
    row_method = row.get("property_method")
    if str(row_method or "").strip().lower() in {"", "to_be_selected", "none"}:
        row_method = ""
    property_method = (
        row_method
        or (((report.get("system_research_brief") or {}).get("thermo_method_confirmation") or {}).get("execution_method_candidate"))
        or task.get("property_method")
        or "NRTL"
    )
    if str(property_method).lower() in {"", "to_be_selected", "none"}:
        property_method = "NRTL"
    try:
        property_method = agent.normalize_aspen_property_method(property_method) or property_method
    except Exception:
        pass
    evidence_candidate = None if base else (solvent or {})
    component_id_map = {
        task.get("component_a"): component_a.get("aspen_component_id"),
        task.get("component_b"): component_b.get("aspen_component_id"),
    }
    if solvent_mapping:
        component_id_map[
            (solvent or {}).get("code")
            or (solvent or {}).get("name")
            or row.get("solvent_code")
        ] = solvent_mapping.get("aspen_component_id")
    evidence_gate = thermo_evidence_gate.evaluate_candidate(
        report,
        candidate=evidence_candidate,
        selected_method=property_method,
    )
    if not task.get("compatible_verified_case") and not evidence_gate.get("aspen_run_authorized"):
        missing = "; ".join(
            "{}/{} ({})".format(
                *((item.get("pair") or ["", ""])[:2]),
                item.get("status") or "missing",
            )
            for item in evidence_gate.get("missing_pairs") or []
        )
        raise ValueError(
            "{}: Aspen input generation is blocked by thermodynamic evidence. {}".format(
                evidence_gate.get("status") or "blocked_thermo_evidence_incomplete",
                missing or evidence_gate.get("reason") or "required binary evidence is incomplete",
            )
        )
    nrtl_pairs = thermo_evidence_gate.nrtl_pairs_for_candidate(
        report,
        candidate=evidence_candidate,
        selected_method=property_method,
        component_id_map=component_id_map,
    )
    return {
        "components": [item for item in components if item.get("id")],
        "flows": flows,
        "pressure_bar": pressure_mpa * 10.0,
        "temperature_c": temperature_c,
        "property_method": property_method,
        "property_keyword": property_method,
        "thermodynamic_evidence_gate": evidence_gate,
        "nrtl_pairs": nrtl_pairs,
        "vapor_fraction": 0.5,
        "flash_mode": "vfrac",
    }


def read_flash_k_values(app, row):
    overhead_id = row.get("overhead_aspen_component_id") or row.get("overhead_component")
    bottom_id = row.get("bottom_aspen_component_id") or row.get("bottom_component")
    y_over, y_over_path = stream_mole_fraction(app, "V", overhead_id)
    y_bottom, y_bottom_path = stream_mole_fraction(app, "V", bottom_id)
    x_over, x_over_path = stream_mole_fraction(app, "L", overhead_id)
    x_bottom, x_bottom_path = stream_mole_fraction(app, "L", bottom_id)
    if y_over is None:
        y_over, y_over_path = block_component_value(app, ["Y", "YOUT", "MOLEFRAC-V", "VFRAC"], overhead_id)
    if y_bottom is None:
        y_bottom, y_bottom_path = block_component_value(app, ["Y", "YOUT", "MOLEFRAC-V", "VFRAC"], bottom_id)
    if x_over is None:
        x_over, x_over_path = block_component_value(app, ["X", "XOUT", "MOLEFRAC-L", "LFRAC"], overhead_id)
    if x_bottom is None:
        x_bottom, x_bottom_path = block_component_value(app, ["X", "XOUT", "MOLEFRAC-L", "LFRAC"], bottom_id)
    k_over = y_over / x_over if y_over is not None and x_over not in {None, 0.0} else None
    k_bottom = y_bottom / x_bottom if y_bottom is not None and x_bottom not in {None, 0.0} else None
    k_over_direct, k_over_path = block_component_value(app, ["B_K", "K", "KMX", "KVALUE"], overhead_id)
    k_bottom_direct, k_bottom_path = block_component_value(app, ["B_K", "K", "KMX", "KVALUE"], bottom_id)
    if k_over is None:
        k_over = k_over_direct
    if k_bottom is None:
        k_bottom = k_bottom_direct
    alpha = k_over / k_bottom if k_over is not None and k_bottom not in {None, 0.0} else None
    return {
        "y_overhead": y_over,
        "x_overhead": x_over,
        "y_bottom": y_bottom,
        "x_bottom": x_bottom,
        "k_overhead": k_over,
        "k_bottom": k_bottom,
        "alpha": alpha,
        "vapor_temperature_c": stream_temperature_c(app, "V"),
        "liquid_temperature_c": stream_temperature_c(app, "L"),
        "read_paths": "; ".join([p for p in [y_over_path, x_over_path, y_bottom_path, x_bottom_path, k_over_path, k_bottom_path] if p]),
    }


def solvent_free_overhead_fraction(values, fallback=None):
    values = values or {}
    x_overhead = agent.safe_float(values.get("x_overhead"))
    x_bottom = agent.safe_float(values.get("x_bottom"))
    if x_overhead is not None and x_bottom is not None and (x_overhead + x_bottom) > 0:
        return max(0.001, min(0.999, x_overhead / (x_overhead + x_bottom)))
    return clamp_composition(fallback)


def property_trust_issue(history):
    upper = str(history or "").upper()
    if "NOT IN A DATA BANK" in upper or "MW MISSING" in upper:
        return "blocked_aspen_component_identity", "Aspen component identity is not valid in the active databank."
    zero_parameter_markers = [
        "NRTL BINARY PARAMETERS FOR ALL COMPONENT PAIRS ARE ZERO",
        "BINARY PARAMETERS FOR ALL COMPONENT PAIRS ARE ZERO",
        "BINARY PARAMETERS ARE ZERO",
    ]
    if any(marker in upper for marker in zero_parameter_markers):
        return (
            "blocked_property_parameters_untrusted",
            "Aspen ran, but binary interaction parameters are all zero; the Agent treats this VLE/alpha result as not publication-trustworthy.",
        )
    if "PROPERTY PARAMETER ERROR" in upper or "MISSING PARAMETERS" in upper:
        return "blocked_property_parameters_missing", "Aspen reports missing property parameters for this method/component set."
    return "trusted", ""


def aspen_run_trust_issue(run):
    run = run or {}
    history = run.get("aspen_history_tail") or ""
    trust_status, trust_reason = property_trust_issue(history)
    if trust_status != "trusted":
        return trust_status, trust_reason
    error = str(run.get("error_message") or "")
    combined = "\n".join([str(history or ""), error])
    upper = combined.upper()
    if (
        "ASPEN COM COULD NOT OPEN THE GENERATED INPUT DECK" in upper
        or "INITFROMFILE" in upper
        or "CANNOT OPEN FILE" in upper
        or "UNABLE TO OPEN FILE" in upper
        or "REMOTE PROCEDURE CALL FAILED" in upper
        or "远程过程调用失败" in combined
        or "鏃犳硶鎵撳紑鏂囦欢" in combined
    ):
        return (
            "blocked_aspen_input_deck_open_failed",
            "Aspen COM could not open the generated input deck; retry with a standard Aspen property-method keyword/deck before trusting alpha.",
        )
    if aspen_license_unavailable_text(combined):
        return (
            "blocked_aspen_property_license",
            "Aspen property engine license is unavailable; recover Aspen Properties before trusting alpha.",
        )
    if (
        "INITFROMFILE" in upper
        or "CANNOT OPEN FILE" in upper
        or "UNABLE TO OPEN FILE" in upper
        or "无法打开文件" in combined
    ):
        return (
            "blocked_aspen_input_deck_open_failed",
            "Aspen COM could not open the generated input deck; retry with a standard Aspen property-method keyword/deck before trusting alpha.",
        )
    if "PYWINTYPES.COM_ERROR" in upper or ("OLE" in upper and "ASPEN" in upper):
        return (
            "blocked_aspen_com_execution_failed",
            "Aspen COM failed before producing a readable result; this alpha row is not trusted.",
        )
    if run.get("status") not in {"", None, "run_complete"} and error:
        return (
            "blocked_aspen_run_failed",
            "Aspen failed before producing a readable result; this alpha row is not trusted. {}".format(short_text(error, 500)),
        )
    return "trusted", ""


def base_no_k_diagnostic(base_run, base_values, probe):
    base_run = base_run or {}
    run_error = str(base_run.get("error_message") or "").strip()
    run_status = str(base_run.get("status") or "").strip()
    if run_status and run_status != "run_complete":
        trust_status, trust_reason = aspen_run_trust_issue(base_run)
        if trust_status.startswith("blocked_") and trust_status != "blocked_aspen_property_license":
            if run_error:
                return "{} Raw Aspen/COM error: {}".format(trust_reason, short_text(run_error, 700))
            return trust_reason
        if run_error:
            return short_text(run_error, 1000)
    probe = probe or {}
    if aspen_probe_license_unavailable(probe):
        return (
            "Aspen property engine license unavailable during alpha flash probe: "
            "Properties license is not available. The Agent will not enter Gaussian/GROMACS "
            "until Aspen can produce real K values."
        )
    if base_run.get("status") != "run_complete":
        return short_text(base_run.get("error_message") or "Aspen base binary flash failed before producing K values.")
    paths = (base_values or {}).get("read_paths") or ""
    if paths:
        return (
            "Aspen opened and ran the base binary flash, but the K-value/phase-composition nodes were empty "
            "or zero. The Agent treated this as no real alpha0; read paths: {}"
        ).format(paths)
    if probe.get("status"):
        return (
            "Aspen opened through COM but produced no base-binary K values. Diagnostic engine probe status: {}; {}"
        ).format(probe.get("status"), short_text(probe.get("message") or "", 500))
    return "Aspen base binary flash did not return K values, so alpha0 is unavailable."


def case_no_k_status_and_error(run, values):
    history = run.get("aspen_history_tail") or ""
    trust_status, trust_reason = property_trust_issue(history)
    if trust_status == "blocked_aspen_component_identity":
        return "blocked_aspen_component_identity", (
            "Aspen component identity is not valid in the active databank; "
            "resolve the candidate Aspen ID/CAS before using this solvent. "
            + short_text(history, 900)
        )
    if trust_status == "blocked_property_parameters_missing":
        return "blocked_property_parameters_missing", (
            "Aspen property parameters are missing for this candidate/property method. "
            + short_text(history, 900)
        )
    if trust_status == "blocked_property_parameters_untrusted":
        return trust_status, trust_reason + " " + short_text(history, 900)
    if history:
        return "no_k_values", "Aspen run completed but no K values were readable. " + short_text(history, 900)
    return "no_k_values", "Aspen run completed but no K values were readable from expected stream/block outputs."


def unique_solvents(pool_items, max_candidates):
    items = []
    seen = set()
    for row in pool_items or []:
        code = agent.component_token(row.get("code"))
        if not code or code in seen:
            continue
        seen.add(code)
        items.append(row)
        if max_candidates and len(items) >= max_candidates:
            break
    return items


def completed_solvent_codes(rows):
    codes = set()
    for row in rows or []:
        code = component_id_token(row.get("solvent_code") or row.get("code"))
        status = str(row.get("status") or "").strip().lower()
        if not code or not status or status.startswith("base_"):
            continue
        codes.add(code)
    return codes


def base_result_rows(rows):
    result = []
    for row in rows or []:
        status = str(row.get("status") or "").strip().lower()
        case_id = str(row.get("case_id") or "").strip().upper()
        if case_id.startswith("BASE") or status.startswith("base_"):
            result.append(row)
    return result


def add_grid_point(points, value, zone):
    value = clamp_composition(value)
    if value is None:
        return
    key = round(value, 4)
    if key not in points:
        points[key] = {"x": key, "composition_zone": zone}
    elif zone not in str(points[key].get("composition_zone") or ""):
        points[key]["composition_zone"] = "{}+{}".format(points[key].get("composition_zone"), zone)


def planned_base_scan_grid(report, template_row):
    points = {}
    for value in [0.001, 0.005, 0.01, 0.03, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.97, 0.99, 0.995, 0.999]:
        add_grid_point(points, value, "coarse_vle_scan")
    feed_x = task_overhead_fraction(report, template_row)
    for delta in [0.0, -0.15, -0.10, -0.05, 0.05, 0.10, 0.15]:
        add_grid_point(points, (feed_x or 0.5) + delta, "feed_neighborhood")
    task = (report or {}).get("separation_task") or {}
    roles = agent.relative_volatility_role_summary(task, lookup_rows=(report or {}).get("online_component_info") or [])
    target_purity = agent.safe_float(task.get("target_purity"), 0.99) or 0.99
    if roles.get("target_product_position") == "overhead":
        for value in [0.90, 0.95, 0.99, target_purity]:
            add_grid_point(points, value, "overhead_product_end")
    elif roles.get("target_product_position") == "bottom":
        for value in [0.001, 0.005, 0.01, 0.03, 0.05, max(0.001, 1.0 - target_purity)]:
            add_grid_point(points, value, "bottom_product_end")
    return [points[key] for key in sorted(points)]


def base_scan_row(template_row, point):
    row = dict(template_row or {})
    row.update({
        "case_id": "BASE_{}".format(composition_token(point.get("x"))),
        "solvent_code": "",
        "solvent_name": "",
        "aspen_component_id": "",
        "sf_ratio": 0,
        "x_overhead_liquid_spec": point.get("x"),
        "composition_zone": point.get("composition_zone") or "base_vle_scan",
        "vle_scan_role": "base_binary_vle_scan",
    })
    return row


def alpha_from_row(row):
    alpha = agent.safe_float((row or {}).get("alpha") or (row or {}).get("alpha_0") or (row or {}).get("alpha_s"))
    if alpha is not None:
        return alpha
    k_over = agent.safe_float((row or {}).get("k_overhead") or (row or {}).get("K_overhead"))
    k_bottom = agent.safe_float((row or {}).get("k_bottom") or (row or {}).get("K_bottom"))
    if k_over is not None and k_bottom not in {None, 0}:
        return k_over / k_bottom
    return None


def detect_vle_scan_summary(report, base_rows):
    task = (report or {}).get("separation_task") or {}
    roles = agent.relative_volatility_role_summary(task, lookup_rows=(report or {}).get("online_component_info") or [])
    points = []
    for row in base_rows or []:
        alpha = alpha_from_row(row)
        x = clamp_composition(row.get("x_overhead") or row.get("x_overhead_liquid_spec"))
        y = agent.safe_float(row.get("y_overhead"))
        if x is None or alpha is None:
            continue
        points.append({
            "x_overhead_liquid": x,
            "y_overhead_vapor": y,
            "alpha": alpha,
            "diff_y_minus_x": (y - x) if y is not None else None,
            "case_id": row.get("case_id") or "",
        })
    points.sort(key=lambda item: item["x_overhead_liquid"])
    azeotrope_x = None
    azeotrope_source = ""
    for left, right in zip(points, points[1:]):
        dl = left.get("diff_y_minus_x")
        dr = right.get("diff_y_minus_x")
        if dl is None or dr is None:
            continue
        if dl == 0:
            azeotrope_x = left["x_overhead_liquid"]
            azeotrope_source = "exact_y_equals_x"
            break
        if dl * dr < 0:
            span = right["x_overhead_liquid"] - left["x_overhead_liquid"]
            denom = abs(dl) + abs(dr)
            azeotrope_x = left["x_overhead_liquid"] + span * (abs(dl) / denom if denom else 0.5)
            azeotrope_source = "interpolated_sign_change"
            break
    hard = None
    if points:
        hard = min(points, key=lambda item: abs((item.get("alpha") or 1.0) - 1.0))
    bp_diff = None
    try:
        bp_diff = abs(float(roles.get("alpha_bottom_bp_c")) - float(roles.get("alpha_overhead_bp_c")))
    except Exception:
        bp_diff = None
    near_low_alpha = bool(hard and abs((hard.get("alpha") or 1.0) - 1.0) <= 0.25)
    near_bp = bool(bp_diff is not None and bp_diff <= 10.0)
    if azeotrope_x is not None:
        classification = "azeotropic_binary"
    elif near_low_alpha or near_bp:
        classification = "near_boiling_low_relative_volatility"
    else:
        classification = "ordinary_binary_vle"
    return {
        "status": "base_vle_scan_imported" if points else "base_vle_scan_missing",
        "classification": classification,
        "alpha_definition": roles.get("alpha_definition") or "",
        "azeotrope_x_overhead_liquid": round(azeotrope_x, 5) if azeotrope_x is not None else "",
        "azeotrope_detection": azeotrope_source,
        "hardest_x_overhead_liquid": round(hard.get("x_overhead_liquid"), 5) if hard else "",
        "hardest_alpha": round(hard.get("alpha"), 5) if hard else "",
        "normal_bp_delta_c": round(bp_diff, 3) if bp_diff is not None else "",
        "base_scan_point_count": len(points),
        "policy": (
            "Azeotropic binaries are screened around x_az; near-boiling/non-azeotropic binaries are screened "
            "around feed, product ends, and the base-VLE point where alpha is closest to 1."
        ),
        "points": points,
    }


def key_composition_grid(report, template_row, scan_summary):
    points = {}
    feed_x = task_overhead_fraction(report, template_row) or 0.5
    for delta in [0.0, -0.10, -0.05, 0.05, 0.10]:
        add_grid_point(points, feed_x + delta, "feed_neighborhood")
    azeotrope_x = agent.safe_float((scan_summary or {}).get("azeotrope_x_overhead_liquid"))
    hard_x = agent.safe_float((scan_summary or {}).get("hardest_x_overhead_liquid"))
    if azeotrope_x is not None:
        for delta in [0.0, -0.10, -0.05, -0.02, 0.02, 0.05, 0.10]:
            add_grid_point(points, azeotrope_x + delta, "azeotrope_neighborhood")
    elif hard_x is not None:
        for delta in [0.0, -0.10, -0.05, -0.02, 0.02, 0.05, 0.10]:
            add_grid_point(points, hard_x + delta, "alpha_hard_neighborhood")
    task = (report or {}).get("separation_task") or {}
    roles = agent.relative_volatility_role_summary(task, lookup_rows=(report or {}).get("online_component_info") or [])
    target_purity = agent.safe_float(task.get("target_purity"), 0.99) or 0.99
    if roles.get("target_product_position") == "overhead":
        for value in [0.90, 0.95, 0.99, target_purity]:
            add_grid_point(points, value, "overhead_product_end")
    elif roles.get("target_product_position") == "bottom":
        for value in [0.001, 0.005, 0.01, 0.03, 0.05, max(0.001, 1.0 - target_purity)]:
            add_grid_point(points, value, "bottom_product_end")
    ordered = [points[key] for key in sorted(points)]
    if len(ordered) > 14:
        critical = [p for p in ordered if any(token in p.get("composition_zone", "") for token in ["feed", "azeotrope", "alpha_hard"])]
        ends = [p for p in ordered if "product_end" in p.get("composition_zone", "")]
        ordered = (critical + ends)[:14]
        ordered = sorted({round(p["x"], 4): p for p in ordered}.values(), key=lambda p: p["x"])
    return ordered


def expand_requests_for_key_grid(requests, key_grid, scan_summary):
    expanded = []
    unique = []
    seen = set()
    for row in requests or []:
        key = (
            str(row.get("solvent_code") or row.get("aspen_component_id") or "").upper(),
            str(row.get("sf_ratio") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    for row in unique:
        for point in key_grid:
            item = dict(row)
            item["x_overhead_liquid_spec"] = point.get("x")
            item["composition_zone"] = point.get("composition_zone") or ""
            item["vle_scan_kind"] = (scan_summary or {}).get("classification") or ""
            expanded.append(item)
    return expanded


def interpolated_base_alpha(base_rows, x_value):
    x_value = clamp_composition(x_value)
    points = []
    for row in base_rows or []:
        alpha = alpha_from_row(row)
        x = clamp_composition(row.get("x_overhead") or row.get("x_overhead_liquid_spec"))
        if alpha is not None and x is not None:
            points.append({"x": x, "alpha": alpha, "row": row})
    if not points:
        return None, {}
    points.sort(key=lambda item: item["x"])
    if x_value is None:
        best = points[0]
        return best["alpha"], {
            "base_alpha_match_method": "first_available_no_target_x",
            "base_alpha_x": best["x"],
            "base_case_id": best["row"].get("case_id") or "",
        }
    nearest = min(points, key=lambda item: abs(item["x"] - x_value))
    if abs(nearest["x"] - x_value) <= 1e-6 or len(points) == 1:
        return nearest["alpha"], {
            "base_alpha_match_method": "exact_or_nearest",
            "base_alpha_x": nearest["x"],
            "base_case_id": nearest["row"].get("case_id") or "",
        }
    lower = None
    upper = None
    for point in points:
        if point["x"] <= x_value:
            lower = point
        if point["x"] >= x_value and upper is None:
            upper = point
    if lower and upper and upper["x"] != lower["x"]:
        weight = (x_value - lower["x"]) / (upper["x"] - lower["x"])
        alpha = lower["alpha"] + weight * (upper["alpha"] - lower["alpha"])
        return alpha, {
            "base_alpha_match_method": "linear_interpolation_actual_liquid_x",
            "base_alpha_x": x_value,
            "base_alpha_x_low": lower["x"],
            "base_alpha_x_high": upper["x"],
            "base_alpha_low": lower["alpha"],
            "base_alpha_high": upper["alpha"],
            "base_case_id_low": lower["row"].get("case_id") or "",
            "base_case_id_high": upper["row"].get("case_id") or "",
        }
    edge = lower or upper or nearest
    return edge["alpha"], {
        "base_alpha_match_method": "nearest_edge_actual_liquid_x",
        "base_alpha_x": edge["x"],
        "base_case_id": edge["row"].get("case_id") or "",
    }


def nearest_base_alpha(base_rows, x_value):
    alpha, detail = interpolated_base_alpha(base_rows, x_value)
    return alpha, {"case_id": detail.get("base_case_id") or detail.get("base_case_id_low") or ""}


def prepare_requests(report, max_candidates=8, skip_codes=None, candidate_code="", property_method=""):
    report = agent.enrich_trial_report_payload(report)
    pool = ((report.get("system_research_brief") or {}).get("candidate_solvent_pool") or {}).get("items") or []
    skip_codes = {component_id_token(code) for code in (skip_codes or []) if component_id_token(code)}
    candidate_code = component_id_token(candidate_code)
    if candidate_code:
        pool = [
            row for row in pool
            if candidate_code in {
                component_id_token(row.get("code")),
                component_id_token(row.get("name")),
                component_id_token(row.get("aspen_component_id")),
            }
        ]
    if skip_codes:
        pool = [
            row for row in pool
            if component_id_token(row.get("code") or row.get("name") or row.get("aspen_component_id")) not in skip_codes
        ]
    selected = unique_solvents(pool, max_candidates)
    requests = agent.relative_volatility_kvalue_request_rows(report, selected)
    task = report.get("separation_task") or {}
    component_a = agent.aspen_identity_for_item(report, task.get("component_a", ""), role="feed_component")
    component_b = agent.aspen_identity_for_item(report, task.get("component_b", ""), role="feed_component")
    roles = agent.relative_volatility_role_summary(task, lookup_rows=(report or {}).get("online_component_info") or [])
    for row in requests:
        if property_method:
            row["property_method"] = property_method
        row["overhead_aspen_component_id"] = (
            component_a.get("aspen_component_id")
            if agent.component_token(row.get("overhead_component")) == agent.component_token(task.get("component_a"))
            else component_b.get("aspen_component_id")
        )
        row["bottom_aspen_component_id"] = (
            component_a.get("aspen_component_id")
            if agent.component_token(row.get("bottom_component")) == agent.component_token(task.get("component_a"))
            else component_b.get("aspen_component_id")
        )
        row["alpha_0_proxy"] = roles.get("alpha_0") or ""
    return report, selected, requests


def evidence_selected_property_method(report, explicit_method=""):
    task = (report or {}).get("separation_task") or {}
    thermo = ((report.get("system_research_brief") or {}).get("thermo_method_confirmation") or {})
    summary = thermo.get("thermodynamic_evidence_gate") or thermo_evidence_gate.build_system_evidence_summary(report)
    method = (
        explicit_method
        or thermo.get("execution_method_candidate")
        or summary.get("recommended_method")
        or task.get("property_method")
        or ""
    )
    try:
        return agent.normalize_aspen_property_method(method) or method
    except Exception:
        return method


def evidence_block_row(request, gate):
    item = dict(request or {})
    missing = gate.get("missing_pairs") or []
    missing_text = "; ".join(
        "{}/{} ({})".format(
            *((row.get("pair") or ["", ""])[:2]),
            row.get("status") or "missing",
        )
        for row in missing
    )
    item.update({
        "status": gate.get("status") or "blocked_thermo_evidence_incomplete",
        "property_trust_status": gate.get("status") or "blocked_thermo_evidence_incomplete",
        "property_trust_reason": gate.get("reason") or "Thermodynamic evidence gate did not pass.",
        "error_message": (
            (gate.get("reason") or "Thermodynamic evidence gate did not pass.")
            + (" Missing pairs: {}.".format(missing_text) if missing_text else "")
        ),
        "thermo_evidence_source": gate.get("evidence_source") or "",
        "thermo_selected_method": gate.get("selected_method") or "",
        "thermo_ready_pair_count": gate.get("ready_pair_count") or 0,
        "thermo_publication_ready_pair_count": gate.get("publication_ready_pair_count") or 0,
        "thermo_screening_ready_pair_count": gate.get("screening_ready_pair_count") or 0,
        "thermo_required_pair_count": gate.get("required_pair_count") or 0,
        "thermo_missing_pairs": missing_text,
        "thermo_claim_level": gate.get("claim_level") or "blocked",
        "thermo_aspen_execution_mode": gate.get("aspen_execution_mode") or "blocked",
        "thermo_publication_ready": bool(gate.get("publication_ready")),
        "thermo_screening_ready": bool(gate.get("screening_ready")),
        "thermo_requires_uncertainty_campaign": bool(gate.get("requires_uncertainty_campaign")),
        "aspen_run_authorized": False,
        "pass_alpha_gate": False,
    })
    return item


def run_prescreen(report_path, output_csv="", work_dir="", max_candidates=8, max_rows=0, force=False, skip_completed=False, candidate_code="", property_method=""):
    report_path = Path(report_path).resolve()
    report = agent.read_json(report_path)
    output_dir = Path(report.get("output_dir") or report_path.parent)
    package_dir = output_dir / "research_task_package"
    work_dir = Path(work_dir or (package_dir / "relative_volatility_flash"))
    work_dir.mkdir(parents=True, exist_ok=True)
    output_csv = Path(output_csv or (package_dir / "relative_volatility_kvalue_results.csv"))
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    existing_rows = agent.read_csv_rows(output_csv) if output_csv.exists() and (skip_completed or candidate_code) and not force else []
    skip_codes = completed_solvent_codes(existing_rows) if skip_completed and not force else set()
    report, solvent_items, requests = prepare_requests(
        report,
        max_candidates=max_candidates,
        skip_codes=skip_codes,
        candidate_code=candidate_code,
        property_method=property_method,
    )
    raw_requests = list(requests)
    solvent_by_code = {agent.component_token(row.get("code")): row for row in solvent_items}
    rows = list(existing_rows)
    selected_property_method = evidence_selected_property_method(report, property_method)
    if selected_property_method:
        raw_requests = [dict(request, property_method=selected_property_method) for request in raw_requests]
        requests = list(raw_requests)
    candidate_gates = {}
    for solvent in solvent_items:
        code = agent.component_token(solvent.get("code") or solvent.get("aspen_component_id") or solvent.get("name"))
        if not code:
            continue
        candidate_gates[code] = thermo_evidence_gate.evaluate_candidate(
            report,
            candidate=solvent,
            selected_method=selected_property_method,
        )
    thermo_audit = {
        "selected_property_method": selected_property_method,
        "policy": "Publication claims require original-source validation for all three pairs; exploratory alpha runs may use qualified two-family predictive consensus constrained by experiments.",
        "candidate_gates": list(candidate_gates.values()),
    }
    thermo_audit_path = package_dir / "thermodynamic_evidence_candidate_audit.json"
    agent.write_json_file(thermo_audit_path, thermo_audit)
    report_paths = dict(report.get("research_task_package_paths") or {})
    report_paths["thermodynamic_evidence_candidate_audit_json"] = str(thermo_audit_path.resolve())
    report["research_task_package_paths"] = report_paths

    ready_codes = {
        code for code, gate in candidate_gates.items()
        if gate.get("aspen_run_authorized")
    }
    blocked_requests = []
    ready_requests = []
    for request in raw_requests:
        code = agent.component_token(request.get("solvent_code"))
        gate = candidate_gates.get(code) or thermo_evidence_gate.evaluate_candidate(
            report,
            candidate=solvent_by_code.get(code) or {
                "code": request.get("solvent_code") or request.get("aspen_component_id") or "",
                "aspen_component_id": request.get("aspen_component_id") or "",
            },
            selected_method=selected_property_method,
        )
        if code in ready_codes and gate.get("aspen_run_authorized"):
            ready_request = dict(request)
            ready_request.update({
                "thermo_claim_level": gate.get("claim_level") or "blocked",
                "thermo_aspen_execution_mode": gate.get("aspen_execution_mode") or "blocked",
                "thermo_publication_ready": bool(gate.get("publication_ready")),
                "thermo_screening_ready": bool(gate.get("screening_ready")),
                "thermo_requires_uncertainty_campaign": bool(gate.get("requires_uncertainty_campaign")),
                "thermo_publication_ready_pair_count": gate.get("publication_ready_pair_count") or 0,
                "thermo_screening_ready_pair_count": gate.get("screening_ready_pair_count") or 0,
                "thermo_required_pair_count": gate.get("required_pair_count") or 0,
                "aspen_run_authorized": True,
            })
            ready_requests.append(ready_request)
        else:
            blocked_requests.append(evidence_block_row(request, gate))
    if blocked_requests:
        rows.extend(blocked_requests)
        agent.write_csv_file(output_csv, rows)
    raw_requests = ready_requests
    requests = list(ready_requests)
    solvent_items = [
        row for row in solvent_items
        if agent.component_token(row.get("code") or row.get("aspen_component_id") or row.get("name")) in ready_codes
    ]
    solvent_by_code = {agent.component_token(row.get("code")): row for row in solvent_items}
    if blocked_requests and not ready_requests:
        report["thermodynamic_evidence_candidate_audit"] = thermo_audit
        report = agent.enrich_trial_report_payload(report)
        agent.write_json_file(report_path, report)
        return {
            "status": "blocked_thermo_evidence_incomplete",
            "rows": len(rows),
            "base_status": "not_run_thermo_evidence_gate",
            "base_alpha": None,
            "base_error": "Aspen was not started because no requested entrainer passed all three binary-pair evidence checks.",
            "engine_probe_status": "not_run",
            "output_csv": str(output_csv.resolve()),
            "work_dir": str(work_dir.resolve()),
            "thermodynamic_evidence_audit": str(thermo_audit_path.resolve()),
        }
    base_scan_rows = base_result_rows(rows)
    base_summary = {}
    base_request = dict(raw_requests[0]) if raw_requests else {}
    base_alpha = None
    base_status = "not_run"
    base_error = ""
    base_probe = {}
    base_probe_license_is_primary = False
    last_base_inp = None
    last_base_run = {}
    if base_request and base_scan_rows:
        base_status = "base_vle_scan_imported"
        base_summary = detect_vle_scan_summary(report, base_scan_rows)
        blocked_trust_rows = [
            row for row in base_scan_rows
            if str(row.get("property_trust_status") or "").startswith("blocked_")
        ]
        if blocked_trust_rows:
            first_block = blocked_trust_rows[0]
            base_summary["property_trust_status"] = first_block.get("property_trust_status") or ""
            base_summary["property_trust_reason"] = first_block.get("property_trust_reason") or first_block.get("error_message") or ""
            base_summary["property_trust_blocked_point_count"] = len(blocked_trust_rows)
        else:
            base_summary["property_trust_status"] = "trusted"
        base_alpha, _ = interpolated_base_alpha(base_scan_rows, task_overhead_fraction(report, base_request))
    elif base_request:
        for point in planned_base_scan_grid(report, base_request):
            scan_request = base_scan_row(base_request, point)
            base_case = build_case(report, scan_request, base=True)
            base_inp = write_input_deck(work_dir / "{}.inp".format(agent.safe_file_token(scan_request["case_id"])), base_case)
            last_base_inp = base_inp
            base_run = run_aspen_case(base_inp)
            last_base_run = dict(base_run)
            base_status = base_run.get("status")
            base_values = {}
            app = base_run.get("app")
            item = dict(scan_request, input_deck=str(base_inp.resolve()))
            item["aspen_run_status"] = base_status or ""
            item["aspen_raw_error_message"] = short_text(base_run.get("error_message") or "", 1500)
            item["aspen_runtime_mode"] = base_run.get("aspen_runtime_mode") or ""
            item["aspen_runtime_dir"] = base_run.get("aspen_runtime_dir") or ""
            item["aspen_runtime_input_deck"] = base_run.get("aspen_runtime_input_deck") or ""
            item["aspen_runtime_copied_outputs"] = base_run.get("aspen_runtime_copied_outputs") or ""
            item["aspen_history_path"] = base_run.get("aspen_history_path") or ""
            item["aspen_history_tail"] = base_run.get("aspen_history_tail") or ""
            trust_status, trust_reason = aspen_run_trust_issue(base_run)
            item["property_trust_status"] = trust_status
            item["property_trust_reason"] = trust_reason
            if app is not None:
                try:
                    base_values = read_flash_k_values(app, scan_request)
                    alpha_here = base_values.get("alpha")
                    item.update(base_values)
                    item.update({
                        "alpha_0": alpha_here,
                        "alpha_s": alpha_here,
                        "alpha_improvement": 1.0 if alpha_here else "",
                        "status": "base_vle_scan_imported" if alpha_here else "base_binary_no_k_values",
                        "error_message": "" if alpha_here else base_no_k_diagnostic(base_run, base_values, {}),
                    })
                    if trust_status != "trusted":
                        item["status"] = trust_status
                        item["error_message"] = trust_reason
                    if alpha_here and base_alpha is None:
                        base_alpha = alpha_here
                finally:
                    close_app(app, preserve_pids=base_run.get("preserve_aspen_pids"))
            else:
                item.update({
                    "status": trust_status if trust_status.startswith("blocked_") else "base_binary_failed",
                    "error_message": trust_reason or base_run.get("error_message") or "",
                })
            rows.append(item)
            base_scan_rows.append(item)
            agent.write_csv_file(output_csv, rows)
        if not any(alpha_from_row(row) is not None for row in base_scan_rows):
            if last_base_inp:
                base_probe = run_aspen_engine_probe(last_base_inp, run_id="rvbase")
            base_error = base_no_k_diagnostic(last_base_run or {"status": base_status}, {}, base_probe)
            if aspen_probe_license_unavailable(base_probe):
                existing_non_license_blocker = any(
                    str(base_item.get("property_trust_status") or base_item.get("status") or "").startswith("blocked_")
                    and str(base_item.get("property_trust_status") or base_item.get("status") or "") != "blocked_aspen_property_license"
                    for base_item in base_scan_rows
                    if alpha_from_row(base_item) is None
                )
                base_probe_license_is_primary = not existing_non_license_blocker
                probe_message = base_probe.get("message") or base_probe.get("stderr_tail") or base_probe.get("stdout_tail") or ""
                for base_item in base_scan_rows:
                    if alpha_from_row(base_item) is not None:
                        continue
                    base_item["engine_probe_status"] = "license_unavailable"
                    base_item["engine_probe_message"] = short_text(probe_message, 1000)
                    if not existing_non_license_blocker:
                        base_item.update({
                            "status": "blocked_aspen_property_license",
                            "property_trust_status": "blocked_aspen_property_license",
                            "property_trust_reason": base_error,
                            "error_message": base_error,
                            "base_blocker": "blocked_aspen_property_license",
                        })
                agent.write_csv_file(output_csv, rows)
        base_summary = detect_vle_scan_summary(report, base_scan_rows)
        blocked_trust_rows = [
            row for row in base_scan_rows
            if str(row.get("property_trust_status") or "").startswith("blocked_")
        ]
        if blocked_trust_rows:
            first_block = blocked_trust_rows[0]
            base_summary["property_trust_status"] = first_block.get("property_trust_status") or ""
            base_summary["property_trust_reason"] = first_block.get("property_trust_reason") or first_block.get("error_message") or ""
            base_summary["property_trust_blocked_point_count"] = len(blocked_trust_rows)
        else:
            base_summary["property_trust_status"] = "trusted" if base_scan_rows else "missing"
        key_grid = key_composition_grid(report, base_request, base_summary)
        requests = expand_requests_for_key_grid(raw_requests, key_grid, base_summary)
        if max_rows:
            requests = requests[: int(max_rows)]
        base_alpha, _ = interpolated_base_alpha(base_scan_rows, task_overhead_fraction(report, base_request))
    candidate_hard_blockers = {}
    for index, row in enumerate(requests, start=1):
        code = agent.component_token(row.get("solvent_code"))
        if code in candidate_hard_blockers:
            continue
        solvent = solvent_by_code.get(code) or {}
        case_id = "{}_sf{}_{}".format(
            row.get("solvent_code") or "SOLVENT",
            str(row.get("sf_ratio")).replace(".", "p"),
            composition_token(row.get("x_overhead_liquid_spec")),
        )
        local_base_alpha, base_match = interpolated_base_alpha(base_scan_rows, row.get("x_overhead_liquid_spec"))
        item = dict(row, case_id=case_id, alpha_0=local_base_alpha or "")
        item.update(base_match)
        if not item.get("base_case_id"):
            item["base_case_id"] = base_match.get("base_case_id_low") or base_match.get("base_case_id_high") or ""
        if not local_base_alpha:
            base_blocker = "blocked_aspen_property_license" if base_probe_license_is_primary else ""
            item.update({
                "status": "blocked_no_base_alpha",
                "error_message": base_error or "Base binary Aspen flash did not return K values.",
                "engine_probe_status": base_probe.get("status") or "",
                "engine_probe_message": short_text(base_probe.get("message") or "", 1000),
                "base_blocker": base_blocker,
                "base_blocker_reason": base_error or "",
            })
            rows.append(item)
            continue
        try:
            case = build_case(report, row, solvent=solvent, base=False)
            inp = write_input_deck(work_dir / "{}.inp".format(agent.safe_file_token(case_id)), case)
            item["input_deck"] = str(inp.resolve())
            run = run_aspen_case(inp)
            item["run_seconds"] = run.get("run_seconds")
            item["aspen_run_status"] = run.get("status") or ""
            item["aspen_raw_error_message"] = short_text(run.get("error_message") or "", 1500)
            item["aspen_runtime_mode"] = run.get("aspen_runtime_mode") or ""
            item["aspen_runtime_dir"] = run.get("aspen_runtime_dir") or ""
            item["aspen_runtime_input_deck"] = run.get("aspen_runtime_input_deck") or ""
            item["aspen_runtime_copied_outputs"] = run.get("aspen_runtime_copied_outputs") or ""
            item["aspen_history_path"] = run.get("aspen_history_path") or ""
            item["aspen_history_tail"] = run.get("aspen_history_tail") or ""
            trust_status, trust_reason = aspen_run_trust_issue(run)
            item["property_trust_status"] = trust_status
            item["property_trust_reason"] = trust_reason
            app = run.get("app")
            if app is None:
                item.update({
                    "status": trust_status if trust_status.startswith("blocked_") else "failed",
                    "error_message": trust_reason or run.get("error_message") or "",
                })
                rows.append(item)
                continue
            try:
                values = read_flash_k_values(app, row)
                alpha_s = values.get("alpha")
                actual_x_total = values.get("x_overhead")
                actual_x = solvent_free_overhead_fraction(values, row.get("x_overhead_liquid_spec"))
                matched_base_alpha, actual_base_match = interpolated_base_alpha(base_scan_rows, actual_x)
                if matched_base_alpha:
                    local_base_alpha = matched_base_alpha
                    item["alpha_0"] = local_base_alpha
                    item.update(actual_base_match)
                item["alpha_match_x_overhead"] = actual_x
                item["alpha_match_x_overhead_solvent_free"] = actual_x
                item["alpha_match_x_overhead_total"] = actual_x_total
                item["alpha_match_basis"] = "solvent_free_feed_components"
                i_alpha = alpha_s / local_base_alpha if alpha_s is not None and local_base_alpha else None
                item.update(values)
                item["alpha_s"] = alpha_s
                item["alpha_improvement"] = i_alpha
                if i_alpha is not None:
                    item["status"] = "imported"
                else:
                    no_k_status, no_k_error = case_no_k_status_and_error(run, values)
                    item["status"] = no_k_status
                    item["error_message"] = no_k_error
                if trust_status != "trusted":
                    item["status"] = trust_status
                    item["error_message"] = trust_reason
                item["pass_alpha_gate"] = bool(
                    trust_status == "trusted"
                    and i_alpha is not None
                    and i_alpha >= agent.RELATIVE_VOLATILITY_I_ALPHA_PASS_THRESHOLD
                )
            finally:
                close_app(app, preserve_pids=run.get("preserve_aspen_pids"))
        except Exception as exc:
            item.update({"status": "failed", "error_message": "{}: {}".format(exc.__class__.__name__, exc)})
        rows.append(item)
        agent.write_csv_file(output_csv, rows)
        item_status = str(item.get("status") or item.get("property_trust_status") or "")
        if item_status.startswith("blocked_property") or item_status.startswith("blocked_aspen_component_identity"):
            candidate_hard_blockers[code] = item_status
    agent.write_csv_file(output_csv, rows)
    paths = dict(report.get("research_task_package_paths") or {})
    paths["relative_volatility_kvalue_results_csv"] = str(output_csv.resolve())
    paths["relative_volatility_flash_work_dir"] = str(work_dir.resolve())
    if base_summary:
        summary_path = package_dir / "relative_volatility_base_vle_scan_summary.json"
        agent.write_json_file(summary_path, base_summary)
        paths["relative_volatility_base_vle_scan_summary_json"] = str(summary_path.resolve())
    report["research_task_package_paths"] = paths
    if base_summary:
        report["relative_volatility_base_vle_scan_summary"] = base_summary
    if property_method:
        retry = dict(report.get("property_strategy_retry") or {})
        retry.update({
            "status": "completed_by_aspen_script",
            "property_method": property_method,
            "candidate_code": candidate_code,
            "output_csv": str(output_csv.resolve()),
            "work_dir": str(work_dir.resolve()),
            "updated_at": agent.iso_from_timestamp(time.time()) if hasattr(agent, "iso_from_timestamp") else "",
        })
        report["property_strategy_retry"] = retry
    report = agent.enrich_trial_report_payload(report)
    agent.write_json_file(report_path, report)
    status_rows = rows[len(existing_rows):] if existing_rows else rows
    status_rows = status_rows or rows
    property_blocked = any(str(row.get("property_trust_status") or "").startswith("blocked_property") for row in status_rows)
    identity_blocked = any(str(row.get("property_trust_status") or row.get("status") or "").startswith("blocked_aspen_component_identity") for row in status_rows)
    trust_blocked_rows = [
        row for row in status_rows
        if str(row.get("property_trust_status") or "").startswith("blocked_")
    ]
    non_license_trust_blocked = any(
        str(row.get("property_trust_status") or row.get("status") or "").startswith("blocked_")
        and str(row.get("property_trust_status") or row.get("status") or "") != "blocked_aspen_property_license"
        for row in status_rows
    )
    direct_license_blocked = any(
        str(row.get("status") or row.get("property_trust_status") or row.get("base_blocker") or "").strip()
        == "blocked_aspen_property_license"
        for row in status_rows
    )
    probe_only_license_blocked = aspen_probe_license_unavailable(base_probe) and not non_license_trust_blocked
    license_blocked = direct_license_blocked or probe_only_license_blocked
    final_status = "complete" if any(row.get("status") == "imported" for row in status_rows) else "failed"
    if identity_blocked:
        final_status = "blocked_aspen_component_identity"
    elif license_blocked:
        final_status = "blocked_aspen_property_license"
    elif property_blocked:
        final_status = "blocked_property_parameters_untrusted"
    elif trust_blocked_rows:
        final_status = trust_blocked_rows[0].get("property_trust_status") or "blocked_aspen_run_failed"
    elif not base_alpha:
        final_status = "blocked_no_base_alpha"
    return {
        "status": final_status,
        "rows": len(rows),
        "base_status": base_status,
        "base_alpha": base_alpha,
        "base_error": base_error,
        "engine_probe_status": base_probe.get("status") or "",
        "output_csv": str(output_csv.resolve()),
        "work_dir": str(work_dir.resolve()),
    }


def main():
    parser = argparse.ArgumentParser(description="Run Aspen flash K-value relative-volatility prescreen.")
    parser.add_argument("--report", required=True)
    parser.add_argument("--output-csv", default="")
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--max-candidates", type=int, default=8)
    parser.add_argument("--max-rows", type=int, default=0)
    parser.add_argument("--skip-completed", action="store_true")
    parser.add_argument("--candidate-code", default="")
    parser.add_argument("--property-method", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    summary = run_prescreen(
        args.report,
        output_csv=args.output_csv,
        work_dir=args.work_dir,
        max_candidates=args.max_candidates,
        max_rows=args.max_rows,
        skip_completed=args.skip_completed,
        candidate_code=args.candidate_code,
        property_method=args.property_method,
        force=args.force,
    )
    print(summary)
    raise SystemExit(0 if summary.get("status") == "complete" else 3)


if __name__ == "__main__":
    main()
