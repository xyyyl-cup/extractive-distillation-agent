# -*- coding: utf-8 -*-
"""Independent proof-of-concept for literature-backed property-method audits.

The auditor deliberately separates four states:
1. metadata lead found;
2. full text obtained;
3. method use and experimental validation statements extracted;
4. enough target-system evidence exists to support the selected method.

It does not infer binary parameters from a title or abstract.  Numerical
parameter extraction/regression remains a later, separately validated step.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import re
import ssl
import urllib.parse
import urllib.request
from pathlib import Path


USER_AGENT = "SESC-PSE-thermo-auditor/0.1 (mailto:research-agent@openai.com)"
OPENAIRE_API = "https://api.openaire.eu/search/publications"
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"

COMPONENT_TERMS = {
    "ETHANOL": ["ethanol", "ethyl alcohol"],
    "H2O": ["water"],
    "WATER": ["water"],
    "METHANOL": ["methanol", "methyl alcohol"],
    "EC": ["ethylene carbonate", "1,3-dioxolan-2-one"],
    "PC": ["propylene carbonate"],
    "GC": ["glycerol carbonate"],
}

COMPONENT_NAMES_ZH = {
    "ETHANOL": "乙醇",
    "H2O": "水",
    "WATER": "水",
    "METHANOL": "甲醇",
    "EC": "碳酸乙烯酯（EC）",
    "PC": "碳酸丙烯酯（PC）",
    "GC": "甘油碳酸酯（GC）",
}

FAMILY_TERMS = {
    "EC": ["cyclic carbonate", "cyclic carbonates", "propylene carbonate", "glycerol carbonate"],
    "PC": ["cyclic carbonate", "cyclic carbonates", "ethylene carbonate", "glycerol carbonate"],
    "GC": ["cyclic carbonate", "cyclic carbonates", "ethylene carbonate", "propylene carbonate"],
}

METHOD_PATTERNS = {
    "NRTL": r"\bNRTL(?:-RK|-HOC)?\b|non[- ]random two[- ]liquid",
    "UNIQUAC": r"\bUNIQUAC\b",
    "WILSON": r"\bWilson\b",
    "UNIFAC": r"\b(?:modified\s+)?UNIFAC\b",
    "COSMO-SAC": r"\bCOSMO[- ]?SAC\b",
    "COSMO-RS": r"\bCOSMO[- ]?RS\b|\bCOSMOtherm\b",
    "PC-SAFT": r"\bPC[- ]?SAFT\b",
    "PR": r"\bPeng[- ]Robinson\b|\bPR(?:-BM)?\b",
    "PRWS": r"\bPRWS\b|Wong[- ]Sandler",
}

VALIDATION_WORDS = re.compile(
    r"validat(?:e|ed|ion)|good\s+model|represent(?:ed|s|ing)?\s+the\s+experimental|"
    r"agreement|agrees?\s+(?:well\s+)?with|compar(?:e|ed|ison)|reproduc(?:e|ed|es)|"
    r"describ(?:e|es|ed|ing)\s+(?:the\s+)?(?:phase\s+equilibria\s+)?(?:properly|accurately)",
    re.I,
)
EXPERIMENT_WORDS = re.compile(
    r"experimental\s+(?:data|validation|measurement|results?)|"
    r"(?:VLE|LLE|VLLE)\s+experiments?|measur(?:e|ed|ement)s?|headspace\s+gas\s+chromatography|ebulliometer|"
    r"(?:phase|vapor[- ]liquid|vapour[- ]liquid)\s+equilibr(?:ium|ia)",
    re.I,
)
METRIC_WORDS = re.compile(
    r"(?:RMSE|RMSD|AAD|ARD|average\s+(?:absolute|relative)|deviation|error)"
    r"[^.;:]{0,90}?\b\d+(?:\.\d+)?\s*%?",
    re.I,
)
PARAMETER_WORDS = re.compile(
    r"binary\s+interaction\s+parameters?|parameter(?:s|ization|isation)?|regress(?:ed|ion)|fit(?:ted|ting)?",
    re.I,
)


def _ssl_context():
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _http(url, accept="application/json", timeout=30, max_bytes=30_000_000):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": accept},
    )
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("remote document exceeds byte limit")
        return data, response.geturl(), response.headers.get("content-type") or ""


def _json_get(url):
    data, _final_url, _content_type = _http(url, accept="application/json")
    return json.loads(data.decode("utf-8", "replace"))


def normalize_doi(value):
    text = str(value or "").strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi:\s*", "", text, flags=re.I)
    return text.strip().lower()


def _clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _terms(component):
    token = str(component or "").strip().upper()
    return COMPONENT_TERMS.get(token, [str(component or "").strip().lower()])


def build_queries(component_a, component_b, entrainer="", selected_method=""):
    a = _terms(component_a)[0]
    b = _terms(component_b)[0]
    rows = [
        "{} {} vapor liquid equilibrium thermodynamic model experimental".format(a, b),
        "{} {} {} VLE experimental validation".format(a, b, selected_method or "property model"),
    ]
    if entrainer:
        solvent = _terms(entrainer)[0]
        rows.append("{} {} {} vapor liquid equilibrium extractive distillation".format(a, b, solvent))
        family = FAMILY_TERMS.get(str(entrainer).upper()) or []
        if family:
            rows.append("alcohol water {} extractive distillation COSMO experimental".format(family[0]))
    return list(dict.fromkeys(item.strip() for item in rows if item.strip()))


def _is_primary_article(record):
    doi = normalize_doi(record.get("doi"))
    title = str(record.get("title") or "")
    if not doi or doi.endswith((".s001", ".s002", ".s003")):
        return False
    if record.get("type") in {"component", "dataset", "posted-content"}:
        return False
    if re.search(r"supporting information|supplementary", title, re.I):
        return False
    return True


def search_crossref(query, rows=15):
    url = "{}?rows={}&query.bibliographic={}".format(
        CROSSREF_API,
        int(rows),
        urllib.parse.quote(query),
    )
    payload = _json_get(url)
    output = []
    for item in payload.get("message", {}).get("items") or []:
        title = (item.get("title") or [""])[0]
        doi = normalize_doi(item.get("DOI"))
        record = {
            "title": _clean_text(title),
            "doi": doi,
            "url": "https://doi.org/{}".format(doi) if doi else item.get("URL") or "",
            "year": ((item.get("published") or {}).get("date-parts") or [[""]])[0][0],
            "container": _clean_text((item.get("container-title") or [""])[0]),
            "type": item.get("type") or "",
            "provider": "Crossref",
            "provider_score": item.get("score") or 0,
            "query": query,
        }
        if _is_primary_article(record):
            output.append(record)
    return output


def search_openalex(query, rows=15):
    url = "{}?search={}&per-page={}&mailto={}".format(
        OPENALEX_API,
        urllib.parse.quote(query),
        int(rows),
        urllib.parse.quote("research-agent@openai.com"),
    )
    payload = _json_get(url)
    output = []
    for item in payload.get("results") or []:
        doi = normalize_doi(item.get("doi"))
        primary = item.get("primary_location") or {}
        source = primary.get("source") or {}
        best_oa = item.get("best_oa_location") or {}
        record = {
            "title": _clean_text(item.get("title")),
            "doi": doi,
            "url": "https://doi.org/{}".format(doi) if doi else primary.get("landing_page_url") or "",
            "year": item.get("publication_year") or "",
            "container": _clean_text(source.get("display_name")),
            "type": "journal-article" if item.get("type") == "article" else item.get("type") or "",
            "provider": "OpenAlex",
            "provider_score": item.get("relevance_score") or 0,
            "query": query,
            "oa_pdf_url": best_oa.get("pdf_url") or "",
        }
        if _is_primary_article(record):
            output.append(record)
    return output


def _literature_score(record, component_a, component_b, entrainer="", selected_method=""):
    title = str(record.get("title") or "").lower()
    score = 0.0
    for component in (component_a, component_b):
        if any(term in title for term in _terms(component)):
            score += 12
    if entrainer:
        exact_entrainer_in_title = any(term in title for term in _terms(entrainer))
        family_in_title = any(term in title for term in FAMILY_TERMS.get(str(entrainer).upper(), []))
        if exact_entrainer_in_title:
            score += 18
        if family_in_title:
            score += 10
        if not exact_entrainer_in_title and not family_in_title:
            score -= 8
    for phrase, weight in (
        ("vapor-liquid", 7),
        ("vapour-liquid", 7),
        ("extractive distillation", 8),
        ("experimental", 5),
        ("thermodynamic", 3),
        ("cosmo", 4),
    ):
        if phrase in title:
            score += weight
    if selected_method and selected_method.lower() in title:
        score += 6
    if record.get("type") == "journal-article":
        score += 3
    return score


def search_literature(component_a, component_b, entrainer="", selected_method="", rows_per_query=15):
    merged = {}
    failures = []
    for query in build_queries(component_a, component_b, entrainer, selected_method):
        for provider in (search_openalex, search_crossref):
            try:
                records = provider(query, rows=rows_per_query)
            except Exception as exc:
                failures.append({"provider": provider.__name__, "query": query, "error": str(exc)})
                continue
            for record in records:
                key = normalize_doi(record.get("doi")) or record.get("title", "").lower()
                existing = merged.get(key)
                if not existing:
                    merged[key] = record
                else:
                    existing["provider"] = "+".join(
                        dict.fromkeys((existing.get("provider") or "").split("+") + [record.get("provider") or ""])
                    ).strip("+")
                    if not existing.get("oa_pdf_url") and record.get("oa_pdf_url"):
                        existing["oa_pdf_url"] = record["oa_pdf_url"]
    records = list(merged.values())
    for record in records:
        record["relevance_score"] = _literature_score(
            record,
            component_a,
            component_b,
            entrainer,
            selected_method,
        )
        record["status"] = "仅元数据线索"
    records.sort(key=lambda item: (-item["relevance_score"], -(int(item.get("year") or 0)), item.get("title") or ""))
    return {"queries": build_queries(component_a, component_b, entrainer, selected_method), "records": records, "failures": failures}


def crossref_work(doi):
    doi = normalize_doi(doi)
    if not doi:
        return {}
    return (_json_get("{}/{}".format(CROSSREF_API, urllib.parse.quote(doi, safe=""))).get("message") or {})


def _xml_to_text(data):
    text = data.decode("utf-8", "replace")
    text = re.sub(r"<(?:ce:)?br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:ce:)?(?:para|section-title|title|row)>", "\n", text, flags=re.I)
    return _clean_text(text)


def _extract_pdf_text(data):
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError("pypdf is required for open PDF extraction: {}".format(exc))
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _collect_url_values(value):
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$" and isinstance(item, str) and item.startswith(("http://", "https://")):
                result.append(item)
            result.extend(_collect_url_values(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(_collect_url_values(item))
    return result


def openaire_locations(doi):
    doi = normalize_doi(doi)
    if not doi:
        return []
    url = "{}?doi={}&format=json".format(OPENAIRE_API, urllib.parse.quote(doi))
    payload = _json_get(url)
    urls = _collect_url_values(payload)
    preferred = []
    for item in urls:
        lowered = item.lower()
        if "doi.org" in lowered or "openaire" in lowered:
            continue
        if item not in preferred:
            preferred.append(item)
    return preferred


def _dspace_pdf_from_landing(url):
    data, final_url, content_type = _http(url, accept="text/html,application/pdf")
    if "pdf" in content_type.lower() or data.startswith(b"%PDF"):
        return data, final_url
    text = data.decode("utf-8", "replace")
    match = re.search(r"/entities/(?:publication|item)/([0-9a-f-]{36})", final_url, re.I)
    if not match:
        match = re.search(r"/entities/(?:publication|item)/([0-9a-f-]{36})", text, re.I)
    if not match:
        return None, ""
    item_uuid = match.group(1)
    parsed = urllib.parse.urlparse(final_url)
    base = "{}://{}".format(parsed.scheme, parsed.netloc)
    bundles = _json_get("{}/server/api/core/items/{}/bundles".format(base, item_uuid))
    bundle_rows = (bundles.get("_embedded") or {}).get("bundles") or []
    originals = [row for row in bundle_rows if str(row.get("name") or "").upper() == "ORIGINAL"]
    for bundle in originals:
        href = (((bundle.get("_links") or {}).get("bitstreams") or {}).get("href"))
        if not href:
            continue
        bitstreams = _json_get(href)
        rows = (bitstreams.get("_embedded") or {}).get("bitstreams") or []
        rows.sort(key=lambda item: 0 if str(item.get("name") or "").lower().endswith(".pdf") else 1)
        for bitstream in rows:
            content = (((bitstream.get("_links") or {}).get("content") or {}).get("href"))
            if not content:
                continue
            pdf, final_pdf_url, pdf_type = _http(content, accept="application/pdf")
            if "pdf" in pdf_type.lower() or pdf.startswith(b"%PDF"):
                return pdf, final_pdf_url
    return None, ""


def obtain_full_text(record):
    attempts = []
    doi = normalize_doi(record.get("doi"))
    if doi:
        try:
            metadata = crossref_work(doi)
            for link in metadata.get("link") or []:
                url = link.get("URL") or ""
                content_type = str(link.get("content-type") or "").lower()
                if not url:
                    continue
                try:
                    data, final_url, returned_type = _http(url, accept=content_type or "text/plain")
                    if "pdf" in (content_type + returned_type).lower() or data.startswith(b"%PDF"):
                        text = _extract_pdf_text(data)
                    else:
                        text = _xml_to_text(data) if ("xml" in content_type or b"<" in data[:30]) else data.decode("utf-8", "replace")
                    if len(_clean_text(text)) >= 4000:
                        return {"status": "已取得全文", "text": text, "source_url": final_url, "source_route": "Crossref全文挖掘链接", "attempts": attempts}
                    attempts.append({"url": final_url, "result": "返回内容不是完整正文"})
                except Exception as exc:
                    attempts.append({"url": url, "result": "失败：{}".format(exc)})
        except Exception as exc:
            attempts.append({"url": "Crossref DOI metadata", "result": "失败：{}".format(exc)})

    direct_urls = []
    if record.get("oa_pdf_url"):
        direct_urls.append(record["oa_pdf_url"])
    if doi:
        try:
            direct_urls.extend(openaire_locations(doi))
        except Exception as exc:
            attempts.append({"url": "OpenAIRE", "result": "失败：{}".format(exc)})
    for url in list(dict.fromkeys(direct_urls)):
        try:
            data, final_url = _dspace_pdf_from_landing(url)
            if not data:
                attempts.append({"url": url, "result": "未定位到开放PDF"})
                continue
            text = _extract_pdf_text(data)
            if len(_clean_text(text)) >= 4000:
                return {"status": "已取得全文", "text": text, "source_url": final_url, "source_route": "OpenAIRE机构库开放全文", "attempts": attempts}
            attempts.append({"url": final_url, "result": "PDF可打开但正文文本不足"})
        except Exception as exc:
            attempts.append({"url": url, "result": "失败：{}".format(exc)})
    return {"status": "仅元数据线索", "text": "", "source_url": record.get("url") or "", "source_route": "", "attempts": attempts}


def _keyword_windows(text, pattern, radius=360, limit=8):
    cleaned = _clean_text(text)
    rows = []
    for match in re.finditer(pattern, cleaned, re.I):
        start = max(0, match.start() - radius)
        end = min(len(cleaned), match.end() + radius)
        snippet = cleaned[start:end].strip()
        if snippet not in rows:
            rows.append(snippet)
        if len(rows) >= limit:
            break
    return rows


def _method_sentence_contexts(text, pattern, limit=12):
    cleaned = _clean_text(text)
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\[])", cleaned)
    rows = []
    for index, sentence in enumerate(sentences):
        if not re.search(pattern, sentence, re.I):
            continue
        previous = sentences[index - 1] if index > 0 else ""
        previous_second = sentences[index - 2] if index > 1 else ""
        following = sentences[index + 1] if index + 1 < len(sentences) else ""
        following_second = sentences[index + 2] if index + 2 < len(sentences) else ""
        rows.append(
            {
                "direct": sentence.strip(),
                "scope": " ".join(item.strip() for item in (previous_second, previous, sentence) if item.strip()),
                "expanded": " ".join(
                    item.strip() for item in (previous, sentence, following, following_second) if item.strip()
                ),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _method_scope(method_context, component_a, component_b, entrainer="", direct_context=""):
    context = _clean_text(method_context).lower()
    direct = _clean_text(direct_context).lower()
    base_present = all(any(term in context for term in _terms(component)) for component in (component_a, component_b))
    if {str(component_a).upper(), str(component_b).upper()} == {"ETHANOL", "H2O"}:
        base_present = base_present or bool(re.search(r"alcohol\s*\+\s*water|water\s*\+\s*alcohol", context, re.I))
    if not base_present:
        return "相似体系"
    if not entrainer:
        return "目标体系实验范围"
    exact_present = any(term in context for term in _terms(entrainer))
    family_present = any(term in context for term in FAMILY_TERMS.get(str(entrainer).upper(), []))
    ternary_context = bool(
        re.search(r"ternary|three[- ]component|alcohol\s*\+\s*water\s*\+|water\s*\+[^.;]{0,90}\+\s*ethanol", context, re.I)
    )
    explicit_binary_context = bool(
        re.search(r"binary\s+(?:system|mixture)|alcohol\s*\+\s*water\s+binary", context, re.I)
    )
    if re.search(r"binary\s+(?:system|mixture)|alcohol\s*\+\s*water\s+binary", direct, re.I):
        return "仅基础二元体系"
    if explicit_binary_context and not ternary_context:
        return "仅基础二元体系"
    if exact_present and ternary_context:
        return "目标体系实验范围"
    if family_present and ternary_context:
        return "基础体系相同、同类萃取剂实验范围"
    if explicit_binary_context:
        return "仅基础二元体系"
    if exact_present and EXPERIMENT_WORDS.search(context):
        return "目标体系实验范围"
    if family_present and EXPERIMENT_WORDS.search(context):
        return "基础体系相同、同类萃取剂实验范围"
    if exact_present:
        return "目标萃取剂在正文出现，但未确认实验范围"
    if family_present:
        return "同类萃取剂在正文出现，但未确认实验范围"
    return "仅基础二元体系"


def extract_tabular_validation_metrics(text):
    """Extract conservative, recognizable experiment-vs-model tables.

    The first supported pattern is the common light-alcohol/water azeotrope
    validation table.  A metric is emitted only when the header identifies the
    experiment and model columns and at least two named rows are recovered.
    """
    cleaned = _clean_text(text)
    results = []
    header = re.search(
        r"Table\s+\d+\s+Validation\s+of\s+the\s+water[- ]azeotrope\s+point"
        r"[^.]{0,500}?predictions\s+using\s+the\s+(COSMO[- ]?SAC|COSMOSAC)\s+model",
        cleaned,
        re.I,
    )
    if not header:
        return results
    window = cleaned[header.start() : min(len(cleaned), header.end() + 1200)]
    row_pattern = re.compile(
        r"(Ethanol|2[- ]?Propanol|(?:tert|t)[- ]?Butanol)\s+"
        r"(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)",
        re.I,
    )
    rows = []
    for label, experimental, predicted in row_pattern.findall(window):
        exp_value = float(experimental)
        pred_value = float(predicted)
        absolute_error = abs(pred_value - exp_value)
        relative_error = absolute_error / abs(exp_value) * 100.0 if exp_value else None
        rows.append(
            {
                "体系": label,
                "实验值": exp_value,
                "模型预测值": pred_value,
                "绝对误差": round(absolute_error, 6),
                "相对误差百分比": round(relative_error, 6) if relative_error is not None else None,
            }
        )
    if len(rows) < 2:
        return results
    relative_errors = [row["相对误差百分比"] for row in rows if row["相对误差百分比"] is not None]
    results.append(
        {
            "表格": "水–轻质醇共沸点实验值与模型预测值",
            "模型": "COSMO-SAC",
            "指标": "共沸点水质量分数",
            "单位": "wt%",
            "验证范围": "仅基础二元体系",
            "数据行": rows,
            "平均绝对误差": round(sum(row["绝对误差"] for row in rows) / len(rows), 6),
            "平均相对误差百分比": round(sum(relative_errors) / len(relative_errors), 6) if relative_errors else None,
            "最大相对误差百分比": round(max(relative_errors), 6) if relative_errors else None,
            "自动提取说明": "由正文表格自动读取；正式使用前仍需核对原始表格排版和单位。",
        }
    )
    return results


def extract_method_evidence(text, component_a, component_b, entrainer=""):
    cleaned = _clean_text(text)
    lower = cleaned.lower()
    tabular_metrics = extract_tabular_validation_metrics(cleaned)
    methods = []
    for name, pattern in METHOD_PATTERNS.items():
        matches = list(re.finditer(pattern, cleaned, re.I))
        if not matches:
            continue
        snippets = _keyword_windows(cleaned, pattern)
        sentence_contexts = _method_sentence_contexts(cleaned, pattern)
        validation_contexts = [item for item in sentence_contexts if VALIDATION_WORDS.search(item["direct"])]
        selected_contexts = validation_contexts or sentence_contexts
        method_context = " ".join(item["expanded"] for item in selected_contexts)
        scope_context = " ".join(item["scope"] for item in selected_contexts)
        direct_context = " ".join(item["direct"] for item in sentence_contexts)
        explicitly_binary = bool(validation_contexts) and all(
            re.search(r"binary\s+(?:system|mixture)|alcohol\s*\+\s*water\s+binary", item["scope"], re.I)
            and not re.search(r"ternary|three[- ]component|alcohol\s*\+\s*water\s*\+", item["direct"], re.I)
            for item in validation_contexts
        )
        scope_priority = {
            "目标体系实验范围": 5,
            "基础体系相同、同类萃取剂实验范围": 4,
            "仅基础二元体系": 3,
            "目标萃取剂在正文出现，但未确认实验范围": 2,
            "同类萃取剂在正文出现，但未确认实验范围": 1,
            "相似体系": 0,
        }
        method_scopes = [
            _method_scope(item["scope"], component_a, component_b, entrainer, direct_context=item["direct"])
            for item in selected_contexts
        ]
        method_scope = max(method_scopes, key=lambda item: scope_priority.get(item, -1)) if method_scopes else _method_scope(
            scope_context,
            component_a,
            component_b,
            entrainer,
        )
        method_tables = [
            table for table in tabular_metrics
            if str(table.get("模型") or "").upper() == name.upper()
        ]
        methods.append(
            {
                "方法": name,
                "正文出现次数": len(matches),
                "与实验验证同时出现": bool(
                    EXPERIMENT_WORDS.search(method_context) and VALIDATION_WORDS.search(direct_context)
                ),
                "包含数值误差指标": bool(METRIC_WORDS.search(method_context) or method_tables),
                "涉及参数回归": bool(PARAMETER_WORDS.search(method_context)),
                "验证范围": method_scope,
                "正文是否明确限定为二元体系": explicitly_binary,
                "数值验证明细": method_tables,
                "证据原文片段": snippets[:4],
            }
        )
    base_exact = all(any(term in lower for term in _terms(component)) for component in (component_a, component_b))
    entrainer_exact = True
    analog_family = False
    if entrainer:
        entrainer_exact = any(term in lower for term in _terms(entrainer))
        analog_family = any(term in lower for term in FAMILY_TERMS.get(str(entrainer).upper(), []))
    experimental_windows = _keyword_windows(cleaned, EXPERIMENT_WORDS.pattern, radius=500, limit=12)
    experimental_context = " ".join(experimental_windows).lower()
    exact_in_experimental_context = base_exact and (
        not entrainer or any(term in experimental_context for term in _terms(entrainer))
    )
    if exact_in_experimental_context:
        scope = "目标体系实验范围"
    elif base_exact and analog_family:
        scope = "基础体系相同、同类萃取剂实验范围"
    elif base_exact:
        scope = "仅基础二元体系"
    else:
        scope = "相似体系"
    if scope in {"目标体系实验范围", "基础体系相同、同类萃取剂实验范围"}:
        for method in methods:
            if (
                method.get("与实验验证同时出现")
                and not method.get("正文是否明确限定为二元体系")
                and method.get("验证范围") in {
                    "仅基础二元体系",
                    "目标萃取剂在正文出现，但未确认实验范围",
                    "同类萃取剂在正文出现，但未确认实验范围",
                    "相似体系",
                }
            ):
                method["验证范围"] = scope
    return {
        "全文长度": len(cleaned),
        "存在实验数据描述": bool(EXPERIMENT_WORDS.search(cleaned)),
        "存在验证或比较描述": bool(VALIDATION_WORDS.search(cleaned)),
        "存在数值误差指标": bool(METRIC_WORDS.search(cleaned)),
        "体系匹配范围": scope,
        "基础组分均在正文出现": base_exact,
        "目标萃取剂在正文出现": entrainer_exact,
        "目标萃取剂在实验上下文出现": exact_in_experimental_context,
        "物性方法证据": sorted(methods, key=lambda item: (-int(item["与实验验证同时出现"]), -item["正文出现次数"], item["方法"])),
        "自动提取的数值验证": tabular_metrics,
        "实验与验证原文片段": experimental_windows[:5],
    }


def audit_property_method(component_a, component_b, entrainer="", selected_method="", max_full_text_articles=6):
    search = search_literature(component_a, component_b, entrainer, selected_method)
    audited = []
    for record in search["records"][:max_full_text_articles]:
        row = dict(record)
        full_text = obtain_full_text(record)
        row["full_text_status"] = full_text["status"]
        row["full_text_source_url"] = full_text.get("source_url") or ""
        row["full_text_source_route"] = full_text.get("source_route") or ""
        row["retrieval_attempts"] = full_text.get("attempts") or []
        if full_text.get("text"):
            row["status"] = "已分析正文"
            row["evidence"] = extract_method_evidence(
                full_text["text"],
                component_a,
                component_b,
                entrainer,
            )
        else:
            row["evidence"] = {}
        audited.append(row)

    selected = str(selected_method or "").strip().upper()
    selected_support = []
    alternative_support = []
    for article in audited:
        evidence = article.get("evidence") or {}
        for method in evidence.get("物性方法证据") or []:
            item = {
                "方法": method.get("方法"),
                "题名": article.get("title"),
                "DOI": article.get("doi"),
                "体系匹配范围": method.get("验证范围") or evidence.get("体系匹配范围"),
                "与实验验证同时出现": method.get("与实验验证同时出现"),
                "包含数值误差指标": method.get("包含数值误差指标"),
                "数值验证明细": method.get("数值验证明细") or [],
                "正文来源": article.get("full_text_source_url"),
            }
            if method.get("方法", "").upper() == selected:
                selected_support.append(item)
            elif method.get("与实验验证同时出现"):
                alternative_support.append(item)

    exact_validated = [
        row for row in selected_support
        if row.get("与实验验证同时出现") and row.get("体系匹配范围") == "目标体系实验范围"
    ]
    analog_validated = [
        row for row in selected_support
        if row.get("与实验验证同时出现") and row.get("体系匹配范围") == "基础体系相同、同类萃取剂实验范围"
    ]
    base_only_validated = [
        row for row in selected_support
        if row.get("与实验验证同时出现") and row.get("体系匹配范围") == "仅基础二元体系"
    ]
    if exact_validated:
        verdict = "验证通过"
        confidence = "高" if any(row.get("包含数值误差指标") for row in exact_validated) else "中"
        main_judgment = "正文证据表明，当前物性方法已在目标体系实验范围内进行验证。仍需单独确认 Aspen 参数及运行范围。"
    elif analog_validated:
        verdict = "有条件通过（同类体系）"
        confidence = "中"
        main_judgment = "当前物性方法得到基础体系或同类萃取剂实验支持，但尚未完成目标萃取剂全体系的直接验证。"
    elif base_only_validated:
        verdict = "仅基础体系通过"
        confidence = "中"
        main_judgment = "当前物性方法只在原料二元体系中得到实验支持，不能据此证明它适用于加入目标萃取剂后的三元体系。"
    elif selected_support:
        verdict = "证据不足"
        confidence = "低"
        main_judgment = "正文提到了当前物性方法，但没有提取到足以证明其在目标体系中表现良好的实验验证证据。"
    elif alternative_support:
        verdict = "当前选择未获验证"
        confidence = "低"
        main_judgment = "检索到其他物性方法的实验验证证据，但没有找到当前选择方法的等强度证据；应进行受控模型比较。"
    else:
        verdict = "证据不足"
        confidence = "低"
        main_judgment = "目前只获得文献线索，或开放全文证据不足，不能验证当前物性方法。"

    return {
        "报告名称": "物性方法自动文献审查测试报告",
        "体系": "{}–{}{}".format(
            COMPONENT_NAMES_ZH.get(str(component_a).upper(), component_a),
            COMPONENT_NAMES_ZH.get(str(component_b).upper(), component_b),
            "–{}".format(COMPONENT_NAMES_ZH.get(str(entrainer).upper(), entrainer)) if entrainer else "",
        ),
        "待验证物性方法": selected_method or "未指定",
        "审查结论": verdict,
        "置信度": confidence,
        "主要判断": main_judgment,
        "参数状态": "未在本步骤自动认定；需要从原始表格提取或重新回归并独立验证",
        "Aspen运行决定": "仅物性方法验证通过仍不自动放行，必须同时通过参数门禁",
        "检索式": search["queries"],
        "检索失败记录": search["failures"],
        "候选文献数量": len(search["records"]),
        "已尝试全文审查数量": len(audited),
        "已取得并分析正文数量": sum(1 for row in audited if row.get("status") == "已分析正文"),
        "当前方法证据": selected_support,
        "其他方法证据": alternative_support,
        "文献审查明细": audited,
    }


def render_report_zh(report):
    lines = [
        "# {}".format(report.get("报告名称") or "物性方法自动文献审查测试报告"),
        "",
        "- 体系：{}".format(report.get("体系") or ""),
        "- 待验证物性方法：{}".format(report.get("待验证物性方法") or "未指定"),
        "- 审查结论：{}".format(report.get("审查结论") or "证据不足"),
        "- 置信度：{}".format(report.get("置信度") or "低"),
        "- 已分析正文：{}/{}".format(report.get("已取得并分析正文数量") or 0, report.get("已尝试全文审查数量") or 0),
        "",
        "## 主要判断",
        "",
        str(report.get("主要判断") or ""),
        "",
        "参数状态：{}".format(report.get("参数状态") or ""),
        "",
        "Aspen运行决定：{}".format(report.get("Aspen运行决定") or ""),
        "",
        "## 当前方法证据",
        "",
    ]
    support = report.get("当前方法证据") or []
    if support:
        for index, item in enumerate(support, 1):
            lines.append(
                "{}. {}（DOI：{}；范围：{}；实验验证：{}；数值误差：{}）".format(
                    index,
                    item.get("题名") or "未命名文献",
                    item.get("DOI") or "未提供",
                    item.get("体系匹配范围") or "未判断",
                    "是" if item.get("与实验验证同时出现") else "否",
                    "有" if item.get("包含数值误差指标") else "未提取到",
                )
            )
            for metric in item.get("数值验证明细") or []:
                lines.append(
                    "   - {}：平均绝对误差 {} {}，平均相对误差 {}%，最大相对误差 {}%；范围：{}。".format(
                        metric.get("指标") or "验证指标",
                        metric.get("平均绝对误差"),
                        metric.get("单位") or "",
                        metric.get("平均相对误差百分比"),
                        metric.get("最大相对误差百分比"),
                        metric.get("验证范围") or "未判断",
                    )
                )
    else:
        lines.append("尚未提取到支持当前方法的正文级证据。")
    lines.extend(["", "## 其他方法证据", ""])
    alternatives = report.get("其他方法证据") or []
    if alternatives:
        for index, item in enumerate(alternatives, 1):
            lines.append(
                "{}. {}：{}（DOI：{}；范围：{}）".format(
                    index,
                    item.get("方法") or "未识别方法",
                    item.get("题名") or "未命名文献",
                    item.get("DOI") or "未提供",
                    item.get("体系匹配范围") or "未判断",
                )
            )
            for metric in item.get("数值验证明细") or []:
                lines.append(
                    "   - {}：平均绝对误差 {} {}，平均相对误差 {}%，最大相对误差 {}%；范围：{}。".format(
                        metric.get("指标") or "验证指标",
                        metric.get("平均绝对误差"),
                        metric.get("单位") or "",
                        metric.get("平均相对误差百分比"),
                        metric.get("最大相对误差百分比"),
                        metric.get("验证范围") or "未判断",
                    )
                )
    else:
        lines.append("未提取到其他方法的实验验证证据。")
    lines.extend(["", "## 文献审查明细", ""])
    for index, item in enumerate(report.get("文献审查明细") or [], 1):
        lines.append(
            "{}. {}（{}；DOI：{}；相关性分数：{}）".format(
                index,
                item.get("title") or "未命名文献",
                item.get("status") or "仅元数据线索",
                item.get("doi") or "未提供",
                item.get("relevance_score") or 0,
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="自动检索并审查物性方法文献证据")
    parser.add_argument("--component-a", required=True)
    parser.add_argument("--component-b", required=True)
    parser.add_argument("--entrainer", default="")
    parser.add_argument("--selected-method", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-full-text-articles", type=int, default=6)
    args = parser.parse_args(argv)
    report = audit_property_method(
        args.component_a,
        args.component_b,
        args.entrainer,
        args.selected_method,
        max_full_text_articles=args.max_full_text_articles,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "物性方法自动文献审查测试报告.json"
    markdown_path = output_dir / "物性方法自动文献审查测试报告.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_report_zh(report), encoding="utf-8")
    print(json.dumps({"json": str(json_path.resolve()), "markdown": str(markdown_path.resolve()), "verdict": report["审查结论"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
