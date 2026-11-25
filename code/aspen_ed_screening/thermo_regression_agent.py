"""Model-independent thermodynamic regression orchestration.

This module deliberately does not read an Aspen template.  It turns a
structured literature/COSMO-RS case into a fresh Aspen DRS input deck,
records provenance, and audits the execution result.  Aspen execution itself
is injected so the same logic can be tested without a licensed installation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence


ADAPTER_MODELS = ("NRTL", "UNIQUAC", "WILSON", "PR", "PRWS")
# Only adapters whose generated deck has passed an Aspen import/regression
# smoke test may authorize the evidence gate.  Registration is not proof.
VERIFIED_ADAPTER_MODELS = frozenset()
FINAL_STATUSES = {
    "verified_exact_parameter",
    "regressed_and_validated",
    "screening_only_predictive",
    "blocked_unvalidated",
}


@dataclass(frozen=True)
class Component:
    cid: str
    aspen_name: str
    db_name: str
    formula: str = ""


@dataclass(frozen=True)
class Evidence:
    source_type: str
    source_id: str
    model_selected_by_literature: bool
    cosmo_protocol: Mapping[str, Any] | None = None
    source_hashes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RegressionSpec:
    model: str
    parameter_count: int
    initial_values: Sequence[float]
    lower_bounds: Sequence[float]
    upper_bounds: Sequence[float]
    temperature_form: str = "constant"

    def validate(self) -> None:
        if self.model.upper() not in ADAPTER_MODELS:
            raise ValueError(f"unsupported Aspen model: {self.model}")
        n = self.parameter_count
        if not (len(self.initial_values) == len(self.lower_bounds) == len(self.upper_bounds) == n):
            raise ValueError("parameter arrays do not match parameter_count")
        if any(lo > hi for lo, hi in zip(self.lower_bounds, self.upper_bounds)):
            raise ValueError("parameter lower bound exceeds upper bound")
        if n <= 0 or any(not math.isfinite(float(x)) for seq in
                        (self.initial_values, self.lower_bounds, self.upper_bounds) for x in seq):
            raise ValueError("parameters and bounds must be finite and nonempty")
        if any(not lo <= x <= hi for x, lo, hi in zip(
                self.initial_values, self.lower_bounds, self.upper_bounds)):
            raise ValueError("initial value outside bounds")


@dataclass
class ThermoCaseSpec:
    case_id: str
    components: Sequence[Component]
    selected_model: str
    data_type: str
    data_rows: Sequence[Mapping[str, Any]]
    training_indices: Sequence[int]
    holdout_indices: Sequence[int]
    regression: RegressionSpec
    evidence: Evidence
    output_dir: str

    def validate(self) -> None:
        if len(self.components) != 2:
            raise ValueError("binary regression requires exactly two components")
        if self.selected_model.upper() != self.regression.model.upper():
            raise ValueError("literature-selected model and regression model differ")
        if not self.evidence.model_selected_by_literature:
            raise ValueError("COSMO-RS may not select the property model")
        if not self.data_rows:
            raise ValueError("regression data cannot be empty")
        if self.data_type != 'TPXY':
            raise ValueError('Only TPXY VLE input implemented; LLE requires a separate adapter')
        if not self.training_indices or not self.holdout_indices:
            raise ValueError('Both training and holdout must be nonempty')
        if len(set(self.training_indices)) != len(self.training_indices) or len(set(self.holdout_indices)) != len(self.holdout_indices):
            raise ValueError('Duplicate row indices')
        for row in self.data_rows:
            vals = [float(row[k]) for k in ('T_K', 'P_BAR', 'x1', 'y1')]
            if not all(math.isfinite(x) for x in vals) or min(vals[:2]) <= 0 or not all(0 <= x <= 1 for x in vals[2:]):
                raise ValueError('Invalid TPXY observation')
        all_indices = set(self.training_indices) | set(self.holdout_indices)
        if all_indices != set(range(len(self.data_rows))):
            raise ValueError("training/holdout indices must partition all data rows")
        if set(self.training_indices) & set(self.holdout_indices):
            raise ValueError("training and holdout rows overlap")
        self.regression.validate()


def _fmt(value: Any) -> str:
    return f"{float(value):.10g}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class AspenModelAdapter:
    model = ""

    def build_deck(self, case: ThermoCaseSpec) -> str:
        raise NotImplementedError

    def parameter_block(self, case: ThermoCaseSpec) -> str:
        values = case.regression
        if self.model == "NRTL":
            c1, c2 = case.components
            specs = [
                (c1.cid, c2.cid, 1, values.initial_values[0], values.lower_bounds[0], values.upper_bounds[0]),
                (c2.cid, c1.cid, 1, values.initial_values[2], values.lower_bounds[2], values.upper_bounds[2]),
                (c1.cid, c2.cid, 2, values.initial_values[1], values.lower_bounds[1], values.upper_bounds[1]),
                (c2.cid, c1.cid, 2, values.initial_values[3], values.lower_bounds[3], values.upper_bounds[3]),
            ]
            return " / ".join(
                f'{"PARAMSPEC " if idx == 0 else ""}TYPE = "BI-PARAMETER" NEL = 8 PARAMNAME = NRTL '
                f'CIDI = {cidi} CIDJ = {cidj} ELEMNO = {elem} PINIT = {_fmt(initial)} '
                f'<0> <0> LOWER = {_fmt(lower)} <0> <0> UPPER = {_fmt(upper)} '
                f'<0> <0> SCALE = 1.0 <0> <0>'
                for idx, (cidi, cidj, elem, initial, lower, upper) in enumerate(specs)
            )
        rows = []
        for i, (initial, lower, upper) in enumerate(
            zip(values.initial_values, values.lower_bounds, values.upper_bounds), 1
        ):
            rows.append(
                f'PARAMSPEC TYPE = "BI-PARAMETER" NEL = {8 if self.model == "NRTL" else values.parameter_count} '
                f'PARAMNAME = {self.model} CIDI = {case.components[0].cid} '
                f'CIDJ = {case.components[1].cid} ELEMNO = {i} PINIT = {_fmt(initial)} '
                f'<0> <0> LOWER = {_fmt(lower)} <0> <0> UPPER = {_fmt(upper)} '
                f'<0> <0> SCALE = 1.0 <0> <0>'
            )
        return " / ".join(rows)

    def _header(self, case: ThermoCaseSpec, option_set: str) -> str:
        comps = case.components
        comp_block = " / ".join(
            f'CID = {c.cid} ANAME = {c.aspen_name} OUTNAME = {c.aspen_name} '
            f'DBNAME1 = "{c.db_name}" ANAME1 = "{c.formula or c.aspen_name}"'
            for c in comps
        )
        prefix = (
            'MM "37.0" FLAVOR "NO" VERSION "37.0" MACHINE "WIN-NT/VC"\n'
            '? SETUP MAIN ? \\ TITLE TITLE = "Generated thermodynamic regression" \\ \\ '
            '"RUN-CLASS" RUN-CLASS = DRS \\ \\ SIMULATE INTERACTIVE = NO \\ '
            '? SETUP GLOBAL ? \\ "IN-UNITS" INSET = SET1 \\ '
            '? SETUP "SIM-OPTIONS" ? ; "SET1_MOLE" ; \\ '
            '? SETUP "UNITS-SET" SET1 ? \\ DESCRIPTION DESCRIPTION = "SI units" \\ \\ '
            'MMLOCAL \\ UNITSET BASESET = SI ( 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 6 1 4 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 ) \\ '
            '? ODATABANKS ? \\ ODATABANKS AUTO-PARAM = YES \\ '
            '? DATABANKS ? \\ DATABANKS FILE-SYM-NAM = '
            '( "APV86 ASPENPCD" "APV86 AQUEOUS" "APV86 SOLIDS" "APV86 INORGANIC" ) \\ '
            f'? COMPONENTS MAIN ? \\ COMPONENTS {comp_block} \\ '
            '? COMPONENTS "ADA/PCS" "LE-PROP-DATA" ? ; "SET1_MOLE" ; '
            '? COMPONENTS "COMP-LIST" GLOBAL ? ? COMPONENTS "UNIFAC-GROUP" ? '
            '? SOLVE ? '
            f'? PROPERTIES MAIN ? \\ GPROPERTIES GBASEOPSET = "{option_set}" '
            f'GOPSETNAME = "{option_set}" PARCON = -1 \\ '
            f'? PROPERTIES "OPTION-SETS" "{option_set}" ? \\ PARAM BASE = "{option_set}" \\ '
        )
        structures = ''.join(f'? PROPERTIES "MOLEC-STRUCT" {c.cid} ? \\ ' for c in comps)
        parameter_name = "NRTL" if option_set == "NRTL-RK" else option_set
        parameters = (
            f'? PROPERTIES PARAMETERS BINARY "T-DEPENDENT" "{parameter_name}-1" ? ; "ENG_MOLE" ; \\ '
            f'PROP-LIST PARAMNAME = {parameter_name} SETNO = 1 UNITROW = 0 TUNITROW = 22 TUNITLABEL = F NEL = 12 \\ '
            f'BPVAL PARAMNAME2 = {parameter_name} CID1 = {comps[0].cid} CID2 = {comps[1].cid} UNITROW2 = 0 TUNITROW2 = 22 '
        )
        return prefix + structures + parameters + ' '.join(f'VAL{i} = "R-DR-1"' for i in range(1, 13)) + ' \\ ? PROPERTIES "ATTR-COMPS" ? '

    def _data_group(self, case: ThermoCaseSpec) -> str:
        c1, c2 = case.components
        using = ['USING USE = "STD-DEV"'] + ["USE = DATA"] * len(case.training_indices)
        lines = [
            'DATA 1 PROPLIST = "TEMPERATURE_K" PROPLABEL = K VALUE = 0.1 <0> <0>',
            '1 PROPLIST = "PRESSURE_atm" PROPLABEL = atm VALUE = "0.1%" <0> <0>',
            f'1 PROPLIST = "X_{c1.cid}" PROPLABEL = {c1.cid} VALUE = "0.1%" <0> <0>',
            f'1 PROPLIST = "Y_{c1.cid}" PROPLABEL = {c1.cid} VALUE = "1%" <0> <0>',
        ]
        for row_no, row_index in enumerate(case.training_indices, 2):
            row = case.data_rows[row_index]
            lines.extend([
                f'{row_no} PROPLIST = "TEMPERATURE_K" PROPLABEL = K VALUE = {_fmt(row["T_K"])} <0> <0>',
                f'{row_no} PROPLIST = "PRESSURE_atm" PROPLABEL = atm VALUE = {_fmt(float(row["P_BAR"]) / 1.01325)} <0> <0>',
                f'{row_no} PROPLIST = "X_{c1.cid}" PROPLABEL = {c1.cid} VALUE = {_fmt(row["x1"])} <0> <0>',
                f'{row_no} PROPLIST = "Y_{c1.cid}" PROPLABEL = {c1.cid} VALUE = {_fmt(row["y1"])} <0> <0>',
            ])
        return (
            '? PROPERTIES DATA MIXTURE D-1 ? ; "SET1_MOLE" ; \\ '
            f'"SYSTEM-DEF" DATATYPE1 = EQUILIBRIUM STATESPEC = {case.data_type} '
            f'CIDS = ( {c1.cid} {c2.cid} ) \\ \\ "PHASE-EQ1" PEQTYPE1 = VL '
            f'VLECIDS1 = ( {c1.cid} {c2.cid} ) \\ \\ '
            + " / ".join(using) + " \\ \\ " + " / ".join(lines)
        )


class NRTLAdapter(AspenModelAdapter):
    model = "NRTL"

    def build_deck(self, case: ThermoCaseSpec) -> str:
        # Use Aspen V11's modern DRS input language.  This is the syntax
        # produced by Aspen itself when an official regression archive is
        # exported to text; it is distinct from the older MM/? archive syntax.
        comps = case.components
        p = case.regression.initial_values
        c1, c2 = comps
        databanks = " / ".join(
            f"'APV110 {name}'" for name in ("ASPENPCD", "AQUEOUS", "SOLIDS", "INORGANIC")
        )
        lines = [
            "DRS",
            "",
            "TITLE &",
            "        'Generated thermodynamic data regression'",
            "",
            "IN-UNITS SI PRESSURE=bar TEMPERATURE=K",
            "",
            "DESCRIPTION \"",
            "    Binary TPXY regression generated from the retained COSMO-RS data",
            "    \"",
            "",
            f"DATABANKS {databanks}",
            f"PROP-SOURCES {databanks}",
            "",
            "COMPONENTS",
            f"    {c1.cid} {c1.aspen_name} /",
            f"    {c2.cid} {c2.aspen_name}",
            "",
            "PROPERTIES NRTL-RK",
            "",
            "PROP-DATA NRTL-1",
            "    IN-UNITS ENG SHORT-LENGTH=in",
            "    PROP-LIST NRTL",
            f"    BPVAL {c1.cid} {c2.cid} {_fmt(p[0])} {_fmt(p[1])} 0.3 0.0 0.0 0.0 100.0 1000.0",
            f"    BPVAL {c2.cid} {c1.cid} {_fmt(p[2])} {_fmt(p[3])} 0.3 0.0 0.0 0.0 100.0 1000.0",
            "",
            "PARAMETERS",
            f"    BIPARAMETER 1 NRTL {c1.cid} {c2.cid} 1 {_fmt(p[0])} -10000.0 10000.0 1.0",
            f"    BIPARAMETER 2 NRTL {c2.cid} {c1.cid} 1 {_fmt(p[2])} -10000.0 10000.0 1.0",
            f"    BIPARAMETER 3 NRTL {c1.cid} {c2.cid} 2 {_fmt(p[1])} -10000.0 10000.0 1.0",
            f"    BIPARAMETER 4 NRTL {c2.cid} {c1.cid} 2 {_fmt(p[3])} -10000.0 10000.0 1.0",
            "",
            "CASE DR-1",
            "    PROPERTIES NRTL-RK TRUE-COMPS=YES",
            "    DATA-GROUPS D-1",
            "    PARAMETERS BINARY=1 2 3 4",
            "",
            "DATA-GROUP D-1",
            "    IN-UNITS SI PRESSURE=bar TEMPERATURE=K",
            f"    SYSTEM-DEF TPXY {c1.cid} {c2.cid} PROP-LIST=TPXY",
        ]
        for row_no, row_index in enumerate(case.training_indices, 1):
            row = case.data_rows[row_index]
            lines.append(
                f"    DATA {row_no} {_fmt(row['T_K'])} {_fmt(row['P_BAR'])} "
                f"{_fmt(row['x1'])} {_fmt(row['y1'])} /"
            )
        lines += ["", "PROPERTY-REP NOPCES PROP-DATA DFMS", ""]
        return "\n".join(lines)


class GenericAdapter(AspenModelAdapter):
    def build_deck(self, case: ThermoCaseSpec) -> str:
        # The model is explicit and never silently changed.  Until the
        # Aspen grammar for a model is verified, the adapter emits a blocked
        # deck marker rather than pretending it is executable.
        raise NotImplementedError(f"Aspen adapter not yet verified for {self.model}")


class UNIQUACAdapter(GenericAdapter): model = "UNIQUAC"
class WilsonAdapter(GenericAdapter): model = "WILSON"
class PRAdapter(GenericAdapter): model = "PR"
class PRWSAdapter(GenericAdapter): model = "PRWS"


ADAPTERS = {
    "NRTL": NRTLAdapter,
    "UNIQUAC": UNIQUACAdapter,
    "WILSON": WilsonAdapter,
    "PR": PRAdapter,
    "PRWS": PRWSAdapter,
}


def split_indices(n: int, holdout_every: int = 3) -> tuple[list[int], list[int]]:
    if n < 2:
        raise ValueError("at least two rows are required for a holdout split")
    holdout = list(range(holdout_every - 1, n, holdout_every))
    if not holdout:
        holdout = [n - 1]
    training = [i for i in range(n) if i not in set(holdout)]
    return training, holdout


def generate_case(case: ThermoCaseSpec) -> dict[str, Any]:
    case.validate()
    adapter_cls = ADAPTERS.get(case.selected_model.upper())
    if adapter_cls is None:
        return {"status": "blocked_unvalidated", "reason": "unsupported_model", "model": case.selected_model}
    try:
        deck = adapter_cls().build_deck(case)
    except NotImplementedError as exc:
        return {"status": "blocked_unvalidated", "reason": str(exc), "model": case.selected_model}
    out_dir = Path(case.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Generate an Aspen Properties input deck.  A .bkp/.aprbkp archive also
    # contains UI and database metadata that cannot be safely fabricated; the
    # COM adapter imports this text deck with InitFromFile2.
    deck_path = out_dir / f"{case.case_id}_{case.selected_model.upper()}.inp"
    manifest_path = out_dir / f"{case.case_id}_manifest.json"
    deck_path.write_text(deck, encoding="latin-1", newline="\n")
    manifest = {
        "case": asdict(case),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "deck": str(deck_path),
        "deck_sha256": _sha256(deck_path),
        "status": "generated_unexecuted",
        "parameter_status": "blocked_unvalidated",
        "template_used": False,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "generated_unexecuted", "deck": str(deck_path), "manifest": str(manifest_path), "template_used": False}


def audit_execution(events: Iterable[str], parameter_readback: Mapping[str, Any] | None = None) -> dict[str, Any]:
    text = "\n".join(str(e) for e in events).lower()
    severe = "severe error" in text or "fatal error" in text
    import_failure = any(token in text for token in (
        "unable to load simulation engine", "cannot open file",
        "open input file error", "cannot process input specification",
        "license is not available", "licensing error",
    ))
    completed = "data regression completed" in text or "drs converged" in text
    table_only = "table generation completed" in text and not completed
    readback_ok = bool(parameter_readback) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        for v in parameter_readback.values())
    if severe or import_failure or table_only or not completed or not readback_ok:
        reason = ("severe_error" if severe else "import_or_engine_failure" if import_failure
                  else "table_generation_only" if table_only else "regression_or_readback_missing")
        return {"status": "blocked_unvalidated", "regression_completed": completed, "parameter_readback": readback_ok, "reason": reason}
    return {"status": "regressed_pending_validation", "regression_completed": True, "parameter_readback": True}


def finalize_status(audit: Mapping[str, Any], *, holdout_rmse: float | None,
                    experimental_validation: bool,
                    validation: Mapping[str, Any] | None = None) -> str:
    """Fail closed: an error value or caller's experiment flag is not evidence.

    The validator must supply a declared error basis/threshold, independent
    rows, readback comparison and a traceable experimental benchmark.
    """
    v = validation or {}
    if (audit.get("status") != "regressed_pending_validation"
            or audit.get("regression_completed") is not True
            or audit.get("parameter_readback") is not True):
        return "blocked_unvalidated"
    def finite_nonnegative(x):
        return isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0
    limit = v.get("max_holdout_rmse")
    if not (finite_nonnegative(holdout_rmse) and finite_nonnegative(limit)
            and holdout_rmse <= limit and v.get("metric")
            and v.get("threshold_basis") and v.get("holdout_disjoint") is True
            and v.get("all_holdout_evaluated") is True
            and v.get("parameter_readback_matches") is True
            and v.get("model_matches_literature") is True):
        return "blocked_unvalidated"
    if not v.get("experimental_benchmark_source"):
        return "blocked_unvalidated"
    if experimental_validation and v.get("independent_target_experiment") is True:
        return "regressed_and_validated"
    if v.get("predictive_screening_gate_passed") is True:
        return "screening_only_predictive"
    return "blocked_unvalidated"


def write_result_package(case: ThermoCaseSpec, generation: Mapping[str, Any],
                         execution: Mapping[str, Any], *,
                         parameters: Mapping[str, Any] | None = None,
                         holdout_rmse: float | None = None,
                         experimental_validation: bool = False,
                         validation: Mapping[str, Any] | None = None) -> Path:
    """Persist the auditable result consumed by the screening gate.

    A parameter package is written even for blocked runs, but its status stays
    ``blocked_unvalidated`` and downstream flows must refuse it.
    """
    out_dir = Path(case.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    status = finalize_status(execution, holdout_rmse=holdout_rmse,
                             experimental_validation=experimental_validation,
                             validation=validation)
    if not parameters or generation.get("status") != "generated_unexecuted":
        status = "blocked_unvalidated"
    payload = {
        "case_id": case.case_id,
        "model": case.selected_model.upper(),
        "status": status,
        "template_used": False,
        "generation": dict(generation),
        "execution": dict(execution),
        "parameters": dict(parameters or {}),
        "validation": {
            **dict(validation or {}),
            "holdout_points": len(case.holdout_indices),
            "holdout_rmse": holdout_rmse,
            "experimental_validation": experimental_validation,
        },
        "evidence": asdict(case.evidence),
        "cosmors_is_model_selector": False,
    }
    path = out_dir / f"{case.case_id}_{case.selected_model.upper()}_parameter_package.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
