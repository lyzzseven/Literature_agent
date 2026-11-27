#!/usr/bin/env python3
"""Run a configurable literature and data extraction job."""
import argparse
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sys
from tempfile import mkdtemp

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException

from extraction import extract_observations
from pdf_reader import inspect_pdf, pdf_identity, table_matches
from sources import brief_error, fetch_pdf, merge_publication, publication_record, search, unpaywall


FIGURE_KEYWORDS = {
    "microstructure": ["SEM", "TEM", "EBSD", "micrograph", "microstructure", "cross-section",
                       "scanning electron", "transmission electron", "显微组织", "截面形貌", "扫描电镜"],
    "performance": ["polarization", "polarisation", "Nyquist", "Bode", "stress–strain",
                    "stress-strain", "corrosion current", "wear rate", "hardness", "fatigue",
                    "fracture toughness", "Peierls barrier", "极化曲线", "电化学阻抗", "硬度", "磨损率", "应力应变"],
}
OUTPUT_KEYS = {"data", "formats", "categories", "tables", "table_keywords", "figure_types", "figure_keywords"}


def list_strings(value, name):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{name} 须为字符串数组")
    return [x.strip() for x in value]


def integer(value, lo, hi, name):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"{name} 须为 {lo}–{hi} 的整数")
    return value


def load_layout(layout):
    if not isinstance(layout, dict) or set(layout) - {"sha256", "text_pages", "text_y_tolerance", "figures", "tables"}:
        raise ValueError("pdf_layout 包含未识别选项，请参照版式配置示例")
    digest = layout.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("pdf_layout.sha256 须为对应 PDF 的 SHA-256")
    for key in ("text_pages", "figures", "tables"):
        if key in layout and not isinstance(layout[key], list):
            raise ValueError(f"pdf_layout.{key} 须为数组")
    for page in layout.get("text_pages", []):
        integer(page, 1, 500, "pdf_layout.text_pages")
    if "text_y_tolerance" in layout:
        integer(layout["text_y_tolerance"], 1, 10, "pdf_layout.text_y_tolerance")
    for key in ("figures", "tables"):
        for item in layout.get(key, []):
            if not isinstance(item, dict):
                raise ValueError(f"pdf_layout.{key} 的每项须为对象")
            integer(item.get("page"), 1, 500, key + ".page")
            for attribute in ("label", "caption"):
                if not isinstance(item.get(attribute), str) or not item[attribute].strip():
                    raise ValueError(f"{key}.{attribute} 须为非空字符串")
            if key == "figures":
                caption_pages = item.get("caption_pages")
                if not isinstance(caption_pages, list) or not caption_pages:
                    raise ValueError("figures.caption_pages 须为非空页码数组")
                for page in caption_pages:
                    integer(page, 1, 500, "figures.caption_pages")
            else:
                columns = list_strings(item.get("columns"), "tables.columns")
                for edge_key, minimum in (("column_edges", len(columns) + 1), ("row_edges", 2)):
                    edges = item.get(edge_key)
                    if (not isinstance(edges, list) or len(edges) < minimum
                        or any(type(x) not in (int, float) or not math.isfinite(x) for x in edges)
                        or any(a >= b for a, b in zip(edges, edges[1:]))):
                        raise ValueError(f"tables.{edge_key} 须为严格递增的边界坐标")
                if not columns or len(item["column_edges"]) != len(columns) + 1:
                    raise ValueError("tables.columns 与 column_edges 的数量须匹配")
    return layout


def load_config(path, topic, offline, *, extractor=None, ollama_model=None):
    with path.open(encoding="utf-8-sig") as file:
        cfg = json.load(file)
    if not isinstance(cfg, dict):
        raise ValueError("配置根节点须为 JSON 对象")
    cfg["topic"] = topic or cfg.get("topic")
    if not isinstance(cfg["topic"], str) or not cfg["topic"].strip():
        raise ValueError("请填写 topic 或提供 --topic")
    queries = [topic] if topic else cfg.get("queries", [cfg["topic"]])
    cfg["queries"] = [] if offline else list_strings(queries, "queries")
    cfg["local_pdfs"] = list_strings(cfg.get("local_pdfs", []), "local_pdfs")
    cfg["providers"] = list_strings(cfg.get("providers", ["crossref", "openalex"]), "providers")
    if not set(cfg["providers"]) <= {"crossref", "openalex"}:
        raise ValueError("providers 支持 crossref、openalex")
    cfg["search_limit"] = integer(cfg.get("search_limit", 12), 1, 100, "search_limit")
    cfg["max_downloads"] = integer(cfg.get("max_downloads", 6), 0, 100, "max_downloads")
    cfg["max_pages"] = integer(cfg.get("max_pages", 40), 1, 500, "max_pages")
    if "pdf_layout" in cfg:
        cfg["pdf_layout"] = load_layout(cfg["pdf_layout"])
    for key in ("year_from", "year_to"):
        cfg[key] = integer(cfg[key], 1500, 2200, key) if cfg.get(key) is not None else None
    if cfg["year_from"] and cfg["year_to"] and cfg["year_from"] > cfg["year_to"]:
        raise ValueError("year_from 不能晚于 year_to")
    download = cfg.get("download_open_pdf", True)
    if type(download) is not bool:
        raise ValueError("download_open_pdf 须为 true 或 false")
    cfg["download_open_pdf"] = download and not offline
    outputs = {
        "data": True, "formats": ["csv", "json"], "categories": [], "tables": True,
        "table_keywords": [], "figure_types": ["microstructure", "performance"], "figure_keywords": [],
    }
    overrides = cfg.get("outputs", {})
    if not isinstance(overrides, dict) or set(overrides) - OUTPUT_KEYS:
        raise ValueError("outputs 含未识别选项，请参照示例配置")
    outputs.update(overrides)
    for key in ("categories", "formats", "table_keywords", "figure_types", "figure_keywords"):
        outputs[key] = list_strings(outputs[key], "outputs." + key)
    if not outputs["formats"] or not set(outputs["formats"]) <= {"csv", "json"}:
        raise ValueError("outputs.formats 支持 csv、json，至少选一项")
    for key in ("data", "tables"):
        if type(outputs[key]) is not bool:
            raise ValueError(f"outputs.{key} 须为 true 或 false")
    cfg["outputs"] = outputs

    custom_keywords = cfg.get("figure_keywords", {})
    if not isinstance(custom_keywords, dict):
        raise ValueError("figure_keywords 须为图类到关键词数组的映射")
    cfg["figure_keywords"] = {**FIGURE_KEYWORDS, **{
        key: list_strings(words, f"figure_keywords.{key}") for key, words in custom_keywords.items()
    }}
    valid_types = {"all", "other", *cfg["figure_keywords"]}
    if set(outputs["figure_types"]) - valid_types:
        raise ValueError("figure_types 含未知类型，请添加对应的 figure_keywords")

    model = cfg.get("model", {})
    if not isinstance(model, dict) or model.get("mode", "rules") not in ("rules", "ollama"):
        raise ValueError("model.mode 支持 rules 或 ollama")
    cfg["model"] = {
        "mode": extractor or model.get("mode", "rules"),
        "name": model.get("name", "qwen3:4b") if ollama_model is None else ollama_model,
        "max_regions_per_paper": integer(model.get("max_regions_per_paper", 12), 1, 150,
                                         "model.max_regions_per_paper"),
        "timeout_seconds": integer(model.get("timeout_seconds", 180), 1, 1800, "model.timeout_seconds"),
    }
    if not isinstance(cfg["model"]["name"], str) or not cfg["model"]["name"].strip():
        raise ValueError("model.name 须为非空字符串")
    cfg["model"]["name"] = cfg["model"]["name"].strip()

    seen = set()
    fields = cfg.get("fields", [])
    if not isinstance(fields, list):
        raise ValueError("fields 须为数组")
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("每个 fields 项须为对象")
        for key in ("id", "label", "category"):
            if not isinstance(field.get(key), str) or not field[key].strip():
                raise ValueError(f"每个字段须提供 {key}")
        if field["id"] in seen:
            raise ValueError(f'字段 ID 重复：{field["id"]}')
        seen.add(field["id"])
        field.setdefault("description", "")
        field["aliases"] = list_strings(field.get("aliases", []), f'{field["id"]}.aliases')
        if type(field.get("use_model", True)) is not bool:
            raise ValueError(f'{field["id"]}.use_model 须为 true 或 false')
        if "units" in field:
            if not isinstance(field["units"], list) or any(not isinstance(u, str) for u in field["units"]):
                raise ValueError(f'{field["id"]}.units 须为字符串数组')
            field["units"] = [unit.strip() for unit in field["units"]]
        if "value_pattern" in field:
            if not isinstance(field["value_pattern"], str) or not field["value_pattern"]:
                raise ValueError(f'{field["id"]}.value_pattern 须为非空正则字符串')
            re.compile(field["value_pattern"])
        for key in ("pattern", "table_pattern"):
            if field.get(key) is not None:
                pattern = re.compile(field[key])
                if "value" not in pattern.groupindex:
                    raise ValueError(f'{field["id"]}.{key} 缺少命名捕获组 value')
    if set(outputs["categories"]) - {field["category"] for field in fields}:
        raise ValueError("outputs.categories 含未定义字段类别")
    cfg["fields"] = [
        field for field in fields
        if outputs["data"] and (not outputs["categories"] or field["category"] in outputs["categories"])
    ]
    return cfg


def safe_cell(value):
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.lstrip(" \r\n\t")[:1] in ("=", "+", "-", "@"):
        # Spreadsheet apps may execute formulas embedded in a PDF's text.
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?", value):
            return "'" + value
    return value


def write_csv(path, rows, columns=None):
    columns = columns or list(dict.fromkeys(k for row in rows for k in row)) or ["status"]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: safe_cell(row.get(k)) for k in columns})


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def local_records(records, paths, warn):
    hashes = set()
    for path in paths:
        if not path.is_file():
            warn(f"本地 PDF 不存在：{path}")
            continue
        try:
            digest = sha256(path.read_bytes()).hexdigest()
            if digest in hashes:
                continue
            with pdfplumber.open(path) as doc:
                title, doi = pdf_identity(doc, path)
            hashes.add(digest)
            item = publication_record(title, doi=doi, source="local_pdf")
            item["pdf"] = str(path)
            record = merge_publication(records, item)
            record.setdefault("sha256", digest)
        except (OSError, ValueError, RuntimeError, PdfminerException) as error:
            warn(f"本地 PDF 无法打开（{path.name}）：{brief_error(error)}")


def fetch_papers(records, config, output, warn):
    if not config["download_open_pdf"]:
        return
    email = os.getenv("UNPAYWALL_EMAIL")
    downloads = 0
    for record in records:
        if record["pdf"]:
            continue
        if downloads >= config["max_downloads"]:
            record["download_status"] = "budget_skipped"
            continue
        try:
            if not record["open_access"] and email and record["doi"]:
                record["open_access"] = unpaywall(record["doi"], email)
            oa = record["open_access"]
            if not oa.get("pdf_url"):
                record["download_status"] = "oa_pdf_not_found"
                continue
            downloads += 1
            path = output / "pdfs" / f'{record["paper_id"]}.pdf'
            fetch_pdf(oa["pdf_url"], path)
            with pdfplumber.open(path) as doc:
                _, found_doi = pdf_identity(doc, path)
            if found_doi and record["doi"] and found_doi != record["doi"]:
                record["download_status"] = "pdf_doi_mismatch"
                warn(f'{record["paper_id"]} PDF 首页 DOI 与检索结果不同，已保留文件供核查。')
                continue
            record["pdf"] = str(path)
            record["sha256"] = sha256(path.read_bytes()).hexdigest()
            record["download_status"] = "oa_pdf_doi_matched" if found_doi and record["doi"] else "oa_pdf_identity_review"
        except (OSError, ValueError, TypeError, RuntimeError, PdfminerException) as error:
            record["download_status"] = "oa_pdf_error"
            warn(f'{record["paper_id"]} 全文下载或验证失败：{brief_error(error)}')


DATA_COLUMNS = [
    "paper_id", "title", "doi", "url", "pdf", "category", "field", "field_label", "value_as_reported",
    "unit_as_reported", "sample_as_reported", "conditions_as_reported", "page", "region", "evidence",
    "method", "status",
]


def export(output, cfg, papers, data, figures, tables, warnings, *, ollama_runs=None, search_run=None):
    parts = {"papers": papers}
    if cfg["outputs"]["data"]:
        parts["data"] = data
    if cfg["outputs"]["figure_types"]:
        parts["figure_index"] = figures
    if cfg["outputs"]["tables"]:
        parts["table_index"] = [{k: v for k, v in t.items() if k != "rows"} for t in tables]
    if "json" in cfg["outputs"]["formats"]:
        for name, rows in parts.items():
            write_json(output / f"{name}.json", rows)
    if "csv" in cfg["outputs"]["formats"]:
        for name, rows in parts.items():
            write_csv(output / f"{name}.csv", rows, DATA_COLUMNS if name == "data" else None)

    no_pdf = [p for p in papers if not p["pdf"]]
    manifest = {
        "topic": cfg["topic"], "queries": cfg["queries"], "configuration": cfg,
        "counts": {
            "papers": len(papers), "metadata_only": len(no_pdf),
            "readable_pdfs": sum(p.get("text_pages", 0) > 0 for p in papers),
            "data_records": len(data), "figures": len(figures), "tables": len(tables),
        },
        "unavailable_full_text": [
            {k: p.get(k) for k in ("paper_id", "title", "doi", "url", "download_status")} for p in no_pdf
        ],
        "warnings": warnings,
    }
    if ollama_runs is not None:
        manifest["ollama"] = {
            "exported_records": sum(row["method"] == "ollama_literal_checked" for row in data),
            "papers": ollama_runs,
        }
    if search_run is not None:
        manifest["search"] = search_run
    write_json(output / "manifest.json", manifest)
    return manifest


def run(args):
    config_path = args.config.resolve()
    cfg = load_config(config_path, args.topic, args.offline,
                      extractor=args.extractor, ollama_model=args.ollama_model)
    if args.provider:
        cfg["providers"] = list(dict.fromkeys(args.provider))
    if args.search_only:
        if not cfg["queries"]:
            cfg["queries"] = [cfg["topic"]]
        if not cfg["providers"]:
            raise ValueError("仅检索模式需要至少一个检索来源")
        cfg["download_open_pdf"] = False
        cfg["local_pdfs"] = []
        cfg["fields"] = []
        cfg["outputs"].update(data=False, tables=False, figure_types=[])
    output_base = args.out.resolve()
    output_base.mkdir(parents=True, exist_ok=True)
    output = Path(mkdtemp(prefix="run-", dir=output_base))
    if not args.search_only:
        for sub in ("figures", "tables", "pdfs"):
            (output / sub).mkdir()
    warnings = []

    def warn(message):
        warnings.append(message)
        print(message, file=sys.stderr)

    search_run = None
    if cfg["queries"] and cfg["providers"]:
        search_run = {"started_at": datetime.now(timezone.utc).isoformat(),
                      "mode": "search_only" if args.search_only else "search_and_read",
                      "requests": []}
        print("联网检索：" + "、".join(cfg["providers"]), flush=True)
    records = search(cfg["queries"], cfg["providers"], cfg["search_limit"], cfg["year_from"],
                     cfg["year_to"], warn, request_log=search_run["requests"] if search_run else None)
    if not args.search_only:
        paths = [config_path.parent / p for p in cfg["local_pdfs"]] + args.pdf
        local_records(records, [p.resolve() for p in paths], warn)
    for index, record in enumerate(records, 1):
        record["paper_id"] = f"P{index:03d}"
    fetch_papers(records, cfg, output, warn)

    data, figures, tables = [], [], []
    ollama_runs = {} if cfg["model"]["mode"] == "ollama" and not args.search_only else None
    for record in records:
        if not record["pdf"]:
            continue
        print(f'读取 {record["paper_id"]}：{record["title"]}', flush=True)
        path = Path(record["pdf"])
        try:
            regions, paper_figures, paper_tables = inspect_pdf(path, record, cfg, output, warn)
            context = {k: record[k] for k in ("paper_id", "title", "doi", "url", "pdf")}
            stats = None
            if ollama_runs is not None and any(field.get("use_model", True) for field in cfg["fields"]):
                stats = ollama_runs.setdefault(record["paper_id"], {})
            data.extend({
                **context, **row
            } for row in extract_observations(regions, paper_tables, cfg["fields"], cfg, warn,
                                              record["paper_id"], ollama_stats=stats))
            figures.extend({**context, **row} for row in paper_figures)
            for table in (paper_tables if cfg["outputs"]["tables"] else []):
                if not table_matches(table["caption"], table["rows"], cfg["outputs"]["table_keywords"]):
                    continue
                name = table["table_id"]
                width = max(map(len, table["rows"]))
                if "csv" in cfg["outputs"]["formats"]:
                    csv_path = output / "tables" / f"{name}.csv"
                    columns = [f"column_{i + 1}" for i in range(width)]
                    write_csv(csv_path, [dict(zip(columns, r)) for r in table["rows"]], columns)
                    table["csv"] = f"tables/{csv_path.name}"
                if "json" in cfg["outputs"]["formats"]:
                    json_path = output / "tables" / f"{name}.json"
                    write_json(json_path, {**context, **table})
                    table["json"] = f"tables/{json_path.name}"
                tables.append({**context, **table})
        except (OSError, ValueError, RuntimeError, PdfminerException) as error:
            record["read_status"] = "read_error"
            warn(f'{record["paper_id"]} 读取失败：{brief_error(error)}')
    if not records:
        warn("没有检索到文献。" if args.search_only else "没有检索到文献或读入本地 PDF。")
    if cfg["outputs"]["data"] and not data:
        warn("没有通过提取规则或原文校验的数据记录。请检查选定字段和全文覆盖。")
    if cfg["model"]["mode"] == "rules" and cfg["outputs"]["data"] and any(
        not (f.get("pattern") or f.get("table_pattern")) for f in cfg["fields"]
    ):
        warn("规则模式会跳过未配置正文或表格规则的字段。")
    summary = export(output, cfg, records, data, figures, tables, warnings,
                     ollama_runs=ollama_runs, search_run=search_run)
    print(json.dumps(summary["counts"], ensure_ascii=False))
    print(f"输出目录：{output}")
    return 0 if records else 2


def main():
    parser = argparse.ArgumentParser(description="文献检索、PDF 阅读、数据及图表导出")
    parser.add_argument("config", type=Path, help="任务 JSON 配置")
    parser.add_argument("--topic", help="覆盖主题和检索词")
    parser.add_argument("--pdf", type=Path, action="append", default=[], help="追加本地 PDF，可重复")
    parser.add_argument("--out", type=Path, default=Path("runs"), help="输出根目录")
    network = parser.add_mutually_exclusive_group()
    network.add_argument("--offline", action="store_true", help="跳过外部文献检索和 PDF 下载")
    network.add_argument("--search-only", action="store_true", help="只联网检索题录并导出查询记录")
    parser.add_argument("--provider", choices=("crossref", "openalex"), action="append",
                        help="覆盖本次检索来源，可重复")
    parser.add_argument("--extractor", choices=("rules", "ollama"), help="覆盖配置中的 model.mode")
    parser.add_argument("--ollama-model", help="覆盖 Ollama 模型名称，与 --extractor ollama 搭配")
    args = parser.parse_args()
    if args.search_only and args.pdf:
        parser.error("--search-only 不能与 --pdf 同时使用")
    try:
        return run(args)
    except (OSError, ValueError, TypeError, re.error) as error:
        parser.exit(2, f"任务无法运行：{error}\n")


if __name__ == "__main__":
    sys.exit(main())
