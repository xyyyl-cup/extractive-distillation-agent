# -*- coding: utf-8 -*-
"""
Aspen Plus COM runner for batch extractive-distillation screening.

This module deliberately reads all Aspen Tree paths from variable_map.py.
"""

import math
import gc
import atexit
import csv
import re
import shutil
import subprocess
import time
import traceback
from collections import OrderedDict
from pathlib import Path

try:
    import variable_map as vm
except ImportError:  # pragma: no cover
    from . import variable_map as vm


class AspenAutomationError(Exception):
    """Base exception for controlled screening errors."""


class AspenConfigError(AspenAutomationError):
    """Raised when user configuration is incomplete."""


class AspenPathError(AspenAutomationError):
    """Raised when an Aspen Tree path cannot be found."""


def is_blank(value):
    if value is None:
        return True
    try:
        if value != value:
            return True
    except Exception:
        pass
    return str(value).strip() == ""


def case_text(case, field):
    value = case.get(field)
    if is_blank(value):
        return ""
    return str(value).strip()


def case_float(case, field):
    value = case.get(field)
    if is_blank(value):
        raise AspenConfigError("Missing case value for '{}'.".format(field))
    try:
        return float(value)
    except Exception:
        raise AspenConfigError("Case field '{}' must be numeric; got {!r}.".format(field, value))


def optional_case_float(case, field):
    value = case.get(field)
    if is_blank(value):
        return None
    try:
        return float(value)
    except Exception:
        raise AspenConfigError("Case field '{}' must be numeric; got {!r}.".format(field, value))


def short_error(exc):
    if isinstance(exc, AspenAutomationError):
        return str(exc)
    return "{}: {}".format(exc.__class__.__name__, str(exc))


def safe_float(value):
    if value is None:
        return None
    try:
        if value != value:
            return None
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return None


class AspenCaseRunner(object):
    def __init__(self, template_bkp=None, case_work_dir=None, visible=None):
        self.template_bkp = Path(template_bkp or vm.TEMPLATE_BKP).resolve()
        self.case_work_dir = Path(case_work_dir or vm.CASE_WORK_DIR).resolve()
        self.visible = vm.ASPEN_VISIBLE if visible is None else bool(visible)
        self._initial_aspen_pids = self._aspen_process_ids()
        self._owned_aspen_pids = set()
        self._session_app = None
        self._session_archive = None
        self._session_case_count = 0
        atexit.register(self.close_session)

    def run_case(self, case, all_solvent_ids):
        result = self._base_result(case)
        app = None
        try:
            if not self.template_bkp.exists():
                raise AspenConfigError("Aspen template not found: {}".format(self.template_bkp))

            case_archive = self._copy_template(case_text(case, "case_id"))
            app = self._open_aspen(case_archive)
            self._apply_case(app, case, all_solvent_ids)
            self._run_simulation(app)

            converged = self._is_converged(app)
            result["converged"] = bool(converged)
            result.update(self._read_results(app, case))

            if not converged:
                result["error_message"] = "Aspen run completed, but convergence status was not successful."

            result["pass_or_fail"] = self._evaluate_pass_fail(result)
            result["score"] = result["total_reboiler_duty"]

        except Exception as exc:
            result["converged"] = False
            result["pass_or_fail"] = "FAIL"
            result["error_message"] = short_error(exc)
            if not isinstance(exc, AspenAutomationError):
                result["error_message"] += "\n" + traceback.format_exc(limit=3)
        finally:
            self._close_aspen(app)

        return result

    @property
    def session_case_count(self):
        return self._session_case_count

    @property
    def has_open_session(self):
        return self._session_app is not None

    def start_session(self, session_id="persistent_session"):
        if self._session_app is not None:
            return self._session_app
        if not self.template_bkp.exists():
            raise AspenConfigError("Aspen template not found: {}".format(self.template_bkp))

        self._session_archive = self._copy_template(session_id)
        self._session_app = self._open_aspen(self._session_archive)
        self._session_case_count = 0
        return self._session_app

    def restart_session(self, session_id="persistent_session"):
        self.close_session()
        return self.start_session(session_id)

    def close_session(self):
        app = self._session_app
        self._session_app = None
        self._session_archive = None
        self._session_case_count = 0
        self._close_aspen(app)

    def run_case_in_session(self, case, all_solvent_ids, session_id="persistent_session"):
        result = self._base_result(case)
        try:
            app = self.start_session(session_id)
            self._apply_case(app, case, all_solvent_ids)
            self._run_simulation(app)

            converged = self._is_converged(app)
            result["converged"] = bool(converged)
            result.update(self._read_results(app, case))

            if not converged:
                result["error_message"] = "Aspen run completed, but convergence status was not successful."

            result["pass_or_fail"] = self._evaluate_pass_fail(result)
            result["score"] = result["total_reboiler_duty"]
            self._session_case_count += 1
        except Exception as exc:
            result["converged"] = False
            result["pass_or_fail"] = "FAIL"
            result["error_message"] = short_error(exc)
            if not isinstance(exc, AspenAutomationError):
                result["error_message"] += "\n" + traceback.format_exc(limit=3)
            self.close_session()

        return result

    def _base_result(self, case):
        result = OrderedDict((column, None) for column in vm.RESULT_COLUMNS)
        result["case_id"] = case_text(case, "case_id")
        result["solvent_name"] = case_text(case, "solvent_name")
        result["converged"] = False
        result["error_message"] = ""
        result["pass_or_fail"] = "FAIL"
        return result

    def _copy_template(self, case_id):
        safe_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", case_id or "case")
        self.case_work_dir.mkdir(parents=True, exist_ok=True)
        destination = self.case_work_dir / "{}.bkp".format(safe_id)
        shutil.copy2(str(self.template_bkp), str(destination))
        return destination.resolve()

    def _open_aspen(self, archive_path):
        try:
            import win32com.client as win32
        except ImportError:
            raise AspenConfigError("pywin32 is required. Install it with: pip install pywin32")
        try:
            import zero_build_aspen_agent as zero_stage
        except Exception:
            zero_stage = None

        last_error = None
        for attempt in range(4):
            before_pids = self._aspen_process_ids()
            app = None
            stage_dir = None
            staged_archive = Path(archive_path).resolve()
            try:
                if zero_stage is not None:
                    stage_dir = zero_stage.make_aspen_stage_dir(staged_archive)
                    staged_archive = zero_stage.stage_input_file_for_aspen(staged_archive, stage_dir)
                try:
                    app = win32.DispatchEx(vm.ASPEN_PROG_ID)
                except Exception as dispatch_exc:
                    if before_pids:
                        raise dispatch_exc
                    app = win32.Dispatch(vm.ASPEN_PROG_ID)
                self._try_set_attr(app, "Visible", self.visible)
                self._try_set_attr(app, "SuppressDialogs", 1 if vm.ASPEN_SUPPRESS_DIALOGS else 0)

                if hasattr(app, "InitFromArchive2"):
                    app.InitFromArchive2(str(staged_archive))
                elif hasattr(app, "InitFromArchive"):
                    app.InitFromArchive(str(staged_archive))
                else:
                    raise AspenConfigError("Aspen COM object has no InitFromArchive method.")

                after_pids = self._aspen_process_ids()
                self._owned_aspen_pids.update(after_pids - before_pids)
                if zero_stage is not None:
                    zero_stage.cleanup_aspen_stage_dir(stage_dir)
                return app
            except Exception as exc:
                last_error = exc
                try:
                    if app is not None:
                        app.Close(False)
                except Exception:
                    pass
                try:
                    if app is not None:
                        app.Quit()
                except Exception:
                    pass

                after_pids = self._aspen_process_ids()
                self._owned_aspen_pids.update(after_pids - before_pids)
                self._terminate_owned_aspen_processes()
                if zero_stage is not None:
                    zero_stage.cleanup_aspen_stage_dir(stage_dir)
                transient = (
                    zero_stage.aspen_transient_com_error(exc)
                    if zero_stage is not None and hasattr(zero_stage, "aspen_transient_com_error")
                    else True
                )
                if attempt < 3 and transient:
                    time.sleep(min(20, 3 * (attempt + 1)))
                    continue
                break

        raise last_error

    def _aspen_process_ids(self):
        try:
            output = subprocess.check_output(
                [
                    "tasklist",
                    "/FI",
                    "IMAGENAME eq AspenPlus.exe",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except Exception:
            return set()

        process_ids = set()
        for row in csv.reader(output.splitlines()):
            if len(row) < 2:
                continue
            try:
                process_ids.add(int(str(row[1]).strip()))
            except Exception:
                continue
        return process_ids

    def _try_set_attr(self, obj, attr, value):
        try:
            setattr(obj, attr, value)
        except Exception:
            pass

    def _apply_case(self, app, case, all_solvent_ids):
        component_id = case_text(case, "aspen_component_id")
        if not component_id:
            raise AspenConfigError("Missing aspen_component_id in case.")

        sf = case_float(case, "SF")
        feed_total_flow = self._feed_total_flow(app)
        solvent_target_flow = sf * feed_total_flow
        if getattr(vm, "CLOSED_LOOP_RECYCLE_MODE", False):
            self._set_closed_loop_recycle(
                app,
                component_id,
                all_solvent_ids,
                solvent_target_flow,
            )
        else:
            self._set_solvent_stream(
                app,
                component_id,
                all_solvent_ids,
                solvent_target_flow,
            )

        self._set_value(app, vm.RR1_PATH, case_float(case, "RR1"), "RR1")
        self._set_value(app, vm.D1_PATH, case_float(case, "D1"), "D1")
        self._set_value(app, vm.RR2_PATH, case_float(case, "RR2"), "RR2")
        self._set_value(app, vm.D2_PATH, case_float(case, "D2"), "D2")

        self._set_optional_case_value(app, vm.N1_PATH, case, "N1")
        self._set_optional_case_value(app, vm.NF1_PATH, case, "NF1")
        self._set_optional_case_value(app, vm.NS1_PATH, case, "NS1")
        self._set_optional_case_value(app, vm.N2_PATH, case, "N2")
        self._set_optional_case_value(app, vm.NF2_PATH, case, "NF2")
        self._set_hydraulic_sections(app, case)

    def _set_solvent_stream(self, app, component_id, all_solvent_ids, solvent_total_flow):
        known_solvents = [str(item).strip() for item in all_solvent_ids if not is_blank(item)]

        component_flow_paths = getattr(vm, "SOLVENT_COMPONENT_FLOW_PATHS", {})
        if component_flow_paths:
            missing_paths = [
                solvent_id for solvent_id in known_solvents if solvent_id not in component_flow_paths
            ]
            if missing_paths:
                raise AspenConfigError(
                    "SOLVENT_COMPONENT_FLOW_PATHS is missing entries for: {}".format(
                        ", ".join(missing_paths)
                    )
                )
            if component_id not in component_flow_paths:
                raise AspenConfigError(
                    "Current solvent '{}' is not configured in SOLVENT_COMPONENT_FLOW_PATHS.".format(
                        component_id
                    )
                )
            if vm.is_configured_path(vm.SOLVENT_TOTAL_FLOW_PATH):
                self._set_value(app, vm.SOLVENT_TOTAL_FLOW_PATH, solvent_total_flow, "solvent total flow")
            for solvent_id, path in component_flow_paths.items():
                value = solvent_total_flow if solvent_id == component_id else 0.0
                self._set_value(app, path, value, "solvent component flow {}".format(solvent_id))
            return

        missing_paths = [
            solvent_id
            for solvent_id in known_solvents
            if solvent_id not in vm.SOLVENT_COMPOSITION_PATHS
        ]
        if missing_paths:
            raise AspenConfigError(
                "SOLVENT_COMPOSITION_PATHS is missing entries for: {}".format(
                    ", ".join(missing_paths)
                )
            )
        if component_id not in vm.SOLVENT_COMPOSITION_PATHS:
            raise AspenConfigError(
                "Current solvent '{}' is not configured in SOLVENT_COMPOSITION_PATHS.".format(
                    component_id
                )
            )

        self._set_value(app, vm.SOLVENT_TOTAL_FLOW_PATH, solvent_total_flow, "solvent total flow")
        for solvent_id, path in vm.SOLVENT_COMPOSITION_PATHS.items():
            value = 1.0 if solvent_id == component_id else 0.0
            self._set_value(app, path, value, "solvent mole fraction {}".format(solvent_id))

    def _set_closed_loop_recycle(self, app, component_id, all_solvent_ids, solvent_target_flow):
        known_solvents = [str(item).strip() for item in all_solvent_ids if not is_blank(item)]
        if component_id not in known_solvents:
            known_solvents.append(component_id)

        makeup_paths = getattr(vm, "SOLVENT_COMPONENT_FLOW_PATHS", {})
        tear_paths = getattr(vm, "RECYCLE_TEAR_COMPONENT_FLOW_PATHS", {})

        self._validate_path_map(makeup_paths, known_solvents, "SOLVENT_COMPONENT_FLOW_PATHS")
        self._validate_path_map(tear_paths, known_solvents, "RECYCLE_TEAR_COMPONENT_FLOW_PATHS")

        self._set_value(
            app,
            vm.CALCULATOR_RECYCLE_COMPONENT_PATH,
            component_id,
            "Calculator recycle solvent component",
        )
        self._set_value(
            app,
            vm.CALCULATOR_MAKEUP_COMPONENT_PATH,
            component_id,
            "Calculator makeup solvent component",
        )
        self._set_value(
            app,
            vm.CALCULATOR_FORTRAN_EXEC_PATH,
            self._calculator_fortran(solvent_target_flow),
            "Calculator target solvent flow formula",
        )

        # C1 is makeup in the closed loop. Clear stale component specs so the
        # Calculator write variable owns the active makeup flow.
        for solvent_id in known_solvents:
            self._set_value(
                app,
                makeup_paths[solvent_id],
                0.0,
                "makeup stream stale component flow {}".format(solvent_id),
            )

        # This path is retained as an input guess for the C1 stream. The actual
        # makeup flow is still written by Calculator C-1.
        if vm.is_configured_path(vm.SOLVENT_TOTAL_FLOW_PATH):
            self._set_value(
                app,
                vm.SOLVENT_TOTAL_FLOW_PATH,
                solvent_target_flow,
                "makeup stream total-flow guess",
            )

        # C2 is the recycle tear and the actual solvent feed to B1. Resetting
        # the tear initial guess prevents the previous solvent from remaining
        # in the next case after the Calculator component is switched.
        for solvent_id in known_solvents:
            self._set_value(
                app,
                tear_paths[solvent_id],
                0.0,
                "recycle tear stale component flow {}".format(solvent_id),
            )
        if vm.is_configured_path(vm.RECYCLE_TEAR_TOTAL_FLOW_PATH):
            self._set_value(
                app,
                vm.RECYCLE_TEAR_TOTAL_FLOW_PATH,
                solvent_target_flow,
                "recycle tear total-flow guess",
            )
        self._set_value(
            app,
            tear_paths[component_id],
            solvent_target_flow,
            "recycle tear active component flow {}".format(component_id),
        )

    def _validate_path_map(self, path_map, solvent_ids, map_name):
        missing = [solvent_id for solvent_id in solvent_ids if solvent_id not in path_map]
        if missing:
            raise AspenConfigError(
                "{} is missing entries for: {}".format(map_name, ", ".join(missing))
            )
        unconfigured = [
            solvent_id for solvent_id in solvent_ids if not vm.is_configured_path(path_map[solvent_id])
        ]
        if unconfigured:
            raise AspenConfigError(
                "{} has unconfigured paths for: {}".format(map_name, ", ".join(unconfigured))
            )

    def _calculator_fortran(self, solvent_target_flow):
        template = getattr(
            vm,
            "CALCULATOR_FORTRAN_TEMPLATE",
            "      FLOW={target_flow:.10g}-MOLARCO",
        )
        return template.format(target_flow=float(solvent_target_flow))

    def _feed_total_flow(self, app):
        if vm.FEED_TOTAL_FLOW_FIXED is not None:
            try:
                return float(vm.FEED_TOTAL_FLOW_FIXED)
            except Exception:
                raise AspenConfigError("FEED_TOTAL_FLOW_FIXED must be numeric.")
        return float(self._get_value(app, vm.FEED_TOTAL_FLOW_PATH, "feed total flow"))

    def _set_optional_case_value(self, app, path, case, field):
        value = optional_case_float(case, field)
        if value is None or not vm.is_configured_path(path):
            return
        self._set_value(app, path, value, field)

    def _set_hydraulic_sections(self, app, case):
        if not getattr(vm, "ENABLE_COLUMN_HYDRAULICS", False):
            return

        for block, path in getattr(vm, "HYDRAULIC_SWITCH_PATHS", {}).items():
            if vm.is_configured_path(path):
                self._set_value(
                    app,
                    path,
                    getattr(vm, "HYDRAULIC_SWITCH_ON_VALUE", "YES"),
                    "{} hydraulic switch".format(block),
                )

        for block, path in getattr(vm, "HYDRAULIC_REPORT_PATHS", {}).items():
            if vm.is_configured_path(path):
                self._set_value(
                    app,
                    path,
                    getattr(vm, "HYDRAULIC_REPORT_ON_VALUE", "HYDRAULIC"),
                    "{} hydraulic report switch".format(block),
                )

        section_paths = getattr(vm, "HYDRAULIC_SECTION_STAGE_PATHS", {})
        if not section_paths:
            return

        n1 = self._case_or_tree_int(app, case, "N1", vm.N1_PATH)
        nf1 = self._case_or_tree_int(app, case, "NF1", vm.NF1_PATH)
        ns1 = self._case_or_tree_int(app, case, "NS1", vm.NS1_PATH)
        n2 = self._case_or_tree_int(app, case, "N2", vm.N2_PATH)

        if n1 is not None and nf1 is not None and ns1 is not None and "B1" in section_paths:
            upper_split = min(nf1, ns1)
            lower_split = max(nf1, ns1)
            b1_ranges = [
                (2, upper_split - 1),
                (upper_split, lower_split - 1),
                (lower_split, n1 - 1),
            ]
            self._write_hydraulic_stage_ranges(app, "B1", b1_ranges)

        if n2 is not None and "B2" in section_paths:
            self._write_hydraulic_stage_ranges(app, "B2", [(2, n2 - 1)])

    def _case_or_tree_int(self, app, case, field, path):
        value = optional_case_float(case, field)
        if value is None and vm.is_configured_path(path):
            value = safe_float(self._get_value(app, path, field))
        if value is None:
            return None
        return int(round(float(value)))

    def _write_hydraulic_stage_ranges(self, app, block, ranges):
        section_paths = getattr(vm, "HYDRAULIC_SECTION_STAGE_PATHS", {}).get(block, [])
        if len(ranges) > len(section_paths):
            raise AspenConfigError(
                "{} hydraulic section map has {} sections, but {} ranges were requested.".format(
                    block, len(section_paths), len(ranges)
                )
            )

        for section, (stage1, stage2) in zip(section_paths, ranges):
            if stage1 < 2 or stage2 < stage1:
                raise AspenConfigError(
                    "{} hydraulic section {} has invalid stage range {}-{}.".format(
                        block, section.get("section", ""), stage1, stage2
                    )
                )
            self._set_value(
                app,
                section.get("stage1"),
                int(stage1),
                "{} hydraulic {} stage1".format(block, section.get("section", "")),
            )
            self._set_value(
                app,
                section.get("stage2"),
                int(stage2),
                "{} hydraulic {} stage2".format(block, section.get("section", "")),
            )

    def _run_simulation(self, app):
        if not hasattr(app, "Engine"):
            raise AspenConfigError("Aspen COM object has no Engine attribute.")

        engine = app.Engine
        engine.Run2()
        start = time.time()

        while True:
            try:
                running = bool(engine.IsRunning)
            except Exception:
                break
            if not running:
                break
            if time.time() - start > vm.RUN_TIMEOUT_SECONDS:
                raise AspenAutomationError(
                    "Aspen run exceeded timeout of {} seconds.".format(vm.RUN_TIMEOUT_SECONDS)
                )
            time.sleep(vm.RUN_POLL_INTERVAL_SECONDS)

    def _is_converged(self, app):
        if vm.is_configured_path(vm.CONVERGENCE_STATUS_PATH):
            status = self._get_value(app, vm.CONVERGENCE_STATUS_PATH, "convergence status")
            return self._status_is_success(status)
        return True

    def _status_is_success(self, status):
        status_text = str(status).strip().upper()
        for success in vm.CONVERGENCE_SUCCESS_VALUES:
            if status == success:
                return True
            if status_text == str(success).strip().upper():
                return True
        return False

    def _read_results(self, app, case):
        component_id = case_text(case, "aspen_component_id")

        qreb_1 = self._get_value(app, vm.QREB1_PATH, "Qreb_1")
        qreb_2 = self._get_value(app, vm.QREB2_PATH, "Qreb_2")
        qcond_1 = self._get_value(app, vm.QCOND1_PATH, "Qcond_1")
        qcond_2 = self._get_value(app, vm.QCOND2_PATH, "Qcond_2")

        qreb_1_float = safe_float(qreb_1)
        qreb_2_float = safe_float(qreb_2)
        total_reboiler_duty = None
        if qreb_1_float is not None and qreb_2_float is not None:
            total_reboiler_duty = qreb_1_float + qreb_2_float

        result = {
            "x_C3F8_top1": self._get_value(app, vm.C3F8_TOP1_MOLEFRAC_PATH, "x_C3F8_top1"),
            "recovery_C3F8": self._read_direct_or_ratio(
                app,
                vm.C3F8_RECOVERY_PATH,
                vm.C3F8_TOP1_COMPONENT_FLOW_PATH,
                vm.C3F8_FEED_COMPONENT_FLOW_PATH,
                "recovery_C3F8",
            ),
            "x_C3F6_top2": self._get_value(app, vm.C3F6_TOP2_MOLEFRAC_PATH, "x_C3F6_top2"),
            "recovery_C3F6": self._read_direct_or_ratio(
                app,
                vm.C3F6_RECOVERY_PATH,
                vm.C3F6_TOP2_COMPONENT_FLOW_PATH,
                vm.C3F6_FEED_COMPONENT_FLOW_PATH,
                "recovery_C3F6",
            ),
            "solvent_loss_top1": self._read_solvent_loss(
                app, vm.SOLVENT_LOSS_TOP1_PATH, vm.SOLVENT_LOSS_TOP1_PATHS, component_id, "solvent_loss_top1"
            ),
            "solvent_loss_top2": self._read_solvent_loss(
                app, vm.SOLVENT_LOSS_TOP2_PATH, vm.SOLVENT_LOSS_TOP2_PATHS, component_id, "solvent_loss_top2"
            ),
            "Qreb_1": qreb_1,
            "Qreb_2": qreb_2,
            "Qcond_1": qcond_1,
            "Qcond_2": qcond_2,
            "total_reboiler_duty": total_reboiler_duty,
        }
        result.update(self._read_tac_inputs(app))
        return result

    def _read_tac_inputs(self, app):
        result = {}
        for block in ("B1", "B2"):
            result["{}_diameter_m".format(block)] = self._read_column_diameter(app, block)
            result["{}_height_m".format(block)] = self._read_column_height(app, block)
            result["{}_top_temp".format(block)] = self._get_optional_value(
                app,
                getattr(vm, "TAC_COLUMN_TOP_TEMP_PATHS", {}).get(block),
                "{} top temperature".format(block),
            )
            result["{}_bottom_temp".format(block)] = self._get_optional_value(
                app,
                getattr(vm, "TAC_COLUMN_BOTTOM_TEMP_PATHS", {}).get(block),
                "{} bottom temperature".format(block),
            )
            result["{}_reboiler_outlet_temp".format(block)] = self._get_optional_value(
                app,
                getattr(vm, "TAC_REBOILER_OUTLET_TEMP_PATHS", {}).get(block),
                "{} reboiler outlet temperature".format(block),
            )
        result["feed_heater_duty"] = self._get_optional_value(
            app,
            getattr(vm, "TAC_FEED_HEATER_DUTY_PATH", ""),
            "feed heater duty",
        )
        return result

    def _read_column_diameter(self, app, block):
        values = []
        for path in getattr(vm, "TAC_COLUMN_DIAMETER_SECTION_PATHS", {}).get(block, []):
            value = safe_float(self._get_optional_value(app, path, "{} diameter".format(block)))
            if value is not None and value > 0.0:
                values.append(value)
        if values:
            return max(values)

        path = getattr(vm, "TAC_COLUMN_DIAMETER_PATHS", {}).get(block)
        value = safe_float(self._get_optional_value(app, path, "{} diameter".format(block)))
        if value is not None and value > 0.0:
            return value
        return None

    def _read_column_height(self, app, block):
        sections = getattr(vm, "HYDRAULIC_SECTION_STAGE_PATHS", {}).get(block, [])
        spacing_paths = getattr(vm, "TAC_COLUMN_TRAY_SPACING_SECTION_PATHS", {}).get(block, [])
        if not sections or not spacing_paths:
            return None

        height = 0.0
        any_section = False
        for index, section in enumerate(sections):
            stage1 = safe_float(self._get_optional_value(app, section.get("stage1"), "{} stage1".format(block)))
            stage2 = safe_float(self._get_optional_value(app, section.get("stage2"), "{} stage2".format(block)))
            if stage1 is None or stage2 is None:
                continue
            spacing_path = spacing_paths[index] if index < len(spacing_paths) else None
            spacing = safe_float(
                self._get_optional_value(app, spacing_path, "{} tray spacing".format(block))
            )
            if spacing is None or spacing <= 0.0:
                continue
            tray_count = max(0, int(round(stage2)) - int(round(stage1)) + 1)
            height += tray_count * spacing
            any_section = True

        if not any_section:
            return None
        return height + float(getattr(vm, "TAC_COLUMN_HEIGHT_ALLOWANCE_M", 0.0) or 0.0)

    def _read_direct_or_ratio(self, app, direct_path, numerator_path, denominator_path, label):
        if vm.is_configured_path(direct_path):
            return self._get_value(app, direct_path, label)
        if vm.is_configured_path(numerator_path) and vm.is_configured_path(denominator_path):
            numerator = float(self._get_value(app, numerator_path, label + " numerator"))
            denominator = float(self._get_value(app, denominator_path, label + " denominator"))
            if abs(denominator) < 1.0e-30:
                return None
            return numerator / denominator
        return None

    def _read_solvent_loss(self, app, direct_path, path_map, component_id, label):
        if vm.is_configured_path(direct_path):
            return self._get_value(app, direct_path, label)
        path = path_map.get(component_id)
        if vm.is_configured_path(path):
            return self._get_value(app, path, label)
        return None

    def _evaluate_pass_fail(self, result):
        if not result.get("converged"):
            return "FAIL"

        minimum_checks = [
            ("x_C3F8_top1", vm.C3F8_PURITY_TARGET),
            ("x_C3F6_top2", vm.C3F6_PURITY_TARGET),
            ("recovery_C3F8", vm.C3F8_RECOVERY_TARGET),
            ("recovery_C3F6", vm.C3F6_RECOVERY_TARGET),
        ]
        for field, target in minimum_checks:
            if target is None:
                continue
            value = result.get(field)
            if value is None or is_blank(value):
                return "FAIL"
            if float(value) < float(target):
                return "FAIL"

        if vm.MAX_SOLVENT_LOSS is not None:
            for field in ("solvent_loss_top1", "solvent_loss_top2"):
                value = result.get(field)
                if value is None or is_blank(value):
                    return "FAIL"
                if float(value) > float(vm.MAX_SOLVENT_LOSS):
                    return "FAIL"

        return "PASS"

    def _get_node(self, app, path, label):
        if not vm.is_configured_path(path):
            raise AspenConfigError("Missing Aspen Tree path for '{}' in variable_map.py.".format(label))
        node = app.Tree.FindNode(str(path))
        if node is None:
            raise AspenPathError(
                "Aspen Tree path for '{}' was not found. Check variable_map.py: {}".format(
                    label, path
                )
            )
        return node

    def _set_value(self, app, path, value, label):
        node = self._get_node(app, path, label)
        node.Value = value

    def _get_value(self, app, path, label):
        node = self._get_node(app, path, label)
        return node.Value

    def _get_optional_value(self, app, path, label):
        if not vm.is_configured_path(path):
            return None
        try:
            node = app.Tree.FindNode(str(path))
        except Exception:
            return None
        if node is None:
            return None
        try:
            return node.Value
        except Exception:
            return None

    def _close_aspen(self, app):
        if app is None:
            return
        if vm.SAVE_MODIFIED_CASE_FILES:
            try:
                app.Save()
            except Exception:
                pass
        try:
            app.Close(False)
        except Exception:
            try:
                app.Close()
            except Exception:
                pass
        try:
            app.Quit()
        except Exception:
            pass
        self._terminate_owned_aspen_processes()
        try:
            del app
        except Exception:
            pass
        gc.collect()

    def _terminate_owned_aspen_processes(self):
        if not getattr(vm, "TERMINATE_RUNNER_ASPEN_PROCESSES", False):
            return
        running_pids = self._aspen_process_ids()
        candidate_pids = set(self._owned_aspen_pids & running_pids)
        candidate_pids.update(running_pids - self._initial_aspen_pids)
        for pid in sorted(candidate_pids):
            self._terminate_process_tree(pid)
            self._owned_aspen_pids.discard(pid)

    def _terminate_process_tree(self, pid):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
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
                    ).format(int(pid)),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass
