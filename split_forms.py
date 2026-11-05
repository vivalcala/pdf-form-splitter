"""
PDF Form Splitter
=================

Split a multi-page PDF (digital or scanned) into one PDF per document and
extract its data to JSON, using a *profile* that describes the document type.

    python split_forms.py --profile profiles/invoice.json

A profile says:
  - which field identifies a document (the ID)
  - which fields to extract, each with a regex and an optional transform
  - how to name the output files
  - whether every page is a document ("per_page") or a document can span
    several pages until the next ID appears ("by_id")

New document type = new profile. The code does not change.

Folders (created on first run, relative to this script unless overridden):
  input_pdf/                 drop PDFs here
  input_pdf/NEEDS_REVIEW/    pages that could not be read
  processed_archive/         originals after processing
  output/                    one PDF per document  (--output to change)
  output/_data/              one JSON per document (written before the PDF)
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime

from pypdf import PdfReader, PdfWriter

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- text + OCR
_ocr_engine = None


def ocr_page(pdf_path, page_index):
    """OCR one page. The engine loads only the first time a scanned page appears."""
    global _ocr_engine
    import numpy as np
    import pypdfium2 as pdfium

    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR

        print("   (scanned page - reading with OCR, first time takes ~20 sec)")
        _ocr_engine = RapidOCR()
    image = pdfium.PdfDocument(pdf_path)[page_index].render(scale=2).to_pil().convert("RGB")
    result, _ = _ocr_engine(np.array(image))
    return "\n".join(r[1] for r in (result or []))


def page_text(reader, pdf_path, i, detect_regex):
    text = reader.pages[i].extract_text() or ""
    # Use the text layer when it has the ID or real content (e.g. page 2 of an invoice);
    # fall back to OCR only for pages that are essentially images.
    if re.search(detect_regex, text, re.IGNORECASE) or len(text.strip()) > 40:
        return text
    return ocr_page(pdf_path, i)


# ---------------------------------------------------------------- transforms
def squash(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def t_date_iso(value, fmt="%m/%d/%Y", **_):
    try:
        return datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def t_name_last_first(value, **_):
    """'DOE, JANE A' -> 'Jane A Doe'. Also re-spaces OCR-glued initials ('JaneA')."""
    def fix(given):
        given = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", given.strip())
        return " ".join(w.capitalize() if len(w) > 1 else w.upper() for w in given.split())

    if "," in value:
        last, given = value.split(",", 1)
        return f"{fix(given)} {' '.join(w.capitalize() for w in last.split())}".strip()
    return " ".join(w.capitalize() for w in value.split())


def t_title(value, **_):
    return " ".join(w.capitalize() for w in value.split())


def t_upper(value, **_):
    return value.strip().upper()


def t_money(value, **_):
    v = re.sub(r"[^0-9.\-]", "", value)
    try:
        return f"{float(v):.2f}"
    except ValueError:
        return ""


def t_pad_id(value, width=5, sep="-", **_):
    """'P2026-123' -> 'P2026-00123'"""
    if sep in value:
        head, num = value.upper().rsplit(sep, 1)
        return f"{head}{sep}{num.zfill(width)}"
    return value.upper()


def t_match_list(value, items=(), **_):
    """Snap OCR text to a known spelling ('RiversideGeneralHospital' -> 'Riverside General Hospital')."""
    for name in items:
        if squash(name) == squash(value):
            return name
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value.strip())


TRANSFORMS = {
    "date_iso": t_date_iso,
    "name_last_first": t_name_last_first,
    "title": t_title,
    "upper": t_upper,
    "money": t_money,
    "pad_id": t_pad_id,
    "match_list": t_match_list,
}


# ---------------------------------------------------------------- profile
def load_profile(path):
    with open(path, encoding="utf-8") as fh:
        p = json.load(fh)
    for key in ("name", "id_field", "fields", "filename"):
        if key not in p:
            raise ValueError(f"Profile {path} is missing '{key}'")
    p.setdefault("split_mode", "per_page")
    p.setdefault("required", [p["id_field"]])
    p["detect"] = p["fields"][p["id_field"]]["regex"]
    return p


def extract(text, profile):
    data = {}
    for field, spec in profile["fields"].items():
        m = re.search(spec["regex"], text, re.IGNORECASE | re.MULTILINE)
        if not m:
            continue
        value = (m.group(1) if m.groups() else m.group(0)).strip()
        transform = spec.get("transform")
        if transform:
            value = TRANSFORMS[transform](value, **spec.get("options", {}))
        if value:
            data[field] = value
    return data


def safe(text):
    return re.sub(r"[^A-Za-z0-9\-_ .]", "", str(text)).strip().replace(" ", "_")


def build_name(profile, data):
    return re.sub(r"\{(\w+)\}", lambda m: safe(data.get(m.group(1), "")), profile["filename"])


# ---------------------------------------------------------------- main
def run(profile, input_dir, output_dir):
    archive = os.path.join(os.path.dirname(input_dir), "processed_archive")
    review = os.path.join(input_dir, "NEEDS_REVIEW")
    data_dir = os.path.join(output_dir, "_data")
    for folder in (input_dir, archive, review, output_dir, data_dir):
        os.makedirs(folder, exist_ok=True)

    files = sorted(f for f in os.listdir(input_dir) if f.lower().endswith(".pdf"))
    if not files:
        print(f"No PDF found in {input_dir}. Drop PDFs there and run again.")
        return 0, 0

    print(f"Profile: {profile['name']}  (mode: {profile['split_mode']})")
    done = review_count = 0

    for name in files:
        pdf_path = os.path.join(input_dir, name)
        reader = PdfReader(pdf_path)
        print(f"\n{name}  ({len(reader.pages)} pages)")

        # 1. read every page and group pages into documents
        docs, current = [], None
        for i in range(len(reader.pages)):
            data = extract(page_text(reader, pdf_path, i, profile["detect"]), profile)
            starts_new = profile["id_field"] in data
            if profile["split_mode"] == "per_page" or starts_new or current is None:
                current = {"pages": [i], "data": data}
                docs.append(current)
            else:  # by_id: a page without an ID continues the current document
                current["pages"].append(i)
                for k, v in data.items():
                    current["data"].setdefault(k, v)

        # 2. write each document, or send it to review
        for doc in docs:
            writer = PdfWriter()
            for i in doc["pages"]:
                writer.add_page(reader.pages[i])
            data = doc["data"]
            first, last = doc["pages"][0] + 1, doc["pages"][-1] + 1
            pages_label = str(first) if first == last else f"{first}-{last}"
            missing = [f for f in profile["required"] if not data.get(f)]

            if missing:
                out = os.path.join(review, f"{os.path.splitext(name)[0]}_p{pages_label}.pdf")
                with open(out, "wb") as fh:
                    writer.write(fh)
                print(f"   page(s) {pages_label}: missing {', '.join(missing)} -> NEEDS_REVIEW")
                review_count += 1
                continue

            base = build_name(profile, data)
            data.update({"file": base + ".pdf", "sourceFile": name,
                         "pages": [i + 1 for i in doc["pages"]], "profile": profile["name"]})
            with open(os.path.join(data_dir, base + ".json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            with open(os.path.join(output_dir, base + ".pdf"), "wb") as fh:
                writer.write(fh)
            print(f"   page(s) {pages_label}: {base}.pdf")
            done += 1

        shutil.move(pdf_path, os.path.join(archive, name))

    print(f"\nDone: {done} document(s) saved, {review_count} need review.")
    return done, review_count


def main():
    ap = argparse.ArgumentParser(description="Split PDFs into documents and extract data by profile.")
    ap.add_argument("--profile", required=True, help="path to a profile JSON")
    ap.add_argument("--input", default=os.path.join(HERE, "input_pdf"))
    ap.add_argument("--output", default=os.environ.get("FORM_OUTPUT_FOLDER", os.path.join(HERE, "output")))
    args = ap.parse_args()
    run(load_profile(args.profile), args.input, args.output)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # keep the window open so the error can be read
        print("\nERROR:", exc)
    if sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close...")
