"""PDF text, evidence locations, raw tables, and caption-linked figure previews."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from pdfplumber.utils import cluster_objects
from pdfplumber.utils.exceptions import PdfminerException

import pdfplumber
import pypdfium2

from sources import doi_id


CAPTION = re.compile(
    r"^\s*(?P<kind>fig(?:ure)?\.?|table|图|表)\s*(?P<number>[Ss]?\d+[a-z]?)"
    r"\s*[.．:：、]?\s*(?P<caption>.*)", re.I,
)
DOI = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|(?<![/\w])doi\s*:\s*)(10\.\d{4,9}/[-.()/:\w]+)", re.I)


@dataclass
class TextRegion:
    page: int
    region: str
    text: str


def norm(text):
    return " ".join((text or "").split())


def pdf_identity(doc, path):
    title = doc.metadata.get("Title")
    title = norm(title) if isinstance(title, str) else ""
    if not doc.pages:
        return title or path.stem, ""
    first_page = doc.pages[0]
    for annotation in first_page.hyperlinks:
        url = annotation.get("uri") or ""
        if urlsplit(url).hostname in {"doi.org", "dx.doi.org"}:
            match = DOI.match(url)
            if match:
                return title or path.stem, doi_id(match.group(1))
    text = first_page.extract_text(x_tolerance=1) or ""
    match = DOI.search(text)
    return title or path.stem, doi_id(match.group(1)) if match else ""


def text_lines(page):
    lines = []
    for line in page.extract_text_lines(x_tolerance=1, y_tolerance=2, return_chars=False):
        if line["text"].strip():
            lines.append(line)
    return lines


def reading_regions(page, *, y_tolerance=2):
    """Read two columns separately when they share a stable central gutter."""
    words = page.extract_words(x_tolerance=1, y_tolerance=y_tolerance)
    if not words:
        return []
    left, top, right, bottom = page.bbox
    mid = (left + right) / 2
    body = [w for w in words if top + 40 < w["top"] < bottom - 35]
    crossing = [w for w in body if w["x0"] < mid + 4 and w["x1"] > mid - 4]
    if len(body) > 100 and len(crossing) < len(body) * .012:
        left_count = sum(w["x1"] <= mid - 4 for w in body)
        right_count = sum(w["x0"] >= mid + 4 for w in body)
        if min(left_count, right_count) > 35:
            left_box = (left, top, mid, bottom)
            right_box = (mid, top, right, bottom)
            return [
                TextRegion(page.page_number, label,
                           page.crop(box).extract_text(x_tolerance=1, y_tolerance=y_tolerance) or "")
                for label, box in (("left", left_box), ("right", right_box))
            ]
    return [TextRegion(page.page_number, "full", page.extract_text(x_tolerance=1, y_tolerance=y_tolerance) or "")]


def captions(page, lines):
    found = []
    for line in lines:
        match = CAPTION.match(line["text"])
        if not match:
            continue
        # Inline references usually continue with verbs such as “shows”.
        body = match["caption"]
        if re.match(r"^(?:shows?|illustrates?|presents?|is|are|in|and)\b", body, re.I):
            continue
        kind = "table" if match["kind"].lower() in ("table", "表") else "figure"
        label = ("Table " if kind == "table" else "Fig. ") + match["number"]
        chunks = [line["text"]]
        box = [line[k] for k in ("x0", "top", "x1", "bottom")]
        page_left, _, page_right, page_bottom = page.bbox
        middle = (page_left + page_right) / 2
        if box[2] <= middle + 8:
            clip_left, clip_right = page_left, middle + 4
        elif box[0] >= middle - 8:
            clip_left, clip_right = max(middle - 4, box[0] - 12), page_right
        else:
            clip_left, clip_right = page_left, page_right
        clipped = page.crop((clip_left, box[1], clip_right, min(page_bottom, box[1] + 153)))
        following = sorted(
            (nxt for nxt in text_lines(clipped) if 3 < nxt["top"] - line["top"]
             and abs(nxt["x0"] - box[0]) < 24), key=lambda nxt: nxt["top"]
        )
        for nxt in following[:12]:
            if nxt["top"] < box[3] - 1:
                continue
            if nxt["top"] - box[3] > 6 or CAPTION.match(nxt["text"]):
                break
            chunks.append(nxt["text"])
            box[2] = max(box[2], nxt["x1"])
            box[3] = nxt["bottom"]
        found.append({"kind": kind, "label": label, "caption": norm(" ".join(chunks)), "bbox": box})
    return found


def figure_band(page, cap, cap_list):
    """Find a nearby band of image/vector objects; fall back to page preview."""
    left, top, right, bottom = page.bbox
    x0, cap_top, x1, cap_bottom = cap["bbox"]
    width = right - left
    mid = (left + right) / 2
    if x0 > mid + .15 * width:
        return None  # Side caption: the visual may occupy the opposite column.
    if x1 < mid + 12:
        scope = (left, mid + 10)
    elif x0 > mid - 12:
        scope = (mid - 10, right)
    else:
        scope = (left, right)
    previous = [
        c["bbox"][3] for c in cap_list
        if c["bbox"][3] < cap_top - 4 and c["bbox"][2] > scope[0] and c["bbox"][0] < scope[1]
    ]
    floor = max([top + 25, cap_top - 510, *previous])
    boxes = []
    for obj in page.images + page.curves + page.rects + page.lines:
        a, b, c, d = [obj[k] for k in ("x0", "top", "x1", "bottom")]
        if c - a > .99 * width and d - b < 4:
            continue
        if c - a < .5 and d - b < .5:
            continue
        if a < scope[0] - 8 or c > scope[1] + 8:
            continue
        if b >= floor and d <= cap_top + 1:
            boxes.append((a, b, c, d))
    if not boxes:
        return None
    boxes.sort(key=lambda box: box[3], reverse=True)
    if cap_top - boxes[0][3] > 70:
        return None
    selected = [boxes[0]]
    band_top = boxes[0][1]
    for box in boxes[1:]:
        if band_top - box[3] > 28:
            break
        selected.append(box)
        band_top = min(band_top, box[1])
    if cap_top - band_top < 28:
        return None
    band_left = min(min(box[0] for box in selected), x0)
    band_right = max(max(box[2] for box in selected), x1)
    return (
        max(left, band_left - 8), max(top, band_top - 8),
        min(right, band_right + 8), min(bottom, cap_bottom + 5),
    )


def words_match(text, keywords):
    lowered = text.casefold()
    for keyword in keywords:
        key = keyword.casefold()
        if re.fullmatch(r"[a-z0-9]+", key):
            if re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", lowered):
                return True
        elif key in lowered:
            return True
    return False


def export_visuals(page, cap_list, render, config, folder, paper_id, result, *, mapped=False):
    figure_types = config["outputs"]["figure_types"]
    keywords = config["figure_keywords"]
    labels = config["outputs"]["figure_keywords"]
    figures = [c for c in cap_list if c["kind"] == "figure"]
    for index, cap in enumerate(figures, 1):
        box = None if mapped else figure_band(page, cap, cap_list)
        nearby = ""
        if box:
            nearby = page.crop(box).extract_text(x_tolerance=1, y_tolerance=2) or ""
        categories = [
            name for name, aliases in keywords.items()
            if words_match(cap["caption"] + "\n" + nearby, aliases)
        ] or ["other"]
        if "all" not in figure_types and not any(t in figure_types for t in categories):
            continue
        if labels and not words_match(cap["caption"] + "\n" + nearby, labels):
            continue
        label = re.sub(r"[^A-Za-z0-9_-]", "_", cap["label"]).strip("_")
        path = folder / f"{paper_id}_p{page.page_number:03d}_{label}_{index}.png"
        render(box, path)
        result.append({
            **cap, "paper_id": paper_id, "page": page.page_number, "categories": categories,
            "file": f"figures/{path.name}", "crop_bbox": box,
            "status": "estimated_crop_review" if box else "full_page_review",
        })


def word_groups(words, gap=14):
    groups = []
    for word in sorted(words, key=lambda w: w["x0"]):
        if not groups or word["x0"] - groups[-1][-1]["x1"] >= gap:
            groups.append([])
        groups[-1].append(word)
    return groups


def horizontal_table(page, caption):
    """Read a borderless-column table bounded by two aligned horizontal rules."""
    x, _, _, caption_bottom = caption["bbox"]
    lines = sorted(
        [line for line in page.horizontal_edges if line["width"] >= 105
         and line["x0"] - 15 <= x <= line["x1"] - 25 and caption_bottom < line["top"]],
        key=lambda line: line["top"],
    )
    if len(lines) < 2 or lines[0]["top"] - caption_bottom > 55:
        return None
    first = lines[0]
    last = next(
        (line for line in lines[1:] if 28 < line["top"] - first["top"] < 460
         and abs(first["x0"] - line["x0"]) < 12 and abs(first["x1"] - line["x1"]) < 12),
        None,
    )
    if not last:
        return None
    page_left, _, page_right, _ = page.bbox
    x0, x1 = max(page_left, first["x0"] - 2), min(page_right, first["x1"] + 2)
    header = page.crop((x0, caption_bottom + .5, x1, first["top"])).extract_words(x_tolerance=1)
    body = page.crop((x0, first["top"], x1, last["top"])).extract_words(x_tolerance=1)
    header_lines = cluster_objects(header, "top", 3)
    body_lines = cluster_objects(body, "top", 3)
    if not header_lines or len(body_lines) < 2:
        return None
    count = len(word_groups(header_lines[-1]))
    if not 2 <= count <= 15:
        return None
    alignment = next((word_groups(words) for words in body_lines[:4] if len(word_groups(words)) == count), None)
    if not alignment:
        return None
    boundaries = [
        (before[-1]["x1"] + after[0]["x0"]) / 2
        for before, after in zip(alignment, alignment[1:])
    ]

    def cells(words):
        cells_for_line = [[] for _ in range(count)]
        for word in sorted(words, key=lambda item: item["x0"]):
            index = sum(word["x0"] >= edge for edge in boundaries)
            cells_for_line[index].append(word["text"])
        return [" ".join(items) for items in cells_for_line]

    header_cells = [""] * count
    for line in header_lines:
        header_cells = [norm(a + " " + b) for a, b in zip(header_cells, cells(line))]
    rows = [header_cells]
    for words in body_lines:
        line = cells(words)
        if len(rows) > 1 and not line[0]:
            rows[-1] = [norm(a + " " + b) for a, b in zip(rows[-1], line)]
        elif any(line):
            rows.append(line)
    return (x0, caption_bottom, x1, last["top"]), rows


def table_matches(caption, rows, keywords):
    return not keywords or words_match(
        caption + " " + " ".join(value for row in rows for value in row), keywords
    )


def export_tables(page, cap_list, warn_keywords, paper_id, result, warn):
    candidates = []
    table_captions = [c for c in cap_list if c["kind"] == "table"]
    for table in page.find_tables():
        rows = [[norm(value) if value is not None else "" for value in row]
                for row in table.extract(x_tolerance=1, y_tolerance=2)]
        rows = [row for row in rows if any(row)]
        eligible = [
            c for c in table_captions if -3 <= table.bbox[1] - c["bbox"][3] <= 100
            and c["bbox"][0] < table.bbox[2] and c["bbox"][2] > table.bbox[0]
        ]
        caption = max(eligible, key=lambda c: c["bbox"][3]) if eligible else {}
        candidates.append((table.bbox, rows, caption, "grid"))
    for caption in table_captions:
        if not any(c is caption for _, _, c, _ in candidates):
            bounded = horizontal_table(page, caption)
            if bounded:
                box, rows = bounded
                candidates.append((box, rows, caption, "horizontal_rules"))
            elif table_matches(caption["caption"], [], warn_keywords):
                warn(f'{paper_id} 第 {page.page_number} 页 {caption["label"]} 未能识别表格边界。')
    for index, (box, rows, caption, method) in enumerate(candidates, 1):
        if len(rows) < 2 or max(sum(bool(v) for v in row) for row in rows) < 2:
            continue
        result.append({
            "paper_id": paper_id, "page": page.page_number,
            "table_id": f"{paper_id}_p{page.page_number:03d}_t{index}",
            "label": caption.get("label", ""), "caption": caption.get("caption", ""),
            "bbox": box, "rows": rows, "method": method, "status": "layout_review",
        })


def mapped_table(page, spec, paper_id, index):
    xs, ys = spec["column_edges"], spec["row_edges"]
    rows = [spec["columns"]]
    for top, bottom in zip(ys, ys[1:]):
        row = [norm(page.crop((left, top, right, bottom)).extract_text(x_tolerance=1))
               for left, right in zip(xs, xs[1:])]
        if not any(row):
            raise ValueError(f'{spec["label"]} 的指定行未找到文本')
        rows.append(row)
    return {
        "paper_id": paper_id, "page": page.page_number,
        "table_id": f"{paper_id}_p{page.page_number:03d}_t{index}",
        "label": spec["label"], "caption": spec["caption"], "rows": rows,
        "bbox": [xs[0], ys[0], xs[-1], ys[-1]], "method": "configured_cells", "status": "layout_review",
    }


def inspect_pdf(path, record, config, output, warn):
    figures, tables, regions = [], [], []
    want_figures = bool(config["outputs"]["figure_types"])
    extract_tables = any(field.get("table_pattern") or (
        config["model"]["mode"] == "ollama" and field.get("use_model", True)
    ) for field in config["fields"])
    want_tables = config["outputs"]["tables"] or extract_tables
    warn_keywords = [] if extract_tables else config["outputs"]["table_keywords"]
    layout = config.get("pdf_layout", {})
    if layout and layout["sha256"] != record.get("sha256"):
        raise ValueError("当前 PDF 与配置中的 pdf_layout.sha256 不匹配")
    renderer = pypdfium2.PdfDocument(path) if want_figures else None
    try:
        with pdfplumber.open(path) as doc:
            total = len(doc.pages)
            pages = doc.pages[:config["max_pages"]]
            mapped_pages = {*layout.get("text_pages", [])}
            mapped_pages.update(item["page"] for key in ("tables", "figures") for item in layout.get(key, []))
            mapped_pages.update(number for item in layout.get("figures", []) for number in item["caption_pages"])
            if mapped_pages and max(mapped_pages) > len(pages):
                raise ValueError("pdf_layout 指向超出本次 PDF 读取范围的页码")
            text_pages = 0
            for page in pages:
                try:
                    def render(box, destination):
                        source_page = renderer[page.page_number - 1]
                        bitmap = source_page.render(scale=2.5)
                        try:
                            img = bitmap.to_pil()
                            if box:
                                left, top, right, bottom = page.bbox
                                sx, sy = img.width / (right - left), img.height / (bottom - top)
                                img = img.crop(tuple(round(v) for v in (
                                    (box[0] - left) * sx, (box[1] - top) * sy,
                                    (box[2] - left) * sx, (box[3] - top) * sy,
                                )))
                            img.save(destination)
                        finally:
                            bitmap.close()
                            source_page.close()

                    page_regions = reading_regions(page, y_tolerance=layout.get("text_y_tolerance", 2))
                    if "text_pages" not in layout or page.page_number in layout["text_pages"]:
                        regions.extend(page_regions)
                    text_pages += any(region.text.strip() for region in page_regions)
                    if not page_regions:
                        warn(f'{record["paper_id"]} 第 {page.page_number} 页无可抽取文本，可能需要 OCR。')
                    auto_figures = want_figures and "figures" not in layout
                    auto_tables = want_tables and "tables" not in layout
                    cap_list = captions(page, text_lines(page)) if auto_figures or auto_tables else []
                    if want_figures:
                        figure_caps = cap_list
                        if "figures" in layout:
                            figure_caps = [{**item, "kind": "figure", "bbox": None}
                                           for item in layout["figures"] if item["page"] == page.page_number]
                        export_visuals(page, figure_caps, render, config, output / "figures",
                                       record["paper_id"], figures, mapped=not auto_figures)
                    if auto_tables:
                        export_tables(page, cap_list, warn_keywords, record["paper_id"], tables, warn)
                    elif want_tables:
                        specs = [item for item in layout["tables"] if item["page"] == page.page_number]
                        for index, spec in enumerate(specs, 1):
                            tables.append(mapped_table(page, spec, record["paper_id"], index))
                except (OSError, ValueError, RuntimeError, PdfminerException) as error:
                    warn(f'{record["paper_id"]} 第 {page.page_number} 页读取或导出失败：{error}')
                finally:
                    page.close()
            record["total_pages"] = total
            record["scanned_pages"] = len(pages)
            record["text_pages"] = text_pages
            record["read_status"] = (
                "readable_text" if record["text_pages"] == len(pages) else
                "partial_text" if record["text_pages"] else "no_readable_text"
            )
            if len(pages) < total:
                warn(f'{record["paper_id"]} 读取了前 {len(pages)} / {total} 页。')
    finally:
        if renderer:
            renderer.close()
    return regions, figures, tables
