"""Download, document-map, and consolidate CMS Medicare fee schedule data."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


APPROVED_URLS = {
    "https://www.cms.gov/medicare/payment/fee-schedules/clinical-laboratory-fee-schedule-clfs/files":
        "CLFS",
    "https://www.cms.gov/medicare/payment/fee-schedules/physician/national-payment-amount-file":
        "Physician",
    "https://www.cms.gov/medicare/payment/fee-schedules/dmepos/dmepos-fee-schedule":
        "DMEPOS",
}

DMEPOS_KEY_FIELDS = (
    "HCPCS_CODE",
    "MODIFIER",
    "JURISDICTION",
    "CATEGORY",
    "REGION",
    "STATE",
)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloaded_files"
OUTPUT_DIR = BASE_DIR / "output"
CACHE_DIR = BASE_DIR / "cache"
REQUEST_TIMEOUT = 60
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Medicare-Fee-Schedule-Downloader/1.0"})


@dataclass
class FieldSpec:
    name: str
    start: int
    length: int


@dataclass
class Release:
    name: str
    url: str
    year: int
    period: int
    sort_key: tuple[int, int]


@dataclass
class PdfSchema:
    pdf_name: str
    text_sha256: str
    effective_period: str
    data_type: str
    fields: list[FieldSpec]
    delimited_columns: list[str]


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip()[:180] or "download"


def download_file(url: str, destination: Path) -> Optional[Path]:
    """Download a file, preserving it on disk and retrying once."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            with SESSION.get(url, stream=True, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()
                with destination.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            output.write(chunk)
            return destination
        except requests.RequestException as exc:
            print(f"Network error downloading {url}: {exc}")
            if attempt == 0:
                continue
    return None


def fetch_html(url: str) -> BeautifulSoup:
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def extract_archives(archive_path: Path, extract_root: Path) -> list[Path]:
    """Extract an archive and nested archives; return all extracted files."""
    extract_root.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    try:
        with zipfile.ZipFile(archive_path) as outer:
            bad_member = outer.testzip()
            if bad_member:
                raise zipfile.BadZipFile(f"CRC error for {bad_member}")
            root_resolved = extract_root.resolve()
            for member in outer.infolist():
                destination = (extract_root / member.filename).resolve()
                if root_resolved not in destination.parents:
                    raise zipfile.BadZipFile(
                        f"Unsafe archive path: {member.filename}"
                    )
                if not member.is_dir():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with outer.open(member) as source, destination.open("wb") as target:
                        while chunk := source.read(1024 * 1024):
                            target.write(chunk)
    except (zipfile.BadZipFile, OSError) as exc:
        print(f"Invalid ZIP {archive_path.name}: {exc}")
        return extracted

    for item in sorted(extract_root.rglob("*")):
        if item.is_file():
            extracted.append(item)

    for nested in sorted(extract_root.rglob("*"), key=lambda p: str(p).lower()):
        if nested.is_file() and nested.suffix.lower() == ".zip":
            nested_root = nested.parent / ("_nested_" + nested.stem)
            nested_extracted = extract_archives(nested, nested_root)
            extracted.extend(nested_extracted)
    return sorted(set(extracted), key=lambda p: str(p).lower())


def discover_releases(schedule: str, page_url: str) -> list[Release]:
    """Discover CMS release detail pages and sort them oldest to newest."""
    soup = fetch_html(page_url)
    releases: list[Release] = []

    if schedule == "CLFS":
        pattern = re.compile(r"(\d{2})CLAB(?:Q([1-4]))?", re.I)
        for link in soup.find_all("a", href=True):
            text = html.unescape(link.get_text(" ", strip=True))
            match = pattern.search(text) or pattern.search(link["href"])
            if not match:
                continue
            year = 2000 + int(match.group(1))
            period = int(match.group(2) or 1)
            url = urljoin(page_url, link["href"])
            releases.append(Release(match.group(0).upper(), url, year, period,
                                    (year, period)))

    elif schedule == "Physician":
        pattern = re.compile(r"PF(?:REV|ALL)(\d{2})([A-Z])", re.I)
        for link in soup.find_all("a", href=True):
            text = html.unescape(link.get_text(" ", strip=True))
            match = pattern.search(text) or pattern.search(link["href"])
            if not match:
                continue
            year = 2000 + int(match.group(1))
            letter = match.group(2).upper()
            period = ord(letter) - ord("A") + 1
            url = urljoin(page_url, link["href"])
            releases.append(Release(match.group(0).upper(), url, year, period,
                                    (year, period)))

    else:
        pattern = re.compile(r"DME(?:-)?(\d{2})-([A-D])", re.I)
        for link in soup.find_all("a", href=True):
            text = html.unescape(link.get_text(" ", strip=True))
            match = pattern.search(text) or pattern.search(link["href"])
            if not match:
                continue
            year = 2000 + int(match.group(1))
            letter = match.group(2).upper()
            period = ord(letter) - ord("A") + 1
            url = urljoin(page_url, link["href"])
            releases.append(Release(match.group(0).upper(), url, year, period,
                                    (year, period)))

    unique: dict[str, Release] = {}
    for release in releases:
        key = release.url.lower()
        if key not in unique or release.sort_key > unique[key].sort_key:
            unique[key] = release
    ordered = sorted(unique.values(), key=lambda r: r.sort_key)
    return ordered[-12:]


def pdf_text(pdf_path: Path) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is required: pip install pypdf")
    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def infer_effective_period(text: str, release_name: str) -> str:
    month_pattern = (
        r"\b(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)"
    )
    month_match = re.search(
        month_pattern + r"\s+(?:\d{1,2}(?:st|nd|rd|th)?[, ]+\s*)?(20\d{2})\b",
        text,
        re.I,
    )
    if month_match:
        return f"{month_match.group(1).title()} {month_match.group(2)}"
    release_match = re.search(r"(20\d{2}).*?([A-D])", release_name, re.I)
    if release_match:
        year = release_match.group(1)
        quarter_month = {"A": "January", "B": "April", "C": "July", "D": "October"}
        letter = release_match.group(2).upper()
        return f"{quarter_month[letter]} {year}"
    return "Not identified"


def infer_data_type(pdf_name: str, text: str) -> str:
    combined = f"{pdf_name} {text}".lower()
    data_types = (
        ("fee schedule", "fee schedule"),
        ("payment", "payment"),
        ("rural", "rural"),
        ("carrier", "carrier"),
        ("jurisdiction", "jurisdiction"),
        ("national", "national"),
        ("supplier", "supplier"),
    )
    for needle, label in data_types:
        if needle in combined:
            return label
    return "Not identified"


def parse_pdf_schema(pdf_path: Path, text: str) -> PdfSchema:
    fields: list[FieldSpec] = []
    delimited_columns: list[str] = []
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        match = re.match(
            r"^([A-Za-z][A-Za-z0-9 /_-]{1,70}?)\s+(\d{1,4})-(\d{1,4})"
            r"(?:\s|$)",
            line,
            re.I,
        )
        if match:
            name = match.group(1).strip(" .:")
            key = normalize_label(name)
            start = int(match.group(2))
            end = int(match.group(3))
            if key not in seen and not key.startswith("filler"):
                fields.append(FieldSpec(name=name, start=start, length=end-start+1))
                seen.add(key)
            continue

        match = re.match(
            r"^\d+[^A-Za-z0-9]+\s*([A-Za-z][A-Za-z0-9 /_-]{1,70}?)\s+"
            r"(\d{1,4})-(\d{1,4})(?:\s|$)",
            line,
            re.I,
        )
        if match:
            name = match.group(1).strip(" .:")
            key = normalize_label(name)
            start = int(match.group(2))
            end = int(match.group(3))
            if key not in seen and not key.startswith("filler"):
                fields.append(FieldSpec(name=name, start=start, length=end-start+1))
                seen.add(key)

    # Use explicit column tables when no fixed-width layout is documented.
    if not fields:
        in_column_table = False
        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if re.search(r"\bcolumn name\b|\bfield name\b", line, re.I):
                in_column_table = True
                continue
            if in_column_table:
                match = re.match(r"^(?:\d+\s+)?([A-Za-z][A-Za-z0-9 /_-]{1,70})$", line)
                if not match:
                    if delimited_columns:
                        break
                    continue
                name = match.group(1).strip()
                key = normalize_label(name)
                if key not in seen and not key.startswith(("filler", "comment")):
                    delimited_columns.append(name)
                    seen.add(key)

    return PdfSchema(
        pdf_name=pdf_path.name,
        text_sha256=sha256_file(pdf_path),
        effective_period=infer_effective_period(text, pdf_path.stem),
        data_type=infer_data_type(pdf_path.name, text),
        fields=sorted(fields, key=lambda field: field.start),
        delimited_columns=delimited_columns,
    )


def load_cached_dmepos_pdf_schemas(
    pdf_paths: Iterable[Path], release: Release
) -> tuple[dict[Path, PdfSchema], dict[Path, str]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "dmepos_pdf_cache.json"
    cache: dict[str, dict] = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  Could not read DMEPOS PDF cache: {exc}")

    schemas: dict[Path, PdfSchema] = {}
    texts: dict[Path, str] = {}
    changed = False
    for pdf in pdf_paths:
        if not pdf.is_file():
            continue
        try:
            file_hash = sha256_file(pdf)
            cached = cache.get(file_hash)
            if cached:
                texts[pdf] = cached.get("text", "")
                fields = [
                    FieldSpec(
                        name=item["name"],
                        start=int(item["start"]),
                        length=int(item["length"]),
                    )
                    for item in cached.get("fields", [])
                ]
                delimited_columns = cached.get("delimited_columns", [])
                if not fields and texts[pdf]:
                    schema = parse_pdf_schema(pdf, texts[pdf])
                    fields = schema.fields
                    delimited_columns = schema.delimited_columns
                    cache[file_hash] = {
                        "pdf_name": schema.pdf_name,
                        "text": texts[pdf],
                        "effective_period": schema.effective_period,
                        "data_type": schema.data_type,
                        "fields": [
                            {
                                "name": field.name,
                                "start": field.start,
                                "length": field.length,
                            }
                            for field in schema.fields
                        ],
                        "delimited_columns": schema.delimited_columns,
                    }
                    changed = True
                    effective_period = schema.effective_period
                    data_type = schema.data_type
                else:
                    effective_period = cached.get("effective_period", "Not identified")
                    data_type = cached.get("data_type", "Not identified")
                schemas[pdf] = PdfSchema(
                    pdf_name=cached.get("pdf_name", pdf.name),
                    text_sha256=file_hash,
                    effective_period=effective_period,
                    data_type=data_type,
                    fields=fields,
                    delimited_columns=delimited_columns,
                )
                print(f"  Cached PDF text/schema: {pdf.name}")
            else:
                text = pdf_text(pdf)
                texts[pdf] = text
                schema = parse_pdf_schema(pdf, text)
                schemas[pdf] = schema
                cache[file_hash] = {
                    "pdf_name": schema.pdf_name,
                    "text": text,
                    "effective_period": schema.effective_period,
                    "data_type": schema.data_type,
                    "fields": [
                        {
                            "name": field.name,
                            "start": field.start,
                            "length": field.length,
                        }
                        for field in schema.fields
                    ],
                    "delimited_columns": schema.delimited_columns,
                }
                changed = True
                print(f"  Extracted and cached PDF text/schema: {pdf.name}")
            period = schemas[pdf].effective_period
            print(
                f"    {pdf.name}: data type={schemas[pdf].data_type}; "
                f"period={period}; fields={len(schemas[pdf].fields)}/"
                f"{len(schemas[pdf].delimited_columns)}"
            )
        except Exception as exc:
            print(f"  Could not extract PDF {pdf.name}: {exc}")

    if changed:
        try:
            cache_path.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"  Could not write DMEPOS PDF cache: {exc}")
    return schemas, texts


def validate_txt_schema(txt_path: Path, schema: PdfSchema) -> tuple[bool, str]:
    try:
        lines = [
            line.rstrip("\r\n")
            for line in txt_path.read_text(
                encoding="utf-8-sig", errors="replace"
            ).splitlines()
            if line.strip()
        ]
    except OSError as exc:
        return False, f"could not read TXT: {exc}"
    if not lines:
        return False, "TXT contains no records"

    data_lines = [line for line in lines if not line.upper().startswith(("HDR", "TDR"))]
    if not data_lines:
        return False, "TXT contains only header records"

    if schema.fields:
        max_end = max(field.start + field.length - 1 for field in schema.fields)
        fitting = sum(len(line) >= max_end for line in data_lines[:100])
        ratio = fitting / min(len(data_lines), 100)
        if ratio < 0.80:
            return False, (
                f"fixed-width validation failed: {ratio:.0%} of records reach "
                f"documented position {max_end}"
            )

        hcpcs_fields = [
            field for field in schema.fields
            if "hcpcs" in normalize_label(field.name)
        ]
        if hcpcs_fields:
            field = hcpcs_fields[0]
            present = 0
            checked = 0
            for line in data_lines[:100]:
                value = line[field.start - 1:field.start - 1 + field.length].strip()
                checked += 1
                if value:
                    present += 1
            if checked and present / checked < 0.80:
                return False, "documented HCPCS position is empty in most records"
        return True, f"fixed-width records fit through position {max_end}"

    if schema.delimited_columns:
        first = data_lines[0]
        delimiter = "|"
        if "\t" in first:
            delimiter = "\t"
        elif "," in first:
            delimiter = ","
        elif "~" in first:
            delimiter = "~"
        header = [
            normalize_label(value)
            for value in split_delimited_record(first)
        ]
        documented = [
            normalize_label(name)
            for name in schema.delimited_columns
        ]
        overlap = len(set(header) & set(documented))
        if overlap == 0:
            return False, (
                "delimited TXT fields do not match PDF column names"
            )
        return True, f"delimited TXT matches {overlap} PDF column name(s)"

    return False, "PDF contains neither fixed-width positions nor column names"


def match_txt_to_pdf(
    txt_path: Path,
    release: Release,
    schemas: dict[Path, PdfSchema],
) -> tuple[Optional[Path], str]:
    txt_label = normalize_label(txt_path.stem)
    candidates: list[tuple[int, Path, str]] = []

    for pdf, schema in schemas.items():
        score = 0
        reasons: list[str] = []
        pdf_label = normalize_label(pdf.stem)

        if txt_label == pdf_label:
            score += 100
            reasons.append("exact filename match")
        else:
            txt_tokens = set(re.findall(r"[a-z]+|\d+", txt_path.stem.lower()))
            pdf_tokens = set(re.findall(r"[a-z]+|\d+", pdf.stem.lower()))
            shared = txt_tokens & pdf_tokens
            if shared:
                score += 10 * len(shared)
                reasons.append("filename token match")

        year = str(release.year)
        if year in schema.effective_period or year in txt_path.name:
            score += 20
            reasons.append("effective year match")

        period = {"A": "January", "B": "April", "C": "July", "D": "October"}
        quarter_month = period.get(release.name[-1:].upper())
        if quarter_month and quarter_month.lower() in schema.effective_period.lower():
            score += 20
            reasons.append("effective period match")

        valid, validation_reason = validate_txt_schema(txt_path, schema)
        if valid:
            score += 50
            reasons.append(validation_reason)
        else:
            reasons.append(validation_reason)

        candidates.append((score, pdf, "; ".join(reasons)))

    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return None, "no DMEPOS PDFs were available"

    best_score, best_pdf, best_reason = candidates[0]
    valid, validation_reason = validate_txt_schema(txt_path, schemas[best_pdf])
    if best_score < 50 or not valid:
        candidate_names = ", ".join(
            f"{pdf.name} ({reason})" for _, pdf, reason in candidates
        )
        return None, (
            f"ambiguous or invalid match; candidates: {candidate_names}; "
            f"selected validation: {validation_reason}"
        )

    if len(candidates) > 1:
        second_score = candidates[1][0]
        if best_score - second_score < 10 and best_score < 150:
            candidate_names = ", ".join(
                f"{pdf.name} ({reason})" for _, pdf, reason in candidates[:3]
            )
            return None, (
                f"match is ambiguous; candidates: {candidate_names}"
            )
    return best_pdf, best_reason


def parse_txt_with_schema(
    txt_path: Path, schema: PdfSchema
) -> tuple[list[str], list[dict[str, str]]]:
    try:
        lines = [
            line for line in txt_path.read_text(
                encoding="utf-8-sig", errors="replace"
            ).splitlines()
            if line.strip()
        ]
    except OSError as exc:
        print(f"Could not read TXT file {txt_path.name}: {exc}")
        return [], []

    records: list[dict[str, str]] = []
    if schema.fields:
        headers = [normalize_header(field.name) for field in schema.fields]
        for line in lines:
            if line.upper().startswith(("HDR", "TDR")):
                continue
            row = {}
            for field in schema.fields:
                start = field.start - 1
                row[normalize_header(field.name)] = (
                    line[start:start + field.length].strip()
                )
            hcpcs = find_hcpcs_column(list(row))
            if hcpcs and row.get(hcpcs):
                records.append(row)
        return headers, records

    if schema.delimited_columns:
        first = lines[0]
        delimiter = "|"
        if "\t" in first:
            delimiter = "\t"
        elif "," in first:
            delimiter = ","
        elif "~" in first:
            delimiter = "~"
        header_values = split_delimited_record(first)
        data_lines = lines[1:] if any(
            normalize_label(name) in {
                normalize_label(value) for value in header_values
            }
            for name in schema.delimited_columns
        ) else lines
        header = [normalize_header(name) for name in schema.delimited_columns]
        for line in data_lines:
            values = [value.strip() for value in next(
                csv.reader([line], delimiter=delimiter)
            )]
            row = {
                header[i]: values[i] if i < len(values) else ""
                for i in range(len(header))
            }
            hcpcs = find_hcpcs_column(list(row))
            if hcpcs and row.get(hcpcs):
                records.append(row)
        return header, records
    return [], []


def parse_documented_fields(text: str) -> list[FieldSpec]:
    """Parse only field positions explicitly stated in CMS PDF documentation."""
    fields: list[FieldSpec] = []
    seen: set[str] = set()
    data_section = text
    match = re.search(r"Data Record\s*\([^)]+\)", text, re.I)
    if match:
        data_section = text[match.start():]

    for raw_line in data_section.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        match = re.match(
            r"^([A-Za-z][A-Za-z \-/]{1,60}?)\s+(\d{1,3})-(\d{1,3})"
            r"(?:\s|$)",
            line,
            re.I,
        )
        if not match:
            continue
        name = re.sub(r"^[\d.)\s]+", "", match.group(1)).strip(" -:\t")
        name = re.sub(r"\s+", " ", name)
        normalized = name.lower()
        if len(name) < 2 or normalized in seen or normalized.startswith("filler"):
            continue
        start = int(match.group(2))
        second = int(match.group(3))
        length = second - start + 1
        fields.append(FieldSpec(name=name, start=start, length=length))
        seen.add(normalized)
    return sorted(fields, key=lambda field: field.start)


def analyze_pdf_documentation(pdf_paths: Iterable[Path]) -> tuple[list[FieldSpec], list[Path]]:
    """Analyze PDF documentation and return explicit fixed-width field definitions."""
    fields: list[FieldSpec] = []
    analyzed: list[Path] = []
    for pdf in pdf_paths:
        if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
            continue
        try:
            text = pdf_text(pdf)
            analyzed.append(pdf)
            parsed = parse_documented_fields(text)
            if parsed:
                by_start = {field.start: field for field in fields}
                by_start.update({field.start: field for field in parsed})
                fields = sorted(by_start.values(), key=lambda field: field.start)
        except Exception as exc:
            print(f"Could not analyze PDF {pdf.name}: {exc}")
    return fields, analyzed


def split_delimited_record(line: str) -> list[str]:
    line = line.strip("\ufeff\r\n")
    for delimiter in ("|", "\t", ","):
        if delimiter in line:
            return [value.strip() for value in next(
                csv.reader([line], delimiter=delimiter))]
    return [part for part in re.split(r"\s{2,}", line) if part]


def choose_txt_files(txt_files: list[Path], schedule: str) -> list[Path]:
    if schedule == "Physician":
        non_qp = [path for path in txt_files
                  if "non" in str(path).lower() and "qp" in str(path).lower()]
        if non_qp:
            return non_qp
        return [path for path in txt_files
                if path.suffix.lower() in {".txt", ".dat"}
                and "qp" not in str(path).lower()]
    relevant = [path for path in txt_files if is_relevant_data_file(path)]
    text_files = [path for path in relevant
                  if path.suffix.lower() in {".txt", ".dat"}]
    return text_files or relevant


def choose_dmepos_txt_files(txt_files: Iterable[Path]) -> list[Path]:
    """Select the core DMEPOS fee schedule, excluding separate supplemental files."""
    return [path for path in txt_files if path.stem.upper().startswith("DMEPOS")]


def is_relevant_data_file(path: Path) -> bool:
    name = path.name.lower()
    ignored_terms = (
        "readme", "read me", "documentation", "broker-help",
        "license", "notice", "instructions",
    )
    if any(term in name for term in ignored_terms):
        return False
    return path.suffix.lower() in {".txt", ".dat", ".csv"}


def find_hcpcs_column(header: list[str]) -> Optional[str]:
    normalized = [re.sub(r"[^a-z0-9]", "", value.lower()) for value in header]
    for index, value in enumerate(normalized):
        if value in {"hcpcs", "hcpccode", "hcpcscode"} or "hcpcs" in value:
            return header[index]
    return None


def normalize_header(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return name or "Column"


def parse_txt_file(txt_path: Path, field_specs: list[FieldSpec],
                   schedule: str) -> tuple[list[str], list[dict[str, str]]]:
    """Parse TXT records using documented fixed widths or explicit delimiters."""
    if field_specs:
        headers = [normalize_header(field.name) for field in field_specs]
    else:
        headers = []
    records: list[dict[str, str]] = []
    try:
        with txt_path.open("r", encoding="utf-8-sig", errors="replace") as source:
            lines = [line for line in source if line.strip()]
    except OSError as exc:
        print(f"Could not read TXT file {txt_path.name}: {exc}")
        return headers, records
    if not lines:
        print(f"No records found in {txt_path.name}")
        return headers, records

    first = lines[0].strip("\r\n")
    if schedule == "CLFS" and field_specs:
        header = headers
        for line in lines:
            line = line.rstrip("\r\n")
            if line.startswith("HDR") or not line:
                continue
            row = {}
            for field in field_specs:
                start = field.start - 1
                end = start + field.length
                row[normalize_header(field.name)] = line[start:end].strip()
            hcpcs_column = find_hcpcs_column(list(row))
            if hcpcs_column and row.get(hcpcs_column):
                records.append(row)
        return header, records

    delimited = bool(re.search(r"[|,\t]", first))
    if delimited:
        candidate_header = split_delimited_record(first)
        if find_hcpcs_column(candidate_header):
            header = [normalize_header(value) for value in candidate_header]
            data_lines = lines[1:]
        else:
            header = headers
            data_lines = lines

        for line in data_lines:
            values = split_delimited_record(line)
            if not values or all(not value for value in values):
                continue
            if header:
                row = {header[i]: values[i].strip() if i < len(values) else ""
                       for i in range(len(header))}
            else:
                row = {f"Column_{i + 1}": value for i, value in enumerate(values)}
                header = list(row)
            records.append(row)
    elif field_specs:
        header = headers
        for line in lines:
            line = line.rstrip("\r\n")
            if line.startswith("HDR") or not line:
                continue
            row = {}
            for field in field_specs:
                start = field.start - 1
                end = start + field.length
                row[normalize_header(field.name)] = line[start:end].strip()
            hcpcs_column = find_hcpcs_column(list(row))
            if hcpcs_column and row.get(hcpcs_column):
                records.append(row)
    else:
        print(
            f"Cannot map {txt_path.name}: the CMS PDF documentation supplied no "
            "explicit field positions."
        )
        return headers, records
    return header, records


def composite_key(
    row: dict[str, str],
    hcpcs_column: str,
    key_fields: Optional[tuple[str, ...]] = None,
) -> tuple[str, ...]:
    """Return an HCPCS-based key, optionally including record dimensions."""
    if key_fields:
        return tuple(str(row.get(field, "")).strip().upper() for field in key_fields)
    return (str(row.get(hcpcs_column, "")).strip().upper(),)


def quarterly_update(existing: list[dict[str, str]],
                     records: Iterable[dict[str, str]],
                     key_fields: Optional[tuple[str, ...]] = None) -> tuple[
                         list[dict[str, str]], int, int, int]:
    """Merge matching records, updating existing rows instead of duplicating them."""
    result: dict[tuple[str, ...], dict[str, str]] = {}
    for row in existing:
        hcpcs_column = find_hcpcs_column(list(row))
        if hcpcs_column and str(row.get(hcpcs_column, "")).strip():
            result[composite_key(row, hcpcs_column, key_fields)] = dict(row)
        else:
            result[(str(row),)] = dict(row)

    new_count = 0
    updated_count = 0
    skipped_count = 0
    for row in records:
        hcpcs_column = find_hcpcs_column(list(row))
        if not hcpcs_column or not str(row.get(hcpcs_column, "")).strip():
            continue
        key = composite_key(row, hcpcs_column, key_fields)
        old = result.get(key)
        if old is None:
            result[key] = dict(row)
            new_count += 1
        elif old != row:
            result[key].update(row)
            updated_count += 1
        else:
            skipped_count += 1
    return list(result.values()), new_count, updated_count, skipped_count


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns: list[str] = []
    for row in rows:
        for column in row:
            if column not in columns:
                columns.append(column)
    if not columns:
        print(f"No mapped records to write to {path.name}")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_existing_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            return list(csv.DictReader(source))
    except (OSError, csv.Error) as exc:
        print(f"Could not read existing CSV {path.name}: {exc}")
        return []


def links_for_detail(detail_url: str) -> tuple[list[str], list[str]]:
    soup = fetch_html(detail_url)
    downloads: list[str] = []
    pdfs: list[str] = []
    for link in soup.find_all("a", href=True):
        href = html.unescape(link["href"].strip()).strip("<> ")
        href = urljoin(detail_url, href)
        full_url = href.lower()
        path = urlparse(full_url).path
        if re.search(r"\.(zip|txt|dat)(?:$|[?#])", full_url, re.I):
            downloads.append(href)
        if "license.asp" in full_url and "file=" in full_url:
            query = parse_qs(urlparse(href).query)
            file_value = (query.get("file") or [""])[0]
            if file_value:
                downloads.append(urljoin("https://www.cms.gov/", file_value.lstrip("/")))
        if re.search(r"\.pdf(?:$|[?#])", full_url, re.I):
            pdfs.append(href)
    return list(dict.fromkeys(downloads)), list(dict.fromkeys(pdfs))


def list_zip_members(archive_path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                return [f"CRC error: {bad_member}"]
            return archive.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        return [f"Invalid ZIP: {exc}"]


def process_selected_schedule(schedule: str, page_url: str) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filenames = {
        "CLFS": "CLFS_fee_schedule.csv",
        "Physician": "Physician_NonQP_fee_schedule.csv",
        "DMEPOS": "DMEPOS_fee_schedule.csv",
    }
    output_path = OUTPUT_DIR / filenames[schedule]
    consolidated = read_existing_csv(output_path)
    if schedule == "DMEPOS":
        if consolidated:
            print("  Rebuilding DMEPOS output using complete record keys.")
        consolidated = []
    if schedule == "CLFS" and consolidated:
        first_row = consolidated[0]
        if (
            "Copyright_Statement" in first_row
            or "Filler" in first_row
            or "Year" not in first_row
            or "~" in str(first_row.get("Year", "")).strip()
        ):
            print(
                "  Replacing legacy incorrectly mapped CLFS CSV records "
                "with the documented fixed-width mapping."
            )
            consolidated = []

    print(f"Discovering the latest 12 {schedule} releases...")
    releases = discover_releases(schedule, page_url)
    if not releases:
        raise RuntimeError(f"No CMS releases were discovered for {schedule}")
    print("Processing oldest to newest:")
    for release in releases:
        print(f"  {release.name}")

    successfully_processed = 0
    failed_quarters: list[str] = []
    txt_dat_discovered = 0
    total_records = 0
    total_new = 0
    total_updated = 0
    total_skipped = 0

    for release in releases:
        quarter_dir = DOWNLOAD_DIR / schedule / safe_name(release.name)
        quarter_dir.mkdir(parents=True, exist_ok=True)
        try:
            downloads, pdf_links = links_for_detail(release.url)
            if schedule == "CLFS" and not any(
                urlparse(url).path.lower().endswith(".zip") for url in downloads
            ):
                direct_zip = (
                    f"https://www.cms.gov/files/zip/{release.name.lower()}.zip"
                )
                downloads.append(direct_zip)
            for download_url in downloads:
                filename = safe_name(
                    Path(urlparse(download_url).path).name
                    or f"{release.name}.download"
                )
                download_file(download_url, quarter_dir / filename)
            for pdf_url in pdf_links:
                filename = safe_name(
                    Path(urlparse(pdf_url).path).name
                    or f"{release.name}.pdf"
                )
                download_file(pdf_url, quarter_dir / filename)
        except Exception as exc:
            print(f"Quarter {release.name} failed: {exc}")
            failed_quarters.append(release.name)
            continue

        archive_files = [
            archive for archive in quarter_dir.rglob("*")
            if archive.is_file() and archive.suffix.lower() == ".zip"
        ]
        for archive in archive_files:
            members = list_zip_members(archive)
            print(f"  Inspecting archive {archive.name}: {len(members)} member(s)")
            if members:
                print("  ZIP contents:")
                for member in members:
                    print(f"    {member}")
            extract_archives(
                archive,
                quarter_dir / "_extracted" / archive.stem,
            )

        direct_files = [path for path in quarter_dir.rglob("*") if path.is_file()]
        pdf_files = [path for path in direct_files if path.suffix.lower() == ".pdf"]
        data_files = [
            path for path in direct_files
            if is_relevant_data_file(path)
        ]
        txt_files = [
            path for path in data_files
            if path.suffix.lower() in {".txt", ".dat"}
        ]

        try:
            if schedule == "DMEPOS":
                if not pdf_files:
                    print(
                        f"Quarter {release.name}: no PDFs available to document TXT schemas"
                    )
                    failed_quarters.append(release.name)
                    continue

                schemas, _ = load_cached_dmepos_pdf_schemas(pdf_files, release)
                if not schemas:
                    print(
                        f"Quarter {release.name}: no usable DMEPOS PDF schemas were extracted"
                    )
                    failed_quarters.append(release.name)
                    continue

                dme_txt_files = choose_dmepos_txt_files(txt_files)
                if not dme_txt_files:
                    print(
                        f"Quarter {release.name}: no core DMEPOS TXT files found "
                        "after recursive extraction"
                    )
                    failed_quarters.append(release.name)
                    continue

                quarter_rows: list[dict[str, str]] = []
                matched_txt_count = 0
                for txt_file in dme_txt_files:
                    txt_dat_discovered += 1
                    matched_pdf, match_reason = match_txt_to_pdf(
                        txt_file, release, schemas
                    )
                    if matched_pdf is None:
                        candidates = ", ".join(
                            pdf.name for pdf in schemas
                        )
                        print(
                            f"  TXT {txt_file.name}: could not be matched safely. "
                            f"Candidate PDFs: {candidates}. Reason: {match_reason}"
                        )
                        continue

                    print(
                        f"  Matching TXT {txt_file.name} to PDF "
                        f"{matched_pdf.name}: {match_reason}"
                    )
                    headers, records = parse_txt_with_schema(
                        txt_file, schemas[matched_pdf]
                    )
                    valid, validation_reason = validate_txt_schema(
                        txt_file, schemas[matched_pdf]
                    )
                    if not valid or not headers or not records:
                        print(
                            f"  TXT {txt_file.name}: match validation failed; "
                            f"reason: {validation_reason or 'no parsed records'}"
                        )
                        continue

                    matched_txt_count += 1
                    quarter_rows.extend(records)
                    print(
                        f"  Mapped {len(records)} record(s) from {txt_file.name} "
                        f"using {matched_pdf.name}"
                    )

                if not quarter_rows:
                    print(
                        f"Quarter {release.name}: no TXT could be matched and validated "
                        "without guessing"
                    )
                    failed_quarters.append(release.name)
                    continue

                consolidated, new_count, updated_count, skipped_count = quarterly_update(
                    consolidated, quarter_rows, key_fields=DMEPOS_KEY_FIELDS
                )
                total_records += len(quarter_rows)
                total_new += new_count
                total_updated += updated_count
                total_skipped += skipped_count
                write_csv(output_path, consolidated)
                successfully_processed += 1
                continue

            fields, analyzed_pdfs = analyze_pdf_documentation(pdf_files)
            if analyzed_pdfs:
                print(f"  Analyzed {len(analyzed_pdfs)} PDF(s)")
            if not data_files:
                print(
                    f"Quarter {release.name}: missing valid TXT, DAT, or CSV data files"
                )
                if archive_files:
                    print("  Downloaded ZIP contents were inspected; no data file found.")
                else:
                    print("  No ZIP archive was downloaded or found.")
                failed_quarters.append(release.name)
                continue

            selected_txt = choose_txt_files(data_files, schedule)
            if not selected_txt:
                print(f"Quarter {release.name}: no relevant data file found")
                failed_quarters.append(release.name)
                continue

            quarter_rows: list[dict[str, str]] = []
            for txt_file in selected_txt:
                if txt_file.suffix.lower() in {".txt", ".dat"}:
                    txt_dat_discovered += 1
                headers, records = parse_txt_file(txt_file, fields, schedule)
                if not headers and not records:
                    continue
                quarter_rows.extend(records)
                print(f"  Mapped {len(records)} record(s) from {txt_file.name}")
            if not quarter_rows:
                print(f"Quarter {release.name}: no records could be mapped")
                failed_quarters.append(release.name)
                continue

            consolidated, new_count, updated_count, skipped_count = quarterly_update(
                consolidated, quarter_rows
            )
            total_records += len(quarter_rows)
            total_new += new_count
            total_updated += updated_count
            total_skipped += skipped_count
            write_csv(output_path, consolidated)
            successfully_processed += 1
        except Exception as exc:
            print(f"Quarter {release.name} conversion failed: {exc}")
            failed_quarters.append(release.name)

    print("\nQuarterly processing summary:")
    print(f"  Total quarters discovered: {len(releases)}")
    print(f"  Successfully processed quarters: {successfully_processed}")
    print(f"  Failed quarters: {len(failed_quarters)}")
    print(f"  TXT and DAT files discovered: {txt_dat_discovered}")
    print(f"  Total records mapped: {total_records}")
    print(f"  Total new records: {total_new}")
    print(f"  Total updated records: {total_updated}")
    print(f"  Total skipped records: {total_skipped}")
    if failed_quarters:
        print("  Failed quarter list: " + ", ".join(failed_quarters))

    if len(releases) == successfully_processed and output_path.is_file():
        print(f"Output saved successfully: {output_path}")
    elif output_path.is_file():
        print(
            f"INCOMPLETE CSV output: {output_path}. "
            "Only successfully processed quarters are represented."
        )
    else:
        print("INCOMPLETE: No successfully mapped records were available; no CSV was created.")
    return output_path


def main() -> int:
    print("Enter one CMS URL:")
    for url in APPROVED_URLS:
        print(url)
    entered = input("CMS URL: ").strip()
    if entered not in APPROVED_URLS:
        print("Access Denied: Only the three approved CMS URLs are allowed.")
        return 1
    schedule = APPROVED_URLS[entered]
    try:
        process_selected_schedule(schedule, entered)
    except requests.RequestException as exc:
        print(f"Network error: {exc}")
        return 1
    except Exception as exc:
        print(f"Application error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())