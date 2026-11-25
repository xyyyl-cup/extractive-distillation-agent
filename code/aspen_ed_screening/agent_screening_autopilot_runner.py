# -*- coding: utf-8 -*-
"""
One-button screening autopilot for the extractive-distillation agent.

The dashboard launches this file as the agent subprocess. It follows the
dashboard screening_state instead of relying on the user to press every stage
button. Long calculations are still guarded and serial: Aspen alpha is started
one candidate at a time, and Gaussian/GROMACS only starts after the alpha gate.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import agent_full_flow_runner as full_runner
import full_agent_dashboard as agent


IMMEDIATE_ACTIONS = {
    "full_flow_self_check",
    "expand_candidate_library",
    "import_external_candidate_library",
    "preflight_external_candidate_identity",
    "activate_external_aspen_candidates",
}

LONG_SCREENING_ACTIONS = {
    "restart_relative_volatility_flash",
    "run_next_relative_volatility_candidate",
    "run_relative_volatility_flash",
    "retry_alpha_with_predictive_property_method",
}

MOLECULAR_ACTIONS = {
    "run_gaussian_queue",
    "run_sobtop_queue",
    "run_nci_vmd_queue",
    "preflight_gromacs_queue",
    "run_gromacs_queue",
    "run_aspen_queue",
    "run_aspen_optimization_once",
}

GAUSSIAN_BATCHES_PER_SUPERVISION = agent.positive_int_env(
    "ED_AGENT_GAUSSIAN_BATCHES_PER_SUPERVISION",
    "GAUSSIAN_BATCHES_PER_SUPERVISION",
    default=24,
)
RESOURCE_WAIT_MAX_SECONDS = agent.positive_int_env(
    "ED_AGENT_RESOURCE_WAIT_MAX_SECONDS",
    "ED_AGENT_ASPEN_RESOURCE_WAIT_MAX_SECONDS",
    default=900,
)


def read_report(report_path):
    report = agent.read_json(report_path)
    report = agent.enrich_trial_report_payload(report)
    agent.write_json_file(report_path, report)
    return report


def alpha_counts(state):
    return ((state or {}).get("progress") or {}).get("alpha") or {}


def state_signature(state):
    counts = alpha_counts(state)
    external = (state or {}).get("external_candidate_library") or {}
    budget = (state or {}).get("budget") or {}
    current = (state or {}).get("current_candidate") or {}
    return (
        state.get("stage") or "",
        state.get("next_action") or "",
        current.get("code") or current.get("aspen_component_id") or "",
        int(counts.get("completed") or 0),
        int(counts.get("pending") or 0),
        int(counts.get("pass") or 0),
        int(counts.get("fail") or 0),
        int(counts.get("blocked") or 0),
        int(budget.get("used") or 0),
        int(external.get("new_ready_for_aspen_alpha_count") or 0),
        int(external.get("needs_aspen_identity_mapping_count") or 0),
        int(external.get("needs_boiling_point_or_process_gate_review_count") or 0),
    )


def compact_state(state):
    counts = alpha_counts(state)
    budget = (state or {}).get("budget") or {}
    external = (state or {}).get("external_candidate_library") or {}
    current = (state or {}).get("current_candidate") or {}
    return {
        "stage": state.get("stage") or "",
        "stage_label": state.get("stage_label") or "",
        "next_action": state.get("next_action") or "",
        "next_action_label": state.get("next_action_label") or "",
        "current_candidate": current,
        "alpha": counts,
        "budget": {
            "used": budget.get("used"),
            "max_candidate_total": budget.get("max_candidate_total"),
            "remaining_total": budget.get("remaining_total"),
            "pool_mode": budget.get("pool_mode"),
        },
        "external": {
            "ready": external.get("ready_for_aspen_alpha_count"),
            "new_ready": external.get("new_ready_for_aspen_alpha_count"),
            "already_in_pool": external.get("ready_already_in_pool_count"),
            "needs_mapping": external.get("needs_aspen_identity_mapping_count"),
            "needs_review": external.get("needs_boiling_point_or_process_gate_review_count"),
        },
    }


def first_passed_candidate_code(state):
    for row in (state or {}).get("passed_candidates") or []:
        code = row.get("code") or row.get("aspen_component_id") or row.get("solvent_code")
        if code:
            return str(code).strip()
    current = (state or {}).get("current_candidate") or {}
    if agent.relative_volatility_gate_allows_execution(current):
        return str(current.get("code") or current.get("aspen_component_id") or current.get("solvent_code") or "").strip()
    return ""


def retryable_property_method_review(state):
    state = state or {}
    if str(state.get("next_action") or "").strip() != "retry_alpha_with_predictive_property_method":
        return False
    blocker = (
        state.get("blocker")
        or ((state.get("blockers") or {}).get("alpha_global_blocker") or {})
    )
    if not blocker:
        return False
    blocker_tokens = {
        str(blocker.get(key) or "").strip().lower()
        for key in ("status", "base_blocker")
    }
    if blocker_tokens & {
        "blocked_aspen_property_license",
        "blocked_aspen_property_engine_unavailable",
    }:
        return False
    if hasattr(agent, "row_has_aspen_property_license_blocker") and agent.row_has_aspen_property_license_blocker(blocker):
        return False
    if blocker.get("retryable") is False:
        return False
    return bool(str(blocker.get("next_method") or "").strip())


def write_autopilot_status(report, report_path, status_payload, **updates):
    status_payload.update(updates)
    full_runner.write_status(report, report_path, status_payload)


def transient_resource_contention(result):
    result = result or {}
    status = str(result.get("status") or "").strip().lower()
    message = str(result.get("message") or result.get("error") or "").strip().lower()
    if result.get("running") or result.get("returncode") == -1:
        return True
    if status != "blocked":
        return False
    if any(
        key in result
        for key in (
            "aspen_process_count",
            "aspen_instance_count",
            "aspen_cleanup_event",
            "compute_processes",
        )
    ):
        return True
    return bool(
        "already running" in message
        or "已有后台" in message
        or "aspen" in message and ("process" in message or "进程" in message)
    )


def pid_is_alive(pid):
    try:
        pid = int(pid or 0)
    except Exception:
        pid = 0
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FI", "PID eq {}".format(pid), "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=8,
            )
            text = (completed.stdout or b"").decode("utf-8", errors="ignore")
            return str(pid) in text
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def settle_nested_action_process(process, payload):
    payload = dict(payload or {})
    if not payload.get("running"):
        return payload
    pid = payload.get("pid") or getattr(process, "pid", None)
    returncode = None
    if process is not None:
        try:
            returncode = process.poll()
        except Exception:
            returncode = None
    inferred_missing_process = False
    if returncode is None and pid and not pid_is_alive(pid):
        returncode = 0
        inferred_missing_process = True
    if returncode is None:
        return payload

    stdout_tail = agent.tail_text_file(payload.get("stdout_log"))
    stderr_tail = agent.tail_text_file(payload.get("stderr_log"))
    payload.update({
        "running": False,
        "finished_at": agent.iso_from_timestamp(time.time()),
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    })
    if inferred_missing_process:
        payload["inferred_finished_from_missing_pid"] = True
        payload["message"] = (
            str(payload.get("message") or "")
            + " Nested action PID is no longer live; treating the finished child state as complete."
        ).strip()
    with agent.ORCH_EXEC_LOCK:
        state_pid = agent.ORCH_EXEC_STATE.get("pid")
        same_state = not state_pid or not pid or str(state_pid) == str(pid)
        if same_state:
            agent.ORCH_EXEC_STATE.update({
                "running": False,
                "finished_at": payload["finished_at"],
                "returncode": returncode,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "message": payload.get("message") or agent.ORCH_EXEC_STATE.get("message") or "",
            })
            if process is None or agent.ORCH_EXEC_PROCESS is process:
                agent.ORCH_EXEC_PROCESS = None
    return payload


def read_full_flow_status(report, report_path):
    paths = (report or {}).get("research_task_package_paths") or {}
    status_path = paths.get("full_flow_autopilot_status_json") or ""
    payload = {}
    if status_path:
        payload = agent.read_json_default(status_path, {})
    if not payload:
        payload = (report or {}).get("full_flow_autopilot_status") or {}
    if not payload:
        return {}
    normalized = full_runner.normalize_required_output_repair_status(payload, report)
    if normalized != payload:
        full_runner.write_status(report, report_path, normalized)
    return normalized


def gaussian_queue_for_solvent(report, solvent_code):
    paths = (report or {}).get("research_task_package_paths") or {}
    primary = agent.primary_validation_solvent_identity(report, solvent_code) if solvent_code else {}
    return full_runner.gaussian_queue_status(paths, primary_solvent=primary)


def should_continue_gaussian_batches(report, flow_status, solvent_code):
    status_text = str((flow_status or {}).get("status") or "").strip().lower()
    message = str((flow_status or {}).get("message") or "")
    if status_text not in {"blocked_before_nci_vmd", "blocked_gaussian_queue_pending"}:
        return False, {}
    if status_text == "blocked_before_nci_vmd" and "Gaussian dimer .fch files are not available yet" not in message:
        return False, {}
    queue = gaussian_queue_for_solvent(report, solvent_code)
    return int(queue.get("pending_count") or 0) > 0, queue


def flow_status_closed_loop_diag(flow_status):
    flow_status = flow_status or {}
    diag = flow_status.get("closed_loop_recycle_diagnosis") or {}
    if diag:
        return diag
    for key in (
        "process_import_after_repair_probe",
        "process_import_after_optimization",
        "process_import_before_optimization",
    ):
        payload = flow_status.get(key) or {}
        diag = payload.get("closed_loop_recycle_diagnosis") or {}
        if diag:
            return diag
    delegated = flow_status.get("delegated_full_flow_status") or {}
    return delegated.get("closed_loop_recycle_diagnosis") or {}


def should_continue_aspen_required_output_repair(flow_status):
    status_text = str((flow_status or {}).get("status") or "").strip().lower()
    action = str((flow_status or {}).get("current_action") or "").strip().lower()
    diag = flow_status_closed_loop_diag(flow_status)
    missing = full_runner.required_output_repair_missing_families(diag)
    if not missing:
        return False, {"closed_loop_recycle_diagnosis": diag}
    retryable_status = (
        status_text in {
            "blocked_required_outputs_missing",
            "blocked_required_outputs_missing_retryable",
            "blocked_aspen_recycle_repair_required",
        }
        or status_text.startswith("blocked")
        or action in {
            "continue_required_output_repair_probe",
            "repair_aspen_model_outputs_before_more_optimization",
            "repair_closed_loop_recycle_solver_before_more_optimization",
        }
    )
    if not retryable_status:
        return False, {"closed_loop_recycle_diagnosis": diag, "missing_repair_mode_families": missing}
    return True, {
        "closed_loop_recycle_diagnosis": diag,
        "missing_repair_mode_families": missing,
    }


def run_full_flow_supervised(report_path, status, solvent_code, max_batches):
    last_flow_status = {}
    last_aspen_repair_context = {}
    for batch_index in range(1, int(max_batches or 1) + 1):
        report = read_report(report_path)
        before_queue = gaussian_queue_for_solvent(report, solvent_code)
        write_autopilot_status(
            report,
            report_path,
            status,
            current_stage="molecular_full_flow",
            current_action="agent_full_flow_autopilot",
            stage_status="running",
            gaussian_batch_index=batch_index,
            gaussian_queue_before=before_queue,
            message=(
                "Full-flow autopilot is running under agent supervision; "
                "Gaussian is budgeted to one job in this batch."
            ),
            solvent_lock=solvent_code,
        )
        rc = full_runner.run_full_flow(str(report_path), max_gaussian_jobs=1, solvent_code=solvent_code)
        report = read_report(report_path)
        flow_status = read_full_flow_status(report, report_path)
        last_flow_status = flow_status
        continue_gaussian, after_queue = should_continue_gaussian_batches(report, flow_status, solvent_code)
        if continue_gaussian:
            write_autopilot_status(
                report,
                report_path,
                status,
                current_stage="gaussian",
                current_action="run_gaussian_queue",
                stage_status="continuing_gaussian_batch",
                delegated_full_flow_status=flow_status,
                gaussian_queue_after=after_queue,
                message=(
                    "One Gaussian job finished. NCI/VMD still needs dimer .fch files, "
                    "so the agent will continue with the next Gaussian job after a short cool-down."
                ),
                solvent_lock=solvent_code,
            )
            time.sleep(3)
            continue
        continue_aspen_repair, repair_context = should_continue_aspen_required_output_repair(flow_status)
        if continue_aspen_repair:
            last_aspen_repair_context = repair_context
            write_autopilot_status(
                report,
                report_path,
                status,
                current_stage="aspen_required_output_repair_probe",
                current_action="continue_required_output_repair_probe",
                stage_status="continuing_aspen_repair_probe",
                status="running",
                delegated_full_flow_status=flow_status,
                aspen_repair_probe_retry=repair_context,
                message=(
                    "Aspen required outputs are still missing, but repair coverage is incomplete. "
                    "The agent will continue the generic repair probe after a short cool-down."
                ),
                solvent_lock=solvent_code,
            )
            time.sleep(5)
            continue
        last_aspen_repair_context = {}
        write_autopilot_status(
            report,
            report_path,
            status,
            current_stage=flow_status.get("current_stage") or "molecular_full_flow",
            current_action=flow_status.get("current_action") or "agent_full_flow_autopilot",
            stage_status=flow_status.get("stage_status") or ("done" if rc == 0 else "blocked"),
            status=flow_status.get("status") or ("complete" if rc == 0 else "blocked"),
            delegated_full_flow_status=flow_status,
            message=flow_status.get("message") or status.get("message") or "",
            solvent_lock=solvent_code,
        )
        return rc
    report = read_report(report_path)
    if last_aspen_repair_context:
        write_autopilot_status(
            report,
            report_path,
            status,
            current_stage="aspen_required_output_repair_probe",
            current_action="continue_required_output_repair_probe",
            stage_status="blocked",
            status="blocked_aspen_repair_probe_budget",
            delegated_full_flow_status=last_flow_status,
            aspen_repair_probe_retry=last_aspen_repair_context,
            message=(
                "Aspen required-output repair still has untested control families, but this supervised run reached "
                "its batch budget. Relaunching the same full-flow agent will continue the generic repair probe."
            ),
            solvent_lock=solvent_code,
        )
        return 3
    queue = gaussian_queue_for_solvent(report, solvent_code)
    write_autopilot_status(
        report,
        report_path,
        status,
        current_stage="gaussian",
        current_action="run_gaussian_queue",
        stage_status="blocked",
        status="blocked_gaussian_batch_budget",
        gaussian_queue_after=queue,
        message="Gaussian still has pending jobs, but the agent reached its supervised batch budget.",
        solvent_lock=solvent_code,
    )
    return 3


def wait_for_nested_action(report_path, status, poll_seconds=10):
    while True:
        with agent.ORCH_EXEC_LOCK:
            process = agent.ORCH_EXEC_PROCESS
            payload = dict(agent.ORCH_EXEC_STATE)
        payload = settle_nested_action_process(process, payload)
        live_payload = agent.attach_orchestrator_live_progress(payload, report_path=report_path)
        report = agent.read_json(report_path)
        write_autopilot_status(
            report,
            report_path,
            status,
            stage_status="running" if live_payload.get("running") else "done",
            nested_execution=live_payload,
            message=live_payload.get("message") or status.get("message") or "",
        )
        if not live_payload.get("running"):
            return live_payload
        time.sleep(max(2, int(poll_seconds or 10)))


def run_action(report_path, action, status):
    report = read_report(report_path)
    payload = agent.run_orchestrator_action({
        "action": action,
        "report_path": str(Path(report_path).resolve()),
        "separation_task": report.get("separation_task") or {},
    })
    report = agent.read_json(report_path)
    write_autopilot_status(
        report,
        report_path,
        status,
        current_action=action,
        stage_status="started" if payload.get("running") else str(payload.get("status") or "done"),
        last_action_result=payload,
        message=payload.get("message") or payload.get("error") or "",
    )
    if payload.get("running"):
        payload = wait_for_nested_action(report_path, status)
    return payload


def stop_status_for_state(state):
    counts = alpha_counts(state)
    if int(counts.get("pass") or 0) > 0:
        return None
    stage = str((state or {}).get("stage") or "")
    action = str((state or {}).get("next_action") or "")
    if stage in {"strategy_review", "property_strategy_review"}:
        if retryable_property_method_review(state):
            return None
        return "blocked_strategy_review"
    if not action:
        return "blocked_no_next_action"
    return None


def run_screening_autopilot(report_path, max_steps=120, poll_seconds=10):
    report_path = Path(report_path).resolve()
    report = read_report(report_path)
    task = report.get("separation_task") or {}
    status = {
        "mode": "agent_screening_autopilot",
        "status": "running",
        "task_id": task.get("task_id") or "",
        "started_at": agent.iso_from_timestamp(time.time()),
        "max_steps": int(max_steps or 120),
        "poll_seconds": int(poll_seconds or 10),
        "steps": [],
        "message": "Screening autopilot started. The agent will advance the current stage without manual step clicks.",
    }
    write_autopilot_status(report, report_path, status)

    repeated = {}
    resource_wait_started_at = None
    resource_wait_count = 0
    step_index = 0
    try:
        while step_index < int(max_steps or 120):
            step_index += 1
            report = read_report(report_path)
            state = report.get("screening_state") or agent.build_screening_state(report)
            counts = alpha_counts(state)
            action = str(state.get("next_action") or "")
            stage = str(state.get("stage") or "")
            signature = state_signature(state)
            repeated[signature] = repeated.get(signature, 0) + 1
            step = {
                "index": step_index,
                "at": agent.iso_from_timestamp(time.time()),
                "stage": stage,
                "action": action,
                "state": compact_state(state),
            }
            status["steps"].append(step)
            status["steps"] = status["steps"][-24:]
            write_autopilot_status(
                report,
                report_path,
                status,
                current_stage=stage,
                current_action=action,
                stage_status="planning",
                screening_state=compact_state(state),
                message="Agent is evaluating screening state: {} / {}.".format(stage or "-", action or "-"),
            )

            alpha_pass = int(counts.get("pass") or 0)
            alpha_pending = int(counts.get("pending") or 0)
            alpha_ready_for_molecular = alpha_pass > 0 and alpha_pending == 0
            if alpha_ready_for_molecular and (action in MOLECULAR_ACTIONS or stage == "molecular_queue"):
                solvent_code = first_passed_candidate_code(state)
                write_autopilot_status(
                    report,
                    report_path,
                    status,
                    current_stage="molecular_full_flow",
                    stage_status="delegating",
                    message="All finite Aspen alpha candidates are complete. Delegating the top alpha candidate to full molecular/process autopilot with one Gaussian job per turn.",
                    solvent_lock=solvent_code,
                )
                return run_full_flow_supervised(
                    report_path,
                    status,
                    solvent_code,
                    max_batches=min(
                        GAUSSIAN_BATCHES_PER_SUPERVISION,
                        max(1, int(max_steps or 120) - step_index + 1),
                    ),
                )

            stop_status = stop_status_for_state(state)
            if stop_status:
                status["status"] = stop_status
                status["stage_status"] = "blocked"
                status["message"] = "Screening autopilot stopped at {}: no alpha-passed candidate is available for Gaussian/GROMACS.".format(stage or "strategy review")
                write_autopilot_status(report, report_path, status)
                return 3

            if repeated[signature] >= 3 and action not in LONG_SCREENING_ACTIONS:
                status["status"] = "blocked_no_progress"
                status["stage_status"] = "blocked"
                status["message"] = "Screening state did not change after repeated agent actions; stopping to avoid a loop."
                write_autopilot_status(report, report_path, status)
                return 3

            if action in IMMEDIATE_ACTIONS or action in LONG_SCREENING_ACTIONS:
                result = run_action(report_path, action, status)
                if result.get("status") == "blocked" or (result.get("returncode") not in (None, 0) and not result.get("running")):
                    report = read_report(report_path)
                    state = report.get("screening_state") or {}
                    stop_status = stop_status_for_state(state)
                    counts = alpha_counts(state)
                    if (
                        action in LONG_SCREENING_ACTIONS
                        and not stop_status
                        and int(counts.get("pending") or 0) > 0
                    ):
                        if transient_resource_contention(result):
                            now = time.time()
                            if resource_wait_started_at is None:
                                resource_wait_started_at = now
                            resource_wait_count += 1
                            wait_seconds = max(0.0, now - resource_wait_started_at)
                            step["resource_wait"] = {
                                "count": resource_wait_count,
                                "seconds": round(wait_seconds, 1),
                                "max_seconds": RESOURCE_WAIT_MAX_SECONDS,
                            }
                            if wait_seconds < RESOURCE_WAIT_MAX_SECONDS:
                                step_index = max(0, step_index - 1)
                                write_autopilot_status(
                                    report,
                                    report_path,
                                    status,
                                    status="running",
                                    stage_status="waiting_for_runtime_resource",
                                    screening_state=compact_state(state),
                                    last_action_result=result,
                                    resource_wait=step["resource_wait"],
                                    message=(
                                        result.get("message")
                                        or result.get("error")
                                        or "A guarded runtime resource is busy; waiting before retrying the same screening action."
                                    ),
                                )
                                time.sleep(max(2, int(poll_seconds or 10)))
                                continue
                        step["action_result"] = {
                            "status": result.get("status") or "nonzero_returncode",
                            "returncode": result.get("returncode"),
                            "message": result.get("message") or result.get("error") or "",
                        }
                        write_autopilot_status(
                            report,
                            report_path,
                            status,
                            stage_status="continuing_after_candidate_block",
                            screening_state=compact_state(state),
                            last_action_result=result,
                            message=(
                                result.get("message")
                                or result.get("error")
                                or "One alpha candidate was blocked; continuing with the next finite candidate."
                            ),
                        )
                        continue
                    status["status"] = stop_status or "blocked"
                    status["stage_status"] = "blocked"
                    status["message"] = result.get("message") or result.get("error") or "Agent action was blocked."
                    status["last_action_result"] = result
                    write_autopilot_status(report, report_path, status)
                    return int(result.get("returncode") or 3)
                resource_wait_started_at = None
                resource_wait_count = 0
                continue

            status["status"] = "blocked_unknown_action"
            status["stage_status"] = "blocked"
            status["message"] = "Screening autopilot does not know how to run next_action={!r}.".format(action)
            write_autopilot_status(report, report_path, status)
            return 3

        report = read_report(report_path)
        status["status"] = "blocked_step_budget"
        status["stage_status"] = "blocked"
        status["message"] = "Screening autopilot reached its step budget and stopped."
        write_autopilot_status(report, report_path, status)
        return 3
    except Exception as exc:
        report = agent.read_json(report_path)
        status["status"] = "failed"
        status["stage_status"] = "failed"
        status["message"] = "{}: {}".format(exc.__class__.__name__, exc)
        write_autopilot_status(report, report_path, status)
        return 2


def main():
    parser = argparse.ArgumentParser(description="Run one-button ED screening autopilot.")
    parser.add_argument("--report", required=True)
    parser.add_argument("--max-steps", type=int, default=120)
    parser.add_argument("--poll-seconds", type=int, default=10)
    args = parser.parse_args()
    raise SystemExit(run_screening_autopilot(args.report, max_steps=args.max_steps, poll_seconds=args.poll_seconds))


if __name__ == "__main__":
    main()
