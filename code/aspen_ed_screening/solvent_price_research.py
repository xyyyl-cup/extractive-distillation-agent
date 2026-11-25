# -*- coding: utf-8 -*-
"""
Online solvent-price evidence for Aspen economic ranking.

This module is intentionally separate from tac_model.py.  TAC calculation should
be deterministic; online research belongs to the agent preparation step that
locks one price assumption for one system/solvent optimization campaign.
"""

import datetime as dt
import html
import json
import math
import re
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path

import tac_model


USD_PER_CNY = 1.0 / 7.20
DEFAULT_TIMEOUT_SECONDS = 15


SOLVENT_NAME_ALIASES = {
    "EC": "ethylene carbonate",
    "ETHYLENE CARBONATE": "ethylene carbonate",
    "PC": "propylene carbonate",
    "NMP": "N-methyl-2-pyrrolidone",
    "DMSO": "dimethyl sulfoxide",
    "SULFOLAN": "sulfolane",
    "SULF": "sulfolane",
    "GBL": "gamma-butyrolactone",
}


SOURCE_SEEDS = {
    "EC": [
        {
            "name": "IMARC ethylene carbonate price trend",
            "url": "https://www.imarcgroup.com/ethylene-carbonate-pricing-report",
            "source_type": "market_index",
            "preferred_region": "China",
        },
        {
            "name": "Made-in-China ethylene carbonate supplier quotes",
            "url": "https://www.made-in-china.com/products-search/hot-china-products/Ethylene_Carbonate_Price.html",
            "source_type": "supplier_quote",
        },
        {
            "name": "ChemAnalyst ethylene carbonate pricing data",
            "url": "https://www.chemanalyst.com/Pricing-data/ethylene-carbonate-ec-1193",
            "source_type": "market_index",
        },
    ],
}


REGION_TERMS = (
    "China",
    "USA",
    "United States",
    "Japan",
    "France",
    "India",
    "Europe",
    "Asia",
    "Shanghai",
)


def now_iso():
    return dt.datetime.now().replace(microsecond=0).isoformat()


def finite_float(value):
    try:
        value = float(value)
    except Exception:
        return None
    return value if math.isfinite(value) else None


def canonical_solvent_code(value):
    text = str(value or "").strip().upper()
    text = re.sub(r"[^A-Z0-9]+", "", text)
    return text


def canonical_solvent_name(code="", name=""):
    code_token = canonical_solvent_code(code)
    name_token = str(name or "").strip()
    if code_token in SOLVENT_NAME_ALIASES:
        return SOLVENT_NAME_ALIASES[code_token]
    if name_token:
        return name_token
    return code_token


def html_to_plain_text(text):
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_url_text(url, timeout=DEFAULT_TIMEOUT_SECONDS):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SESC-PSE solvent-price-research/1.0 (+local thesis agent)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(800000)
        charset = response.headers.get_content_charset() or "utf-8"
        return {
            "url": url,
            "status": getattr(response, "status", 200),
            "content_type": response.headers.get("content-type", ""),
            "elapsed_seconds": round(time.time() - started, 3),
            "text": raw.decode(charset, errors="replace"),
        }


def generated_search_sources(solvent_name):
    slug = re.sub(r"[^a-z0-9]+", "-", str(solvent_name or "").lower()).strip("-")
    mic_token = "_".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", solvent_name or "") if part)
    sources = []
    if slug:
        sources.append({
            "name": "IMARC {} price trend".format(solvent_name),
            "url": "https://www.imarcgroup.com/{}-pricing-report".format(slug),
            "source_type": "market_index",
        })
    if mic_token:
        sources.append({
            "name": "Made-in-China {} supplier quotes".format(solvent_name),
            "url": "https://www.made-in-china.com/products-search/hot-china-products/{}_Price.html".format(mic_token),
            "source_type": "supplier_quote",
        })
    query = "{} price USD MT China 2026".format(solvent_name)
    sources.append({
        "name": "DuckDuckGo search: {}".format(query),
        "url": "https://duckduckgo.com/html/?q={}".format(urllib.parse.quote_plus(query)),
        "source_type": "search_result",
    })
    return sources


def price_unit_to_usd_per_kg(currency, unit, low, high):
    currency = str(currency or "").upper().replace("US$", "USD").replace("$", "USD")
    unit = str(unit or "").lower()
    low = finite_float(str(low).replace(",", ""))
    high = finite_float(str(high).replace(",", "")) if high not in (None, "") else low
    if low is None:
        return None
    if high is None:
        high = low
    factor = 1.0
    if currency in {"CNY", "RMB"} or currency in {"¥", "￥"}:
        factor *= USD_PER_CNY
    elif currency != "USD":
        return None
    if unit in {"kg", "kilogram", "kilograms"}:
        divisor = 1.0
    elif unit in {"t", "ton", "tons", "mt", "metricton", "metrictons", "metric ton", "metric tons"}:
        divisor = 1000.0
    else:
        return None
    low_usd = low * factor / divisor
    high_usd = high * factor / divisor
    if high_usd < low_usd:
        low_usd, high_usd = high_usd, low_usd
    return {
        "usd_per_kg_min": low_usd,
        "usd_per_kg_max": high_usd,
        "usd_per_kg_mid": (low_usd + high_usd) / 2.0,
    }


def context_around(text, start, end, width=180):
    left = max(0, start - width)
    right = min(len(text), end + width)
    return text[left:right].strip()


def infer_price_region(text, start):
    pre = text[max(0, start - 180):start]
    matches = []
    for term in REGION_TERMS:
        for match in re.finditer(r"\b{}\b".format(re.escape(term)), pre, flags=re.I):
            matches.append((match.start(), term))
    if not matches:
        return ""
    return sorted(matches, key=lambda item: item[0])[-1][1]


def infer_price_period(context):
    text = str(context or "")
    quarter = re.search(r"\bQ([1-4])\s*(20\d{2})\b", text, flags=re.I)
    if not quarter:
        quarter_words = {
            "first": 1,
            "second": 2,
            "third": 3,
            "fourth": 4,
        }
        match = re.search(r"\b(first|second|third|fourth)\s+quarter\s+of\s+(20\d{2})\b", text, flags=re.I)
        if match:
            q = quarter_words.get(match.group(1).lower(), 0)
            year = int(match.group(2))
            return {"period": "Q{} {}".format(q, year), "period_rank": year * 10 + q}
        return {"period": "", "period_rank": 0}
    q = int(quarter.group(1))
    year = int(quarter.group(2))
    return {"period": "Q{} {}".format(q, year), "period_rank": year * 10 + q}


def extract_price_mentions(plain_text, source):
    mentions = []
    patterns = [
        re.compile(
            r"(?P<currency>US\$|\$|USD|CNY|RMB|¥|￥)\s*"
            r"(?P<low>\d[\d,]*(?:\.\d+)?)\s*"
            r"(?:[-~–]\s*(?P<high>\d[\d,]*(?:\.\d+)?))?\s*"
            r"(?:/|per|\s+)\s*(?P<unit>kg|kilogram|kilograms|MT|metric ton|metric tons|ton|tons|t)\b",
            re.I,
        ),
        re.compile(
            r"(?P<low>\d[\d,]*(?:\.\d+)?)\s*"
            r"(?:[-~–]\s*(?P<high>\d[\d,]*(?:\.\d+)?))?\s*"
            r"(?P<currency>USD|CNY|RMB)\s*/\s*"
            r"(?P<unit>kg|kilogram|kilograms|MT|metric ton|metric tons|ton|tons|t)\b",
            re.I,
        ),
        re.compile(
            r"(?P<currency>US\$|\$|USD|CNY|RMB|¥|￥)\s*"
            r"(?P<low>\d[\d,]*(?:\.\d+)?)\s*"
            r"(?:[-~–]\s*(?P<high>\d[\d,]*(?:\.\d+)?))?\s+"
            r"1\s*(?P<unit>kg|kilogram|kilograms|MT|metric ton|metric tons|ton|tons|t)\s*\(MOQ\)",
            re.I,
        ),
    ]
    for pattern in patterns:
        for match in pattern.finditer(plain_text or ""):
            values = price_unit_to_usd_per_kg(
                match.group("currency"),
                match.group("unit"),
                match.group("low"),
                match.group("high"),
            )
            if not values:
                continue
            mid = values["usd_per_kg_mid"]
            if mid <= 0 or mid > 50:
                continue
            context = context_around(plain_text, match.start(), match.end())
            region = infer_price_region(plain_text, match.start())
            period = infer_price_period(context)
            mentions.append({
                "source_name": source.get("name") or "",
                "source_url": source.get("url") or "",
                "source_type": source.get("source_type") or "",
                "raw": match.group(0).strip(),
                "currency": match.group("currency"),
                "unit": match.group("unit"),
                "usd_per_kg_min": round(values["usd_per_kg_min"], 6),
                "usd_per_kg_max": round(values["usd_per_kg_max"], 6),
                "usd_per_kg_mid": round(values["usd_per_kg_mid"], 6),
                "context": context[:500],
                "region_hint": region,
                "period": period.get("period", ""),
                "period_rank": period.get("period_rank", 0),
            })
    return mentions


def round_engineering_price(value):
    value = finite_float(value)
    if value is None:
        return None
    return round(round(value / 0.05) * 0.05, 2)


def choose_price(mentions):
    plausible = [
        row for row in mentions
        if finite_float(row.get("usd_per_kg_mid")) is not None
        and 0.05 <= float(row.get("usd_per_kg_mid")) <= 10.0
    ]
    if not plausible:
        return None
    china_market = [
        row for row in plausible
        if row.get("source_type") == "market_index" and str(row.get("region_hint") or "").lower() == "china"
    ]
    if china_market:
        latest_rank = max(int(row.get("period_rank") or 0) for row in china_market)
        if latest_rank:
            china_market = [row for row in china_market if int(row.get("period_rank") or 0) == latest_rank]
    market = [row for row in plausible if row.get("source_type") == "market_index"]
    supplier_bulk = [
        row for row in plausible
        if row.get("source_type") == "supplier_quote"
        and finite_float(row.get("usd_per_kg_mid")) is not None
        and 0.20 <= float(row.get("usd_per_kg_mid")) <= 2.50
        and str(row.get("unit") or "").lower() not in {"kg", "kilogram", "kilograms"}
    ]
    selected_pool = china_market or market or supplier_bulk or plausible
    selected_mid = statistics.median(float(row["usd_per_kg_mid"]) for row in selected_pool)
    selected = round_engineering_price(selected_mid)
    range_pool = china_market + supplier_bulk
    if not range_pool:
        range_pool = selected_pool
    lows = [float(row["usd_per_kg_min"]) for row in range_pool if finite_float(row.get("usd_per_kg_min")) is not None]
    highs = [float(row["usd_per_kg_max"]) for row in range_pool if finite_float(row.get("usd_per_kg_max")) is not None]
    if not lows or not highs:
        low = max(0.01, selected * 0.7)
        high = selected * 1.5
    else:
        low = min(lows)
        high = max(highs)
        low = min(low, selected * 0.85)
        high = max(high, selected * 1.25)
    low = round_engineering_price(max(0.01, low))
    high = round_engineering_price(max(low, high))
    confidence = "market_index_china" if china_market else ("market_index" if market else "supplier_quote")
    return {
        "selected_price_usd_per_kg": selected,
        "price_range_usd_per_kg": [low, high],
        "confidence": confidence,
        "selected_pool_count": len(selected_pool),
        "price_basis": "online_{}_{}".format(confidence, dt.date.today().isoformat()),
    }


def source_note(evidence):
    sources = evidence.get("sources") or []
    pieces = []
    for source in sources:
        if source.get("status") != "fetched":
            continue
        count = source.get("price_mention_count") or 0
        pieces.append("{}: {} price mentions".format(source.get("name") or source.get("url"), count))
    if not pieces:
        return "Online lookup attempted, but no parseable public price quote was found; review required."
    return "Online solvent price lookup. " + "; ".join(pieces[:4])


def research_solvent_price(solvent_code="", solvent_name="", aspen_component_id="", timeout=DEFAULT_TIMEOUT_SECONDS):
    code = canonical_solvent_code(aspen_component_id or solvent_code)
    name = canonical_solvent_name(code or solvent_code, solvent_name)
    sources = list(SOURCE_SEEDS.get(code) or [])
    if not sources:
        sources = generated_search_sources(name)
    fetched_sources = []
    mentions = []
    for source in sources[:5]:
        source_record = dict(source)
        try:
            fetched = fetch_url_text(source["url"], timeout=timeout)
            plain = html_to_plain_text(fetched.get("text") or "")
            source_record.update({
                "status": "fetched",
                "http_status": fetched.get("status"),
                "content_type": fetched.get("content_type"),
                "elapsed_seconds": fetched.get("elapsed_seconds"),
                "text_length": len(plain),
            })
            source_mentions = extract_price_mentions(plain, source)
            source_record["price_mention_count"] = len(source_mentions)
            mentions.extend(source_mentions)
        except Exception as exc:
            source_record.update({
                "status": "fetch_failed",
                "error": "{}: {}".format(exc.__class__.__name__, exc),
            })
        fetched_sources.append(source_record)
    selected = choose_price(mentions)
    fallback = tac_model.solvent_economics_for(code, {})
    status = "ok_online_price_resolved" if selected else "online_lookup_failed_using_default_or_review_required"
    price = selected.get("selected_price_usd_per_kg") if selected else finite_float(fallback.get("solvent_price_usd_per_kg"))
    price_range = selected.get("price_range_usd_per_kg") if selected else fallback.get("price_range_usd_per_kg", "")
    basis = selected.get("price_basis") if selected else fallback.get("basis", "online_lookup_failed_review_required")
    evidence = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "status": status,
        "solvent": {
            "code": solvent_code or code,
            "name": solvent_name or name,
            "aspen_component_id": aspen_component_id or code,
        },
        "selected_price_usd_per_kg": price,
        "price_range_usd_per_kg": price_range,
        "basis": basis,
        "currency_conversion": {
            "USD_PER_CNY": USD_PER_CNY,
            "note": "CNY quotes are converted with a fixed screening exchange factor; final thesis economics should cite the chosen exchange-rate basis.",
        },
        "confidence": selected.get("confidence") if selected else "fallback_default_review_required",
        "sources": fetched_sources,
        "mentions": sorted(mentions, key=lambda row: float(row.get("usd_per_kg_mid") or 0.0))[:80],
        "source_note": "",
        "recommended_for_tac": bool(price is not None and price > 0),
    }
    evidence["source_note"] = source_note(evidence)
    return evidence


def read_json_default(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def evidence_matches(evidence, solvent_identity):
    solvent = (evidence or {}).get("solvent") or {}
    expected = {
        canonical_solvent_code(solvent_identity.get("aspen_component_id")),
        canonical_solvent_code(solvent_identity.get("code")),
        canonical_solvent_code(solvent_identity.get("solvent_code")),
    }
    expected.discard("")
    observed = {
        canonical_solvent_code(solvent.get("aspen_component_id")),
        canonical_solvent_code(solvent.get("code")),
    }
    observed.discard("")
    return bool(expected and observed and expected.intersection(observed))


def evidence_is_fresh(evidence, max_age_days=30):
    stamp = str((evidence or {}).get("generated_at") or "").strip()
    if not stamp:
        return False
    try:
        generated = dt.datetime.fromisoformat(stamp)
    except Exception:
        return False
    return (dt.datetime.now() - generated) <= dt.timedelta(days=max_age_days)


def price_metadata_for_spec(evidence):
    price = finite_float((evidence or {}).get("selected_price_usd_per_kg"))
    return {
        "solvent_price_usd_per_kg": price,
        "solvent_price_basis": (evidence or {}).get("basis") or "",
        "solvent_price_source_url": "; ".join(
            source.get("url") or ""
            for source in ((evidence or {}).get("sources") or [])
            if source.get("status") == "fetched" and source.get("url")
        ),
        "solvent_price_range_usd_per_kg": (evidence or {}).get("price_range_usd_per_kg", ""),
        "solvent_price_source_note": (evidence or {}).get("source_note") or "",
        "solvent_price_evidence_status": (evidence or {}).get("status") or "",
        "solvent_price_evidence_generated_at": (evidence or {}).get("generated_at") or "",
    }


def apply_price_evidence_to_spec(spec_path, evidence, evidence_path=None):
    spec_path = Path(spec_path)
    spec = read_json_default(spec_path, {})
    if not isinstance(spec, dict):
        return False
    solvent = (evidence or {}).get("solvent") or {}
    tokens = {
        canonical_solvent_code(solvent.get("aspen_component_id")),
        canonical_solvent_code(solvent.get("code")),
        canonical_solvent_code(solvent.get("name")),
    }
    tokens.discard("")
    metadata = price_metadata_for_spec(evidence)
    if evidence_path:
        metadata["solvent_price_evidence_json"] = str(Path(evidence_path).resolve())
    changed = False
    for row in spec.get("components") or []:
        if not isinstance(row, dict):
            continue
        row_tokens = {
            canonical_solvent_code(row.get("id")),
            canonical_solvent_code(row.get("aspen_component_id")),
            canonical_solvent_code(row.get("name")),
            canonical_solvent_code(row.get("db_name")),
        }
        row_tokens.discard("")
        if tokens.intersection(row_tokens) or str(row.get("role") or "").lower() == "candidate_solvent":
            row.update(metadata)
            changed = True
    validation_case = spec.get("validation_case")
    if isinstance(validation_case, dict):
        validation_tokens = {
            canonical_solvent_code(validation_case.get("aspen_component_id")),
            canonical_solvent_code(validation_case.get("solvent_code")),
            canonical_solvent_code(validation_case.get("solvent_name")),
        }
        validation_tokens.discard("")
        if tokens.intersection(validation_tokens):
            validation_case.update(metadata)
            changed = True
    if changed:
        write_json(spec_path, spec)
    return changed


def ensure_solvent_price_evidence(spec_path, package_dir, solvent_identity, force=False, max_age_days=30):
    package_dir = Path(package_dir)
    evidence_path = package_dir / "solvent_price_evidence.json"
    evidence = read_json_default(evidence_path, {})
    if (
        not force
        and evidence
        and evidence_matches(evidence, solvent_identity or {})
        and evidence_is_fresh(evidence, max_age_days=max_age_days)
        and finite_float(evidence.get("selected_price_usd_per_kg")) is not None
    ):
        reused = dict(evidence)
        reused["reused_existing_evidence"] = True
        apply_price_evidence_to_spec(spec_path, reused, evidence_path)
        return reused
    evidence = research_solvent_price(
        solvent_code=solvent_identity.get("code") or solvent_identity.get("solvent_code") or "",
        solvent_name=solvent_identity.get("name") or solvent_identity.get("solvent_name") or "",
        aspen_component_id=solvent_identity.get("aspen_component_id") or solvent_identity.get("code") or "",
    )
    write_json(evidence_path, evidence)
    apply_price_evidence_to_spec(spec_path, evidence, evidence_path)
    return evidence
