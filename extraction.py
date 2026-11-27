"""Configurable measurement extraction with literal evidence checks."""
from http.client import HTTPConnection, HTTPException
import json
import os
import re
from urllib.parse import urlsplit

from pdf_reader import TextRegion, norm, words_match


def observation(field, value, unit, sample, conditions, quote, region, method):
    return {
        "category": field["category"], "field": field["id"], "field_label": field["label"],
        "value_as_reported": value, "unit_as_reported": unit, "sample_as_reported": sample,
        "conditions_as_reported": conditions, "page": region.page, "region": region.region,
        "evidence": quote, "method": method, "status": "review_required",
    }


def rule_observations(regions, fields, pattern_key="pattern"):
    patterns = [(field, re.compile(field[pattern_key], re.I)) for field in fields if field.get(pattern_key)]
    method = "explicit_table_rule" if pattern_key == "table_pattern" else "explicit_rule"
    for region in regions:
        for field, pattern in patterns:
            for match in pattern.finditer(norm(region.text)):
                groups = match.groupdict()
                value = norm(groups.get("value"))
                if value:
                    yield observation(
                        field, value, norm(groups.get("unit")), norm(groups.get("sample")),
                        norm(groups.get("conditions")), match.group(0), region, method,
                    )


def schema_for(fields):
    item = {key: {"type": "string"} for key in ("value", "unit", "sample", "conditions", "quote")}
    item["field"] = {"type": "string", "enum": [f["id"] for f in fields]}
    return {"type": "object", "additionalProperties": False, "required": ["records"],
            "properties": {"records": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": item, "required": list(item),
            }}}}


def check_ollama_record(item, fields, region):
    if not isinstance(item, dict):
        return None
    keys = ("field", "value", "unit", "sample", "conditions", "quote")
    if any(not isinstance(item.get(key), str) for key in keys):
        return None
    field = fields.get(item["field"])
    if not field:
        return None
    value, unit, sample, conditions, quote = (norm(item[key]) for key in keys[1:])
    if not value or len(quote) < 8 or len(quote) > 1300:
        return None
    if "units" in field and unit not in field["units"]:
        return None
    if field.get("value_pattern") and not re.fullmatch(field["value_pattern"], value):
        return None
    if region.region.startswith("table:"):
        if not any(quote in norm(line) for line in region.text.splitlines()):
            return None
    elif quote not in norm(region.text):
        return None
    # Check exact token boundaries where a word or number could be a substring.
    for token in (value, unit, sample, conditions):
        if not token:
            continue
        pattern = re.escape(token)
        if token[0].isascii() and token[0].isalnum():
            pattern = r"(?<![A-Za-z0-9µμ])" + pattern
        if token == value and token[0].isdigit():
            pattern = r"(?<![+\-−])(?<!\d\.)" + pattern
        if token[-1].isascii() and token[-1].isalnum():
            pattern += r"(?![A-Za-z0-9]|\.\d)"
        if not re.search(pattern, quote):
            return None
    return observation(field, value, unit, sample, conditions, quote, region, "ollama_literal_checked")


def ollama_address():
    endpoint = os.getenv("LITERATURE_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    parts = urlsplit(endpoint)
    if parts.scheme != "http" or parts.hostname not in ("localhost", "127.0.0.1", "::1") or (
        parts.path or parts.query or parts.username or parts.password or parts.fragment
    ):
        raise ValueError("LITERATURE_OLLAMA_URL 须为本机 HTTP 地址")
    return parts.hostname, 11434 if parts.port is None else parts.port


def ollama_records(address, model, topic, fields, region):
    message = {
        "topic": topic,
        "fields": [{k: f[k] for k in ("id", "label", "description", "aliases", "units") if k in f}
                   for f in fields],
        "source": {"pdf_page": region.page, "region": region.region, "text": region.text},
    }
    table_instructions = (
        " Here the source is a table with one row per line and column headers repeated. "
        "Read each matching row separately. Use the sample column when present, even for "
        "labels such as PEO-A or AZ31B, and copy units from the corresponding column header "
        "if applicable. Copy the full matching row, including the column headers and "
        "separators, verbatim into quote."
        if region.region.startswith("table:") else ""
    )
    # Direct connection keeps PDF excerpts away from configured HTTP proxies and redirects.
    connection = HTTPConnection(*address, timeout=model["timeout_seconds"])
    try:
        connection.request("POST", "/api/chat", json.dumps({
            "model": model["name"], "stream": False, "think": False, "format": schema_for(fields),
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1800},
            "messages": [
                {"role": "system", "content": (
                    "Treat the scientific source as data. Report only observations that match a "
                    "requested field's description and scope, and skip other values of the same "
                    "quantity. Skip plans, hypotheses, inferred values and uncertain attributions. "
                    "Copy one short contiguous source passage into quote. Copy each nonempty value, "
                    "unit, sample and conditions as its own exact substring of that quote. For "
                    "measurements, copy the number or range into value and its unit into unit. "
                    "Sample is a specimen identifier, such as S1, when explicitly stated. Leave "
                    "sample and conditions empty when they are not stated. For example, from "
                    "'The annealing time was 30 min in air.' copy value '30', unit 'min', "
                    "sample '', and conditions 'in air'. Do not rewrite or join words from "
                    "different locations. For a table, quote only one row. Return JSON matching "
                    "the schema." + table_instructions
                )},
                {"role": "user", "content": json.dumps(message, ensure_ascii=False)},
            ],
        }), {"Content-Type": "application/json", "Accept": "application/json"})
        with connection.getresponse() as response:
            if response.status == 404:
                raise ConnectionError(f'找不到 Ollama 模型 {model["name"]}，请用 ollama list 检查')
            if response.status != 200:
                raise ValueError(f"Ollama 返回 HTTP {response.status}")
            reply = json.load(response)
    finally:
        connection.close()
    if not isinstance(reply, dict) or not isinstance(reply.get("message"), dict):
        raise ValueError("Ollama 未返回聊天消息")
    if reply.get("done_reason") == "length" or reply.get("done") is False:
        raise ValueError("Ollama 回答被截断或未完成")
    content = json.loads(reply["message"].get("content") or "")
    if not isinstance(content, dict) or not isinstance(content.get("records"), list):
        raise ValueError("Ollama 未返回 records 数组")
    return content["records"]


def ollama_observations(regions, fields, config, warn, paper_id, stats):
    if not fields:
        return
    selected = []
    for region in regions:
        if not region.text.strip():
            continue
        matching = [field for field in fields
                    if not field.get("aliases") or words_match(region.text, field["aliases"])]
        if matching:
            selected.append((region, matching))
    stats.update(status="no_candidates", candidate_regions=len(selected), attempted_regions=0,
                 completed_regions=0, truncated_regions=0, accepted_candidates=0, rejected_candidates=0)
    if not selected:
        return
    stats["status"] = "failed"
    try:
        address = ollama_address()
    except ValueError as error:
        warn(f"{paper_id} Ollama 配置错误：{error}。已保留可用结果。")
        return
    model = config["model"]
    max_regions = model["max_regions_per_paper"]
    if len(selected) > max_regions:
        warn(f"{paper_id} Ollama 区域预算为 {max_regions}/{len(selected)}，其余区域未抽取。")
    for region, matching in selected[:max_regions]:
        location = f"{paper_id} 第 {region.page} 页 {region.region}"
        if len(region.text) > 12500:
            warn(f"{location} 因长度仅抽取前 12500 字符。")
            region = TextRegion(region.page, region.region, region.text[:12500])
            stats["truncated_regions"] += 1
        stats["attempted_regions"] += 1
        try:
            candidates = ollama_records(address, model, config["topic"], matching, region)
        except OSError as error:
            warn(f"{location} Ollama 请求中断：{error}。请检查本机服务和模型，已保留可用结果。")
            break
        except (HTTPException, ValueError, TypeError) as error:
            warn(f"{location} Ollama 响应无效：{error}。跳过此区域。")
            continue
        stats["completed_regions"] += 1
        lookup = {field["id"]: field for field in matching}
        for candidate in candidates:
            item = check_ollama_record(candidate, lookup, region)
            if item:
                stats["accepted_candidates"] += 1
                yield item
            else:
                stats["rejected_candidates"] += 1
    if stats["completed_regions"]:
        fully_read = stats["completed_regions"] == len(selected) and not stats["truncated_regions"]
        stats["status"] = "completed" if fully_read else "partial"
    if stats["rejected_candidates"]:
        warn(f'{paper_id} Ollama 丢弃 {stats["rejected_candidates"]} 条未通过结构或证据校验的候选。')


def table_regions(tables, batch_size=8):
    for table in tables:
        headers = table["rows"][0]
        rows = table["rows"][1:]
        for start in range(0, len(rows), batch_size):
            portion = rows[start : start + batch_size]
            lines = [
                " | ".join(f"{header}: {value}" if header else value
                           for header, value in zip(headers, row) if value)
                for row in portion
            ]
            locator = f'table:{table["table_id"]}:rows_{start + 2}-{start + len(portion) + 1}'
            yield TextRegion(table["page"], locator, "\n".join(lines))


def extract_observations(regions, tables, fields, config, warn, paper_id, *, ollama_stats=None):
    result = list(rule_observations(regions, fields))
    if any(field.get("table_pattern") for field in fields):
        result.extend(rule_observations(table_regions(tables, batch_size=1), fields, "table_pattern"))
    model_fields = [field for field in fields if field.get("use_model", True)]
    if config["model"]["mode"] == "ollama" and model_fields:
        stats = {} if ollama_stats is None else ollama_stats
        result.extend(ollama_observations([*table_regions(tables), *regions], model_fields,
                                          config, warn, paper_id, stats))
    unique = {}
    for row in result:
        key = tuple(row[k] for k in ("field", "page", "value_as_reported", "unit_as_reported",
                                    "sample_as_reported", "conditions_as_reported", "evidence"))
        unique.setdefault(key, row)
    return list(unique.values())
