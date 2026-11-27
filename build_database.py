"""Build a portable SQLite database from reviewed PEO data and agent run exports."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import urlsplit

from PIL import Image

from literature_agent import write_csv, write_json
from prepare_training import conversion, number, prepare, required_text
from sources import doi_id


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE papers (
  doi TEXT PRIMARY KEY, title TEXT NOT NULL, year INTEGER, doi_url TEXT,
  catalog_status TEXT, metadata_json TEXT NOT NULL
);
CREATE TABLE samples (
  id INTEGER PRIMARY KEY, doi TEXT NOT NULL REFERENCES papers, sample_key TEXT NOT NULL,
  study_group TEXT NOT NULL, substrate_grade TEXT NOT NULL, coating_scope TEXT NOT NULL,
  UNIQUE (doi, sample_key)
);
CREATE TABLE processes (
  sample_id INTEGER PRIMARY KEY REFERENCES samples, electrolyte_family TEXT NOT NULL,
  duration_s REAL CHECK(duration_s > 0), control_mode TEXT, pulse_mode TEXT, frequency_hz REAL,
  duty_pct REAL, current_setpoint_a_cm2 REAL, current_peak_a_cm2 REAL,
  current_unspecified_basis_a_cm2 REAL, source_url TEXT NOT NULL, locator TEXT NOT NULL
);
CREATE TABLE electrolyte_components (
  sample_id INTEGER NOT NULL REFERENCES samples, compound TEXT NOT NULL,
  concentration_g_l REAL NOT NULL CHECK(concentration_g_l >= 0), PRIMARY KEY(sample_id, compound)
);
CREATE TABLE protocols (
  id TEXT PRIMARY KEY, doi TEXT NOT NULL REFERENCES papers, method TEXT NOT NULL,
  medium TEXT, concentration REAL, concentration_unit TEXT, metadata_json TEXT NOT NULL
);
CREATE TABLE observations (
  id INTEGER PRIMARY KEY, sample_id INTEGER NOT NULL REFERENCES samples,
  protocol_id TEXT NOT NULL REFERENCES protocols, modality TEXT NOT NULL CHECK(modality IN ('structure', 'performance')),
  metric TEXT NOT NULL, value REAL, unit TEXT, value_text TEXT,
  raw_value TEXT NOT NULL, raw_unit TEXT, spread REAL CHECK(spread >= 0), spread_kind TEXT,
  exposure_h REAL CHECK(exposure_h >= 0), origin TEXT NOT NULL,
  source_url TEXT NOT NULL, locator TEXT NOT NULL,
  CHECK((value IS NOT NULL AND value_text IS NULL) OR (value IS NULL AND value_text IS NOT NULL))
);
CREATE UNIQUE INDEX uq_observation ON observations(sample_id, protocol_id, metric, coalesce(exposure_h, -1));
CREATE TABLE assets (
  id TEXT PRIMARY KEY, doi TEXT NOT NULL REFERENCES papers,
  kind TEXT NOT NULL CHECK(kind IN ('figure', 'table')), modality TEXT NOT NULL,
  label TEXT NOT NULL, page INTEGER, caption TEXT, source_url TEXT NOT NULL, rights TEXT NOT NULL,
  filename TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL, sha256 TEXT NOT NULL,
  width_px INTEGER, height_px INTEGER, metadata_json TEXT NOT NULL, content BLOB NOT NULL
);
CREATE TABLE sample_assets (
  sample_id INTEGER NOT NULL REFERENCES samples, asset_id TEXT NOT NULL REFERENCES assets,
  panel TEXT NOT NULL DEFAULT '', PRIMARY KEY(sample_id, asset_id, panel)
);
CREATE TABLE auto_candidates (
  id INTEGER PRIMARY KEY, doi TEXT NOT NULL REFERENCES papers, run_name TEXT NOT NULL,
  field TEXT, page INTEGER, evidence TEXT, record_json TEXT NOT NULL
);
CREATE VIEW quantitative_data AS
SELECT o.id AS observation_id, s.doi, s.sample_key, s.study_group, s.substrate_grade, s.coating_scope,
       p.electrolyte_family, p.duration_s, p.control_mode, p.pulse_mode, p.frequency_hz, p.duty_pct,
       p.current_setpoint_a_cm2, p.current_peak_a_cm2, p.current_unspecified_basis_a_cm2,
       o.modality, o.metric, o.value, o.unit, o.spread, o.spread_kind, o.exposure_h,
       o.protocol_id, t.method, t.medium, t.concentration, t.concentration_unit, o.origin,
       o.source_url, o.locator
FROM observations o JOIN samples s ON o.sample_id=s.id
LEFT JOIN processes p ON p.sample_id=s.id JOIN protocols t ON t.id=o.protocol_id
WHERE o.value IS NOT NULL;
CREATE VIEW model_candidates AS
SELECT * FROM quantitative_data
WHERE coating_scope='as_prepared_peo' AND duration_s IS NOT NULL
AND origin IN ('reported_table','reported_text');
CREATE VIEW sample_figure_index AS
SELECT s.doi, s.sample_key, s.study_group, a.id AS asset_id, a.modality, a.label, l.panel,
       a.filename, a.page, a.source_url
FROM sample_assets l JOIN samples s ON s.id=l.sample_id JOIN assets a ON a.id=l.asset_id
WHERE a.kind='figure';
"""

METRICS = {
    "thickness": ("structure", "µm", "thickness"),
    "surface_porosity": ("structure", "%", "%"),
    "cross_section_porosity": ("structure", "%", "%"),
    "roughness_ra": ("structure", "µm", "thickness"),
    "roughness_rz": ("structure", "µm", "thickness"),
    "morphology": ("structure", None, None),
    "polarization_resistance": ("performance", "Ω·cm²", "resistance"),
    "corrosion_current_density": ("performance", "A/cm²", "current_density"),
    "corrosion_potential_sce": ("performance", "V vs SCE", "V vs SCE"),
    "wear_volume": ("performance", "mm³", "wear_volume"),
}
PROCESS_KEYS = ("electrolyte_family", "duration_s", "control_mode", "pulse_mode", "frequency_hz",
                "duty_pct", "current_setpoint_a_cm2", "current_peak_a_cm2",
                "current_unspecified_basis_a_cm2")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def safe_url(value):
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("来源须为 HTTPS")
    return value


def path_in(folder, name):
    path = (folder / name).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError(f"图表路径超出目录：{name}")
    return path


def register_catalog(db, catalog):
    with catalog.open(encoding="utf-8-sig", newline="") as handle:
        for paper in csv.DictReader(handle):
            doi = doi_id(paper["doi"])
            if not doi:
                raise ValueError("题录需含 DOI")
            db.execute("INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?)",
                       (doi, paper["title"], paper["year"] or None, paper["doi_url"],
                        paper["evidence_status"], encoded(paper)))


def insert_sample(db, doi, sample_key, study_group, grade, scope, process):
    row_id = db.execute(
        "INSERT INTO samples(doi,sample_key,study_group,substrate_grade,coating_scope) VALUES (?,?,?,?,?)",
        (doi, sample_key, study_group, grade, scope),
    ).lastrowid
    db.execute("INSERT INTO processes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (row_id, *(process.get(key) for key in PROCESS_KEYS),
                safe_url(process["source_url"]), required_text(process, "process_locator")))
    db.executemany("INSERT INTO electrolyte_components VALUES (?,?,?)",
                   [(row_id, name, value) for name, value in process.get("electrolyte_components", {}).items()])
    return row_id


def register_protocol(db, protocol):
    db.execute("INSERT INTO protocols VALUES(?,?,?,?,?,?,?)", (
        protocol["id"], protocol["doi"], protocol["method"], protocol.get("medium"),
        protocol.get("concentration"), protocol.get("concentration_unit"), encoded(protocol),
    ))


def normalize_observation(record, protocol):
    metric = record["metric"]
    if metric not in METRICS:
        raise ValueError(f"未知指标：{metric}")
    modality, unit, kind = METRICS[metric]
    if modality == "performance" and not protocol.get("medium"):
        raise ValueError(f"{metric} 缺少测试环境")
    raw, raw_unit = record["value"], record.get("unit")
    if kind is None:
        if raw_unit is not None or record.get("spread") is not None:
            raise ValueError("定性观察须使用文字且无数值单位")
        return modality, None, None, required_text(record, "value"), None
    if kind == "resistance":
        factor = {"Ω·cm²": 1, "kΩ·cm²": 1000}.get(raw_unit)
    elif kind == "wear_volume":
        factor = {"mm³": 1, "10^-3 mm³": .001}.get(raw_unit)
    elif kind in ("%", "V vs SCE"):
        factor = 1 if raw_unit == unit else None
    else:
        factor = conversion(kind, raw_unit)
    if factor is None:
        raise ValueError(f"{metric} 单位不匹配：{raw_unit}")
    result = number(raw) * factor
    if (not math.isfinite(result) or (metric != "corrosion_potential_sce" and result < 0)
        or (unit == "%" and result > 100)):
        raise ValueError(f"{metric} 值越界")
    spread = number(record["spread"]) * factor if record.get("spread") is not None else None
    if spread is not None and (not math.isfinite(spread) or spread < 0 or not record.get("spread_kind")):
        raise ValueError(f"{metric} 离散量缺少口径或数值非法")
    return modality, result, unit, None, spread


def insert_observation(db, sample_id, protocol, record):
    modality, value, unit, value_text, spread = normalize_observation(record, protocol)
    if record["origin"] not in ("reported_table", "reported_text"):
        raise ValueError("此入口只接收原文直接报告的核定观测")
    exposure = number(record["exposure_h"]) if record.get("exposure_h") is not None else None
    db.execute("""INSERT INTO observations
        (sample_id,protocol_id,modality,metric,value,unit,value_text,raw_value,raw_unit,spread,
         spread_kind,exposure_h,origin,source_url,locator) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sample_id, protocol["id"], modality, record["metric"], value, unit, value_text,
         str(record["value"]), record.get("unit"), spread, record.get("spread_kind"), exposure,
         record["origin"], safe_url(record["source_url"]), required_text(record, "locator")))


def register_thickness(db, reviewed, samples, protocols):
    rows, rejected = prepare(reviewed)
    # Keep original units and labels from the reviewed source while reusing the v1 admission and dedup rules.
    raw_by_locator = {(doi_id(study["doi"]), sample["sample_id"], sample["thickness_locator"]): sample
                      for study in reviewed["studies"] for sample in study["samples"]
                      if isinstance(sample, dict) and {"sample_id", "thickness_locator"} <= sample.keys()}
    for row in rows:
        doi, sample = row["doi"], row["sample_id"]
        sample_id = insert_sample(db, doi, sample, row["study_group"], row["substrate_grade"],
                                  "as_prepared_peo", row)
        samples[doi, sample] = sample_id
        method = row["measurement_method"]
        key = f"{doi}:{method}"
        if key not in protocols:
            protocols[key] = {"id": key, "doi": doi, "method": method}
            register_protocol(db, protocols[key])
        raw = raw_by_locator[doi, sample, row["thickness_locator"]]
        insert_observation(db, sample_id, protocols[key], {
            "metric": "thickness", **raw["thickness"], "origin": raw["target_origin"],
            "source_url": row["source_url"], "locator": row["thickness_locator"],
        })
    return rejected


def insert_asset(db, record, content):
    width = height = None
    if record["kind"] == "figure":
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            image.verify()
    if Path(record["filename"]).name != record["filename"]:
        raise ValueError("图表导出文件名不能包含目录")
    db.execute("INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        record["id"], record["doi"], record["kind"], record["modality"], record["label"],
        record.get("page"), record.get("caption"), safe_url(record["source_url"]), record["rights"],
        record["filename"], record["mime_type"], hashlib.sha256(content).hexdigest(),
        width, height, encoded(record.get("metadata", {})), content,
    ))


def register_run(db, folder, spec):
    papers = {paper["paper_id"]: paper for paper in read_json(folder / "papers.json")}
    aliases = spec.get("doi_aliases", {})
    identity = {key: doi_id(aliases.get(key, paper.get("doi"))) for key, paper in papers.items()}
    name = folder.name
    for key, paper in papers.items():
        if not identity[key]:
            raise ValueError(f"{name}/{key} 需要 DOI 映射")
        db.execute("INSERT OR IGNORE INTO papers VALUES (?,?,?,?,?,?)", (
            identity[key], paper["title"], paper.get("year"), f"https://doi.org/{identity[key]}",
            "agent_extracted", encoded(paper),
        ))
    for filename, kind, file_key in (("figure_index.json", "figure", "file"), ("table_index.json", "table", "csv")):
        if not (folder / filename).is_file():
            continue
        for record in read_json(folder / filename):
            key = record["paper_id"]
            if kind == "table" and not record.get(file_key):
                file_key_used = "json"
            else:
                file_key_used = file_key
            source_file = path_in(folder, record[file_key_used])
            categories = record.get("categories") or [spec.get("modality", "unspecified")]
            insert_asset(db, {
                "id": f"{name}:{source_file.stem}", "doi": identity[key], "kind": kind,
                "modality": categories[0] if kind == "figure" else "tabular",
                "label": record.get("label") or source_file.stem, "page": record.get("page"),
                "caption": record.get("caption"), "filename": f"{name}-{source_file.name}",
                "mime_type": {"csv": "text/csv", "json": "application/json", "png": "image/png"}[source_file.suffix[1:]],
                "source_url": spec.get("source_urls", {}).get(key) or f"https://doi.org/{identity[key]}",
                "rights": spec.get("rights", "research_copy_rights_unverified"), "metadata": record,
            }, source_file.read_bytes())
    if (folder / "data.json").is_file():
        for record in read_json(folder / "data.json"):
            db.execute("INSERT INTO auto_candidates(doi,run_name,field,page,evidence,record_json) VALUES (?,?,?,?,?,?)",
                       (identity[record["paper_id"]], name, record.get("field"), record.get("page"),
                        record.get("evidence"), encoded(record)))


def register_annotations(db, config_folder, note, samples, protocols):
    for entry in note.get("samples", []):
        key = entry["doi"], entry["sample_key"]
        samples[key] = insert_sample(db, *key, entry.get("study_group", entry["doi"]),
                                     entry["substrate_grade"], entry["coating_scope"], entry["process"])
    for protocol in note.get("protocols", []):
        register_protocol(db, protocol)
        protocols[protocol["id"]] = protocol
    for record in note.get("observations", []):
        protocol = protocols[record["protocol_id"]]
        if protocol["doi"] != record["doi"]:
            raise ValueError("观测与检测协议必须来自同一论文")
        insert_observation(db, samples[record["doi"], record["sample_key"]], protocol, record)
    for record in note.get("assets", []):
        insert_asset(db, record, path_in(config_folder, record["path"]).read_bytes())
    for spec in note.get("links", []):
        row = db.execute("SELECT doi FROM assets WHERE id=?", (spec["asset_id"],)).fetchone()
        if row is None or row[0] != spec["doi"]:
            raise ValueError(f"图件与试样的论文不符：{spec['asset_id']}")
        db.execute("INSERT INTO sample_assets VALUES (?,?,?)", (
            samples[spec["doi"], spec["sample_key"]], spec["asset_id"], spec.get("panel", ""),
        ))


def build(config_path, database):
    config_path = config_path.resolve()
    folder = config_path.parent
    config = read_json(config_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=database.parent, prefix=".peo-db-") as temp:
        staged = Path(temp) / database.name
        db = sqlite3.connect(staged)
        try:
            with db:
                db.executescript(SCHEMA)
                register_catalog(db, folder / config["catalog"])
                samples, protocols = {}, {}
                rejected = register_thickness(db, read_json(folder / config["reviewed_thickness"]), samples, protocols)
                for spec in config.get("runs", []):
                    register_run(db, (folder / spec["path"]).resolve(), spec)
                if config.get("annotations"):
                    register_annotations(db, folder, read_json(folder / config["annotations"]), samples, protocols)
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("数据库存在未关联的记录")
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("数据库完整性检查失败")
        finally:
            db.close()
        staged.replace(database)
    return rejected


def export(db, folder):
    folder.mkdir(parents=True, exist_ok=True)
    asset_folder = folder / "assets"
    asset_folder.mkdir(exist_ok=True)
    db.row_factory = sqlite3.Row
    index = []
    for asset in db.execute("SELECT * FROM assets ORDER BY doi, id"):
        row = dict(asset)
        content = row.pop("content")
        path_in(asset_folder, row["filename"]).write_bytes(content)
        row["path"] = "assets/" + row["filename"]
        index.append(row)
    write_csv(folder / "asset_index.csv", index)
    for name, sql in {
        "observations": """SELECT s.doi, s.sample_key, s.study_group, o.* FROM observations o
                           JOIN samples s ON s.id=o.sample_id ORDER BY s.doi, s.sample_key, o.metric, o.exposure_h""",
        "model_candidates": "SELECT * FROM model_candidates ORDER BY doi,sample_key,metric,exposure_h",
        "sample_figures": "SELECT * FROM sample_figure_index ORDER BY doi,sample_key,asset_id,panel",
        "processes": "SELECT s.doi,s.sample_key,s.study_group,s.substrate_grade,p.* FROM samples s JOIN processes p ON p.sample_id=s.id ORDER BY s.doi,s.sample_key",
        "electrolyte_components": """SELECT s.doi,s.sample_key,e.compound,e.concentration_g_l
                                     FROM electrolyte_components e JOIN samples s ON s.id=e.sample_id
                                     ORDER BY s.doi,s.sample_key,e.compound""",
        "protocols": "SELECT * FROM protocols ORDER BY doi, id",
    }.items():
        cursor = db.execute(sql)
        write_csv(folder / f"{name}.csv", [dict(row) for row in cursor],
                  [column[0] for column in cursor.description])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="数据库输入配置 JSON")
    parser.add_argument("--out", type=Path, required=True, help="SQLite 文件")
    parser.add_argument("--export", type=Path, help="另导出互操作 CSV 与图表原文件")
    args = parser.parse_args()
    try:
        rejected = build(args.config, args.out)
        db = sqlite3.connect(args.out)
        try:
            tables = ("papers", "samples", "observations", "assets", "sample_assets", "auto_candidates")
            summary = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
            summary["asset_kinds"] = dict(db.execute("SELECT kind,count(*) FROM assets GROUP BY kind"))
            summary["reviewed_metrics"] = dict(db.execute("SELECT metric,count(*) FROM observations GROUP BY metric"))
            summary["rejected_thickness"] = rejected
            if args.export:
                export(db, args.export)
                write_json(args.export / "manifest.json", summary)
        finally:
            db.close()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(2, f"数据库构建失败：{error}\n")
    print(encoded(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
