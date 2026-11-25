# -*- coding: utf-8 -*-
"""
Preliminary TAC model for the closed-loop extractive-distillation optimizer.

The model is intentionally centralized here so that economic assumptions can be
replaced without touching Aspen Tree paths or optimization logic.
"""

import math
import os
from pathlib import Path

import pandas as pd


HOURS_PER_YEAR = 8000.0
PAYBACK_YEARS = 3.0

CAL_PER_SEC_TO_KW = 4.184e-3
KW_YEAR_TO_GJ_YEAR = HOURS_PER_YEAR * 3600.0 / 1.0e6

# Common distillation TAC correlations used in many Aspen optimization papers:
#   column shell cost = 17640 * D^1.066 * L^0.802
#   heat exchanger cost = 7296 * A^0.65
#   A = Q / (U * deltaT)
# Units: D, L, and A are in m, m, and m2; Q is kW; U is kW/(m2 K).
COLUMN_SHELL_COST_FACTOR = 17640.0
COLUMN_DIAMETER_EXPONENT = 1.066
COLUMN_HEIGHT_EXPONENT = 0.802
COLUMN_HEIGHT_FACTOR = 1.2

HEAT_EXCHANGER_COST_FACTOR = 7296.0
HEAT_EXCHANGER_AREA_EXPONENT = 0.65
REBOILER_U_KW_PER_M2_K = 0.568
CONDENSER_U_KW_PER_M2_K = 0.852
FEED_EXCHANGER_U_KW_PER_M2_K = 0.852
MIN_REBOILER_APPROACH_K = 12.0
MIN_CONDENSER_APPROACH_K = 10.0
DEFAULT_CONDENSER_DELTA_T_K = 15.0
DEFAULT_FEED_EXCHANGER_DELTA_T_K = 15.0

# Utility prices are deliberately exposed. Replace them with the values required
# by the thesis source before using TAC as a final reported result.
LP_STEAM_TEMP_K = 433.0
MP_STEAM_TEMP_K = 457.0
HP_STEAM_TEMP_K = 527.0
LP_STEAM_COST_USD_PER_GJ = 7.78
MP_STEAM_COST_USD_PER_GJ = 8.22
HP_STEAM_COST_USD_PER_GJ = 9.88
COOLING_WATER_COST_USD_PER_GJ = 0.354
LOW_TEMP_COOLING_COST_USD_PER_GJ = 4.00

# The optimizer uses this as a process CO2 indicator, not a hard regulatory
# inventory. 56.1 kg/GJ is the common natural-gas combustion factor used as a
# first-pass steam duty proxy; override with thesis-specific factors if needed.
STEAM_CO2_KG_PER_GJ = float(os.environ.get("ED_AGENT_STEAM_CO2_KG_PER_GJ", "56.1"))

COOLING_WATER_SUPPLY_TEMP_K = 305.0
COOLING_WATER_MAX_SERVICE_TEMP_C = 40.0


DEFAULT_SOLVENT_MW = {
    "ETHYL-01": 62.068,
    "NMP": 99.133,
    "DMSO": 78.133,
    "SULFOLAN": 120.170,
    "PC": 102.089,
    "EC": 88.062,
    "GBL": 86.089,
    "ADPN": 108.144,
}

DEFAULT_SOLVENT_PRICE_USD_PER_KG = {
    # Web-sourced engineering defaults for process-level TAC screening.
    # EC quotes in 2026 for China/Asia industrial supply cluster around
    # 0.5-1.0 USD/kg; use a rounded current-market engineering basis.
    "EC": 0.70,
}

DEFAULT_SOLVENT_PRICE_SOURCE = {
    "EC": {
        "basis": "fallback_online_price_basis_ec_2026_review_if_no_evidence_file",
        "price_range_usd_per_kg": [0.50, 1.20],
        "source_url": "https://www.imarcgroup.com/ethylene-carbonate-pricing-report; https://www.made-in-china.com/products-search/hot-china-products/Ethylene_Carbonate_Price.html; https://www.chemanalyst.com/Pricing-data/ethylene-carbonate-ec-1193",
        "source_note": "Fallback only. The agent should create solvent_price_evidence.json before Aspen economics; current EC basis is IMARC China Q1 2026 688 USD/MT, cross-checked with public supplier/market pages.",
    },
}


def finite_float(value):
    try:
        value = float(value)
    except Exception:
        return None
    if not math.isfinite(value):
        return None
    return value


def load_solvent_economics(path):
    data = {}
    path = Path(path)
    if not path.exists():
        return data
    df = pd.read_excel(str(path))
    if "aspen_component_id" not in df.columns:
        return data
    for _, row in df.iterrows():
        component_id = str(row.get("aspen_component_id", "")).strip()
        if not component_id:
            continue
        mw = finite_float(row.get("molecular_weight_kg_per_kmol"))
        if mw is None:
            mw = DEFAULT_SOLVENT_MW.get(component_id)
        price = finite_float(row.get("solvent_price_usd_per_kg"))
        data[component_id] = {
            "molecular_weight_kg_per_kmol": mw,
            "solvent_price_usd_per_kg": price,
        }
    return data


def solvent_economics_for(component_id, solvent_economics):
    component_id = str(component_id or "").strip()
    values = dict((solvent_economics or {}).get(component_id, {}))
    if values.get("molecular_weight_kg_per_kmol") is None:
        values["molecular_weight_kg_per_kmol"] = DEFAULT_SOLVENT_MW.get(component_id)
    if values.get("solvent_price_usd_per_kg") is None:
        values["solvent_price_usd_per_kg"] = DEFAULT_SOLVENT_PRICE_USD_PER_KG.get(component_id)
        if component_id in DEFAULT_SOLVENT_PRICE_SOURCE:
            values.update(DEFAULT_SOLVENT_PRICE_SOURCE.get(component_id) or {})
    if values.get("solvent_price_usd_per_kg") is None:
        values["solvent_price_usd_per_kg"] = 0.0
        values.setdefault("basis", "missing_price_assumed_zero_review_required")
    return values


def positive(value):
    value = finite_float(value)
    if value is None or value <= 0.0:
        return None
    return value


def duty_kw(value):
    value = finite_float(value)
    if value is None:
        return None
    return abs(value) * CAL_PER_SEC_TO_KW


def exchanger_area_m2(q_kw, u_kw_per_m2_k, delta_t_k):
    q_kw = positive(q_kw)
    delta_t_k = positive(delta_t_k)
    u_kw_per_m2_k = positive(u_kw_per_m2_k)
    if q_kw is None or delta_t_k is None or u_kw_per_m2_k is None:
        return None
    return q_kw / (u_kw_per_m2_k * delta_t_k)


def exchanger_cost(area_m2):
    area_m2 = positive(area_m2)
    if area_m2 is None:
        return 0.0
    return HEAT_EXCHANGER_COST_FACTOR * area_m2 ** HEAT_EXCHANGER_AREA_EXPONENT


def column_shell_cost(diameter_m, height_m):
    diameter_m = positive(diameter_m)
    height_m = positive(height_m)
    if diameter_m is None or height_m is None:
        return None
    effective_height = COLUMN_HEIGHT_FACTOR * height_m
    return (
        COLUMN_SHELL_COST_FACTOR
        * diameter_m ** COLUMN_DIAMETER_EXPONENT
        * effective_height ** COLUMN_HEIGHT_EXPONENT
    )


def select_steam_utility(reboiler_temp_c):
    temp_k = finite_float(reboiler_temp_c)
    if temp_k is None:
        return LP_STEAM_TEMP_K, LP_STEAM_COST_USD_PER_GJ, "LP_STEAM"
    temp_k += 273.15
    if LP_STEAM_TEMP_K - temp_k >= MIN_REBOILER_APPROACH_K:
        return LP_STEAM_TEMP_K, LP_STEAM_COST_USD_PER_GJ, "LP_STEAM"
    if MP_STEAM_TEMP_K - temp_k >= MIN_REBOILER_APPROACH_K:
        return MP_STEAM_TEMP_K, MP_STEAM_COST_USD_PER_GJ, "MP_STEAM"
    return HP_STEAM_TEMP_K, HP_STEAM_COST_USD_PER_GJ, "HP_STEAM"


def condenser_utility(top_temp_c):
    temp_c = finite_float(top_temp_c)
    if temp_c is None:
        return LOW_TEMP_COOLING_COST_USD_PER_GJ, DEFAULT_CONDENSER_DELTA_T_K, "LOW_TEMP_COOLING"
    if temp_c >= COOLING_WATER_MAX_SERVICE_TEMP_C:
        delta_t = max(temp_c + 273.15 - COOLING_WATER_SUPPLY_TEMP_K, MIN_CONDENSER_APPROACH_K)
        return COOLING_WATER_COST_USD_PER_GJ, delta_t, "COOLING_WATER"
    return LOW_TEMP_COOLING_COST_USD_PER_GJ, DEFAULT_CONDENSER_DELTA_T_K, "LOW_TEMP_COOLING"


def add_column_costs(record):
    b1_cost = column_shell_cost(record.get("B1_diameter_m"), record.get("B1_height_m"))
    b2_cost = column_shell_cost(record.get("B2_diameter_m"), record.get("B2_height_m"))
    if b1_cost is None or b2_cost is None:
        return None
    return {
        "B1_column_shell_cost": b1_cost,
        "B2_column_shell_cost": b2_cost,
        "column_capital_cost": b1_cost + b2_cost,
    }


def add_heat_exchanger_costs(record):
    costs = {}
    total_area = 0.0
    total_cost = 0.0

    for block, duty_field, temp_field in [
        ("B1", "Qreb_1", "B1_reboiler_outlet_temp"),
        ("B2", "Qreb_2", "B2_reboiler_outlet_temp"),
    ]:
        q_kw = duty_kw(record.get(duty_field))
        steam_temp_k, steam_cost, steam_name = select_steam_utility(record.get(temp_field))
        temp_c = finite_float(record.get(temp_field))
        if temp_c is None:
            delta_t = MIN_REBOILER_APPROACH_K
        else:
            delta_t = max(steam_temp_k - (temp_c + 273.15), MIN_REBOILER_APPROACH_K)
        area = exchanger_area_m2(q_kw, REBOILER_U_KW_PER_M2_K, delta_t)
        cost = exchanger_cost(area)
        total_area += area or 0.0
        total_cost += cost
        costs["{}_reboiler_area_m2".format(block)] = area
        costs["{}_reboiler_capital_cost".format(block)] = cost
        costs["{}_reboiler_utility".format(block)] = steam_name
        costs["{}_reboiler_utility_cost_usd_per_gj".format(block)] = steam_cost

    for block, duty_field, temp_field in [
        ("B1", "Qcond_1", "B1_top_temp"),
        ("B2", "Qcond_2", "B2_top_temp"),
    ]:
        q_kw = duty_kw(record.get(duty_field))
        utility_cost, delta_t, utility_name = condenser_utility(record.get(temp_field))
        area = exchanger_area_m2(q_kw, CONDENSER_U_KW_PER_M2_K, delta_t)
        cost = exchanger_cost(area)
        total_area += area or 0.0
        total_cost += cost
        costs["{}_condenser_area_m2".format(block)] = area
        costs["{}_condenser_capital_cost".format(block)] = cost
        costs["{}_condenser_utility".format(block)] = utility_name
        costs["{}_condenser_utility_cost_usd_per_gj".format(block)] = utility_cost

    feed_q_kw = duty_kw(record.get("feed_heater_duty"))
    if feed_q_kw is not None and feed_q_kw > 0.0:
        area = exchanger_area_m2(feed_q_kw, FEED_EXCHANGER_U_KW_PER_M2_K, DEFAULT_FEED_EXCHANGER_DELTA_T_K)
        cost = exchanger_cost(area)
        total_area += area or 0.0
        total_cost += cost
        costs["feed_exchanger_area_m2"] = area
        costs["feed_exchanger_capital_cost"] = cost

    costs["heat_exchanger_area_m2"] = total_area
    costs["heat_exchanger_capital_cost"] = total_cost
    return costs


def utility_costs(record):
    reb1_kw = duty_kw(record.get("Qreb_1")) or 0.0
    reb2_kw = duty_kw(record.get("Qreb_2")) or 0.0
    cond1_kw = duty_kw(record.get("Qcond_1")) or 0.0
    cond2_kw = duty_kw(record.get("Qcond_2")) or 0.0
    feed_kw = duty_kw(record.get("feed_heater_duty")) or 0.0

    _, b1_steam_cost, _ = select_steam_utility(record.get("B1_reboiler_outlet_temp"))
    _, b2_steam_cost, _ = select_steam_utility(record.get("B2_reboiler_outlet_temp"))
    b1_cond_cost, _, _ = condenser_utility(record.get("B1_top_temp"))
    b2_cond_cost, _, _ = condenser_utility(record.get("B2_top_temp"))

    reboiler_energy_cost = (
        reb1_kw * KW_YEAR_TO_GJ_YEAR * b1_steam_cost
        + reb2_kw * KW_YEAR_TO_GJ_YEAR * b2_steam_cost
    )
    condenser_energy_cost = (
        cond1_kw * KW_YEAR_TO_GJ_YEAR * b1_cond_cost
        + cond2_kw * KW_YEAR_TO_GJ_YEAR * b2_cond_cost
    )
    feed_energy_cost = feed_kw * KW_YEAR_TO_GJ_YEAR * LOW_TEMP_COOLING_COST_USD_PER_GJ
    reboiler_steam_duty_gj_per_year = (reb1_kw + reb2_kw) * KW_YEAR_TO_GJ_YEAR
    carbon_emission_kg_per_year = reboiler_steam_duty_gj_per_year * STEAM_CO2_KG_PER_GJ
    return {
        "reboiler_energy_cost": reboiler_energy_cost,
        "condenser_energy_cost": condenser_energy_cost,
        "feed_exchanger_energy_cost": feed_energy_cost,
        "energy_cost": reboiler_energy_cost + condenser_energy_cost + feed_energy_cost,
        "total_reboiler_duty_kW": reb1_kw + reb2_kw,
        "total_condenser_duty_kW": cond1_kw + cond2_kw,
        "reboiler_steam_duty_gj_per_year": reboiler_steam_duty_gj_per_year,
        "carbon_emission_kg_per_year": carbon_emission_kg_per_year,
        "carbon_emission_ton_per_year": carbon_emission_kg_per_year / 1000.0,
        "carbon_emission_basis": "reboiler_steam_duty_gj_per_year * {} kg_CO2_per_GJ".format(STEAM_CO2_KG_PER_GJ),
    }


def solvent_makeup_cost(record, solvent_economics):
    component_id = record.get("aspen_component_id")
    solvent = solvent_economics_for(component_id, solvent_economics)
    mw = finite_float(solvent.get("molecular_weight_kg_per_kmol"))
    price = finite_float(solvent.get("solvent_price_usd_per_kg")) or 0.0
    loss = finite_float(record.get("solvent_loss_total"))
    if mw is None or loss is None:
        return {
            "solvent_molecular_weight_kg_per_kmol": mw,
            "solvent_price_usd_per_kg": price,
            "solvent_price_basis": solvent.get("basis", ""),
            "solvent_price_source_url": solvent.get("source_url", ""),
            "solvent_price_source_note": solvent.get("source_note", ""),
            "solvent_price_range_usd_per_kg": solvent.get("price_range_usd_per_kg", ""),
            "solvent_makeup_kg_per_year": None,
            "solvent_makeup_cost": None,
        }
    makeup_kg_per_year = max(loss, 0.0) * mw * HOURS_PER_YEAR
    return {
        "solvent_molecular_weight_kg_per_kmol": mw,
        "solvent_price_usd_per_kg": price,
        "solvent_price_basis": solvent.get("basis", ""),
        "solvent_price_source_url": solvent.get("source_url", ""),
        "solvent_price_source_note": solvent.get("source_note", ""),
        "solvent_price_range_usd_per_kg": solvent.get("price_range_usd_per_kg", ""),
        "solvent_makeup_kg_per_year": makeup_kg_per_year,
        "solvent_makeup_cost": makeup_kg_per_year * price,
    }


def calculate_tac(record, solvent_economics=None):
    values = {}
    column_costs = add_column_costs(record)
    if column_costs is None:
        values["TAC"] = None
        values["tac_error"] = "Missing usable column diameter or height."
        return values

    hx_costs = add_heat_exchanger_costs(record)
    utility = utility_costs(record)
    solvent = solvent_makeup_cost(record, solvent_economics)

    total_capital = column_costs["column_capital_cost"] + hx_costs["heat_exchanger_capital_cost"]
    annualized_capital = total_capital / PAYBACK_YEARS
    solvent_cost = solvent.get("solvent_makeup_cost")
    if solvent_cost is None:
        solvent_cost = 0.0
    operating = utility["energy_cost"] + solvent_cost

    values.update(column_costs)
    values.update(hx_costs)
    values.update(utility)
    values.update(solvent)
    values["total_capital_cost"] = total_capital
    values["annualized_capital_cost"] = annualized_capital
    values["operating_cost"] = operating
    values["TAC"] = annualized_capital + operating
    values["tac_error"] = ""
    return values
