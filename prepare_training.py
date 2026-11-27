"""Prepare source-reviewed PEO coating thickness measurements for regression."""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
from statistics import mean, median
from urllib.parse import urlsplit

from literature_agent import write_csv, write_json
from sources import doi_id


QUANTITIES = {
    "thickness": {"um": 1, "nm": .001, "mm": 1000},
    "duration": {"s": 1, "min": 60, "h": 3600},
    "frequency": {"hz": 1, "khz": 1000},
    "current_density": {"a/cm2": 1, "ma/cm2": .001, "a/dm2": .01, "ma/dm2": .00001},
    "duty_factor": {"%": 1, "fraction": 100},
    "concentration": {"g/l": 1, "mg/l": .001, "kg/m3": 1},
}
FEATURES = [
    "substrate_grade", "electrolyte_family", "control_mode", "pulse_mode",
    "duration_s", "frequency_hz", "duty_pct", "current_setpoint_a_cm2",
    "current_peak_a_cm2", "current_unspecified_basis_a_cm2",
]
TARGET = "thickness_um"
AUDIT_COLUMNS = [
    "study_group", "doi", "sample_id", "source_url", "process_locator", "thickness_locator",
    "measurement_method", "spread_um", "spread_kind", "electrolyte_components",
]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("需要有限实数")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("需要有限实数")
    return result


def conversion(kind, unit):
    if not isinstance(unit, str):
        raise ValueError(f"{kind}: 缺少单位")
    token = re.sub(r"[\s^·]", "", unit.casefold().replace("μ", "u").replace("µ", "u")
                   .replace("²", "2").replace("³", "3").replace("−", "-"))
    if kind == "current_density":
        token = re.sub(r"^(ma|a)(cm|dm)-2$", r"\1/\g<2>2", token)
    factor = QUANTITIES[kind].get(token)
    if factor is None:
        raise ValueError(f"{kind}: 单位不受支持 ({unit})")
    return factor


def quantity(kind, entry, *, allow_zero=False):
    if not isinstance(entry, dict):
        raise ValueError(f"{kind}: 缺少带单位的数值")
    result = number(entry.get("value")) * conversion(kind, entry.get("unit"))
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        raise ValueError(f"{kind}: 数值必须为有限{'非负' if allow_zero else '正'}数")
    return result


def required_text(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"缺少 {key}")
    return value.strip()


def clean_observation(study, sample):
    if sample.get("reviewed") is not True:
        raise ValueError("尚未原文复核")
    if sample.get("coating_scope") != "as_prepared_peo":
        raise ValueError("仅纳入未后处理的 PEO/MAO 涂层组")
    if sample.get("target_origin") not in {"reported_text", "reported_table"}:
        raise ValueError("标签须为原文文字或表格直接报告的实测值")
    values = {**study.get("defaults", {}), **sample}
    doi = doi_id(required_text(study, "doi"))
    if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        raise ValueError("需要可审计的 DOI")
    url = required_text(study, "source_url")
    if urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
        raise ValueError("source_url 须为 HTTPS")
    thickness = values.get("thickness")
    label = quantity("thickness", thickness)
    row = {
        "study_group": required_text(study, "study_group") if "study_group" in study else doi, "doi": doi,
        "sample_id": required_text(values, "sample_id"), "source_url": url,
        "process_locator": required_text(values, "process_locator"),
        "thickness_locator": required_text(values, "thickness_locator"),
        "substrate_grade": required_text(values, "substrate_grade").upper(),
        "electrolyte_family": required_text(values, "electrolyte_family").casefold(),
        "control_mode": values.get("control_mode"), "pulse_mode": values.get("pulse_mode"),
        "duration_s": quantity("duration", values.get("duration")),
        TARGET: label, "measurement_method": required_text(values, "measurement_method"),
    }
    if thickness.get("spread") is not None:
        row["spread_um"] = number(thickness["spread"]) * conversion("thickness", thickness["unit"])
        if not math.isfinite(row["spread_um"]) or row["spread_um"] < 0:
            raise ValueError("统计离散量须为有限非负数")
        row["spread_kind"] = required_text(thickness, "spread_kind")
    for field, destination in (("frequency", "frequency_hz"), ("duty_factor", "duty_pct")):
        if values.get(field) is not None:
            row[destination] = quantity(field, values[field])
    if row.get("duty_pct", 0) > 100:
        raise ValueError("占空比超过 100%")
    current = values.get("current_density")
    if current is not None:
        basis = {"setpoint": "current_setpoint_a_cm2", "peak": "current_peak_a_cm2",
                 "unspecified": "current_unspecified_basis_a_cm2"}.get(current.get("basis"))
        if not basis:
            raise ValueError("电流密度须区分设定值、峰值与原文未说明的口径")
        row[basis] = quantity("current_density", current)
    components = values.get("electrolyte_components", {})
    if not isinstance(components, dict):
        raise ValueError("电解液组分须为名称与带单位浓度的映射")
    row["electrolyte_components"] = {
        name: quantity("concentration", concentration, allow_zero=True)
        for name, concentration in sorted(components.items())
    }
    return row


def prepare(document):
    by_sample = defaultdict(list)
    rejected = []
    studies = document.get("studies") if isinstance(document, dict) else None
    if not isinstance(studies, list):
        raise ValueError("studies 须为研究数组")
    for study in studies:
        if not isinstance(study, dict) or not isinstance(study.get("samples"), list):
            raise ValueError("每项研究须包含 samples 数组")
        for sample in study["samples"]:
            if not isinstance(sample, dict):
                rejected.append({"doi": study.get("doi"), "sample_id": None, "reason": "试样记录须为对象"})
                continue
            try:
                row = clean_observation(study, sample)
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
                rejected.append({"doi": study.get("doi"), "sample_id": sample.get("sample_id"),
                                 "reason": str(error)})
                continue
            by_sample[row["study_group"], row["sample_id"]].append(row)

    clean = []
    for (_, sample_id), candidates in sorted(by_sample.items()):
        first = candidates[0]
        comparison = [*FEATURES, TARGET, "measurement_method", "electrolyte_components",
                      "spread_um", "spread_kind"]
        if any(any(row.get(key) != first.get(key) for key in comparison) for row in candidates[1:]):
            rejected.extend({"doi": row["doi"], "sample_id": sample_id, "reason": "同源试样记录相互冲突"}
                            for row in candidates)
            continue
        clean.append(first)
        rejected.extend({"doi": row["doi"], "sample_id": sample_id, "reason": "重复记录"}
                        for row in candidates[1:])
    return clean, rejected


def evaluate(rows):
    """Leave one study out. Refit both simple baselines within each training fold."""
    groups = sorted({row["study_group"] for row in rows})
    folds = []
    if len(groups) < 2:
        return {"status": "insufficient_studies", "folds": folds}
    for group in groups:
        train = [row for row in rows if row["study_group"] != group]
        test = [row for row in rows if row["study_group"] == group]
        center = median(row[TARGET] for row in train)
        x = [math.log(row["duration_s"]) for row in train]
        y = [row[TARGET] for row in train]
        x_bar, y_bar = mean(x), mean(y)
        variance = sum((value - x_bar) ** 2 for value in x)
        slope = sum((a - x_bar) * (b - y_bar) for a, b in zip(x, y)) / variance if variance else 0
        errors = [abs(row[TARGET] - max(0, y_bar + slope * (math.log(row["duration_s"]) - x_bar)))
                  for row in test]
        folds.append({
            "held_out_study": group, "train_rows": len(train), "test_rows": len(test),
            "train_groups": sorted({row["study_group"] for row in train}),
            "median_mae_um": mean(abs(row[TARGET] - center) for row in test),
            "log_duration_linear_mae_um": mean(errors),
        })
    return {
        "status": "ran", "protocol": "leave_one_study_out", "folds": folds,
        "macro_median_mae_um": mean(fold["median_mae_um"] for fold in folds),
        "macro_log_duration_linear_mae_um": mean(fold["log_duration_linear_mae_um"] for fold in folds),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reviewed", type=Path, help="带原文定位的核定实验记录 JSON")
    parser.add_argument("--out", type=Path, required=True, help="清洗版 CSV/JSON 的输出目录")
    args = parser.parse_args()
    try:
        document = json.loads(args.reviewed.read_text(encoding="utf-8"))
        rows, rejected = prepare(document)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"无法准备训练数据：{error}\n")
    args.out.mkdir(parents=True, exist_ok=True)
    component_names = sorted({name for row in rows for name in row["electrolyte_components"]})
    component_columns = ["electrolyte_g_l:" + name for name in component_names]
    for row in rows:
        row.update({"electrolyte_g_l:" + name: value for name, value in row["electrolyte_components"].items()})
    missing = {key: sum(row.get(key) is None for row in rows) for key in [*FEATURES, *component_columns]}
    features = [key for key, count in missing.items() if count < len(rows)]
    write_csv(args.out / "training.csv", rows, [*AUDIT_COLUMNS, *features, TARGET])
    write_json(args.out / "cleaning_report.json", {
        "target": TARGET, "features": features, "group_column": "study_group",
        "raw_rows": sum(len(study["samples"]) for study in document["studies"]),
        "clean_rows": len(rows), "rows_by_study": dict(Counter(row["study_group"] for row in rows)),
        "missing_by_feature": missing, "features_all_missing": sorted(set(missing) - set(features)),
        "rejected": rejected, "regression_smoke_test": evaluate(rows),
    })
    print(f"清洗后 {len(rows)} 行，{len({row['study_group'] for row in rows})} 个研究分组，排除 {len(rejected)} 行。")
    return 0 if rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
