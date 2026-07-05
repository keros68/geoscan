from __future__ import annotations

import argparse
import csv
import html
import os
from pathlib import Path
from typing import Iterable


HIGH_KEYWORDS = (
    "比例尺",
    "图例",
    "矿床",
    "勘探线",
    "剖面",
    "平面",
    "实测",
    "图号",
    "资料",
    "日期",
    "审核",
    "编图",
    "钻孔",
    "采样",
    "终孔",
    "标高",
    "座标",
    "坐标",
)


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _mostly_numeric(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    numeric_chars = sum(1 for char in stripped if char.isdigit() or char in ".:-+[]() /\\°")
    return numeric_chars / max(len(stripped), 1) >= 0.75


def classify_ocr_text(text: str, *, confidence: float) -> dict[str, str | int]:
    cleaned = text.strip()
    if not cleaned:
        return {"priority": "skip", "category": "empty", "score": 0}

    if any(keyword in cleaned for keyword in HIGH_KEYWORDS):
        return {"priority": "high", "category": "scale_title" if "比例尺" in cleaned else "map_text", "score": 300}

    if _mostly_numeric(cleaned):
        return {"priority": "low", "category": "number_table", "score": 50}

    if _has_chinese(cleaned) and len(cleaned) >= 2:
        return {"priority": "high" if confidence >= 0.7 else "medium", "category": "chinese_text", "score": 220}

    if _has_chinese(cleaned):
        return {"priority": "medium", "category": "single_chinese", "score": 120}

    return {"priority": "low", "category": "mixed_or_unclear", "score": 40}


def prepare_review_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    prepared = []
    for row in rows:
        confidence = float(row.get("ocr_confidence") or 0)
        label = classify_ocr_text(row.get("review_text") or row.get("ocr_text", ""), confidence=confidence)
        item = dict(row)
        item["priority"] = str(label["priority"])
        item["category"] = str(label["category"])
        item["priority_score"] = str(int(label["score"]) + int(confidence * 100))
        item["suggested_action"] = _suggested_action(item)
        prepared.append(item)
    return sorted(
        prepared,
        key=lambda item: (
            -int(item["priority_score"]),
            item.get("crop_path", ""),
        ),
    )


def load_review_corrections(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return {
            str(row.get("crop_path", "")).strip(): {
                "review_text": str(row.get("review_text", "")).strip(),
                "review_note": str(row.get("review_note", "")).strip(),
            }
            for row in csv.DictReader(handle)
            if str(row.get("crop_path", "")).strip()
        }


def apply_review_corrections(
    rows: Iterable[dict[str, str]],
    corrections: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    corrected = []
    for row in rows:
        item = dict(row)
        correction = corrections.get(str(item.get("crop_path", "")))
        if correction:
            item["review_text"] = correction.get("review_text", "")
            item["review_status"] = "manual_corrected" if item["review_text"] else "manual_rejected"
            item["review_note"] = correction.get("review_note", "")
        corrected.append(item)
    return corrected


def _suggested_action(row: dict[str, str]) -> str:
    if row["priority"] == "high":
        return "优先复核"
    if row["priority"] == "medium":
        return "可复核"
    if row["priority"] == "low":
        return "低优先级"
    return "跳过"


def read_review_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_priority_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "priority",
        "category",
        "suggested_action",
        "crop_path",
        "ocr_text",
        "review_text",
        "ocr_confidence",
        "checked",
        "review_status",
        "review_note",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_review_html(rows: list[dict[str, str]], path: Path, *, crop_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = {name: sum(1 for row in rows if row["priority"] == name) for name in ["high", "medium", "low", "skip"]}
    body = "\n".join(_render_row(row, crop_dir=crop_dir, base_dir=path.parent) for row in rows)
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OCR文字复核</title>
  <style>
    :root {{
      --ink: #1d2528;
      --muted: #667174;
      --line: #d8dfdc;
      --paper: #f8faf7;
      --accent: #17705f;
      --warn: #9a4d00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      letter-spacing: 0;
    }}
    header {{
      padding: 28px 32px 18px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 24px;
      font-weight: 650;
    }}
    .summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 18px;
      color: var(--muted);
      font-size: 14px;
    }}
    .summary strong {{ color: var(--ink); }}
    main {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 20px 24px 48px;
    }}
    .row {{
      display: grid;
      grid-template-columns: 250px minmax(0, 1fr) 130px;
      gap: 18px;
      align-items: center;
      padding: 16px 0;
      border-bottom: 1px solid var(--line);
    }}
    .crop {{
      width: 250px;
      min-height: 62px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #fff;
      border: 1px solid var(--line);
      overflow: hidden;
    }}
    .crop img {{
      max-width: 100%;
      max-height: 120px;
      image-rendering: auto;
    }}
    .text {{
      font-size: 20px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .meta {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
    }}
    .status {{
      text-align: right;
      font-size: 14px;
      color: var(--muted);
    }}
    .pill {{
      display: inline-block;
      padding: 4px 8px;
      border-radius: 999px;
      color: #fff;
      background: var(--accent);
      font-size: 12px;
      margin-bottom: 8px;
    }}
    .medium .pill {{ background: #596b8f; }}
    .low .pill {{ background: var(--warn); }}
    .skip .pill {{ background: #777; }}
    @media (max-width: 820px) {{
      header {{ position: static; padding: 22px 18px 14px; }}
      main {{ padding: 12px 16px 32px; }}
      .row {{ grid-template-columns: 1fr; gap: 10px; }}
      .crop {{ width: 100%; min-height: 56px; }}
      .status {{ text-align: left; }}
      .text {{ font-size: 18px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>OCR文字复核</h1>
    <div class="summary">
      <span>总候选 <strong>{len(rows)}</strong></span>
      <span>优先看 <strong>{counts["high"]}</strong></span>
      <span>可复核 <strong>{counts["medium"]}</strong></span>
      <span>低优先级 <strong>{counts["low"]}</strong></span>
    </div>
  </header>
  <main>
    {body}
  </main>
</body>
</html>
"""
    path.write_text(html_text, encoding="utf-8")


def _render_row(row: dict[str, str], *, crop_dir: Path, base_dir: Path) -> str:
    crop_path = crop_dir / row.get("crop_path", "")
    label = row.get("review_text") or row.get("ocr_text", "")
    raw_text = row.get("ocr_text", "")
    raw_meta = ""
    if row.get("review_text") and row.get("review_text") != raw_text:
        raw_meta = f"<span>原OCR {html.escape(raw_text)}</span>"
    relative_crop = html.escape(os.path.relpath(crop_path, start=base_dir).replace("\\", "/"))
    priority = html.escape(row.get("priority", ""))
    confidence = html.escape(row.get("ocr_confidence", ""))
    category = html.escape(row.get("category", ""))
    action = html.escape(row.get("suggested_action", ""))
    crop_name = html.escape(row.get("crop_path", ""))
    return f"""<section class="row {priority}">
  <div class="crop"><img src="{relative_crop}" alt="{crop_name}"></div>
  <div>
    <div class="text">{html.escape(label)}</div>
    <div class="meta">
      <span>{crop_name}</span>
      <span>置信度 {confidence}</span>
      <span>{category}</span>
      {raw_meta}
    </div>
  </div>
  <div class="status"><span class="pill">{action}</span><br>checked=no</div>
</section>"""


def build_review_page(
    input_csv: Path,
    crop_dir: Path,
    output_html: Path,
    output_csv: Path,
    *,
    corrections_csv: Path | None = None,
) -> dict[str, int | str]:
    corrections = load_review_corrections(corrections_csv) if corrections_csv is not None else {}
    rows = prepare_review_rows(apply_review_corrections(read_review_csv(input_csv), corrections))
    write_priority_csv(rows, output_csv)
    write_review_html(rows, output_html, crop_dir=crop_dir)
    return {
        "input": str(input_csv),
        "html": str(output_html),
        "priority_csv": str(output_csv),
        "rows": len(rows),
        "high": sum(1 for row in rows if row["priority"] == "high"),
        "medium": sum(1 for row in rows if row["priority"] == "medium"),
        "low": sum(1 for row in rows if row["priority"] == "low"),
        "manual_corrections": len(corrections),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("crop_dir", type=Path)
    parser.add_argument("output_html", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("--corrections-csv", type=Path, default=None)
    args = parser.parse_args()
    report = build_review_page(
        args.input_csv,
        args.crop_dir,
        args.output_html,
        args.output_csv,
        corrections_csv=args.corrections_csv,
    )
    for key, value in report.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
