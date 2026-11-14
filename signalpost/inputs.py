"""Manifest and optional registry snapshot readers. Missing rows never abort a batch."""
from __future__ import annotations
import csv
import gzip
import hashlib
import io
import json
import re
from pathlib import Path
from .net import Response

ORG_KEYS = ("organisation_number", "organisasjonsnummer", "orgnr", "org_nr", "organization_number", "id")


def read_text(path):
    p = Path(path)
    opener = gzip.open if p.suffix.lower() == ".gz" else open
    with opener(p, "rt", encoding="utf-8-sig") as handle:
        return handle.read()


def normalize_input(item):
    value = next((item[k] for k in ORG_KEYS if k in item), "") if isinstance(item, dict) else item
    raw = str(value or "").strip()
    digits = re.sub(r"\s", "", raw)
    return {"organisation_number": digits if re.fullmatch(r"\d{9}", digits) else raw,
            "input_valid": bool(re.fullmatch(r"\d{9}", digits))}


def load_manifest(path):
    text = read_text(path).strip()
    if not text:
        raise ValueError("The supplied manifest is empty")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        if any(k in body for k in ORG_KEYS):
            body = [body]
        else:
            body = next((body[k] for k in ("organisations", "organisation_numbers", "companies", "manifest", "items", "rows", "data") if isinstance(body.get(k), list)), None)
            if body is None:
                raise ValueError("Manifest object has no recognised list field")
    if body is not None:
        rows = body if isinstance(body, list) else [body]
    else:
        lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if not lines:
            raise ValueError("The supplied manifest has no records")
        if lines[0].lstrip().startswith(("{", '"')):
            rows = [json.loads(line) for line in lines]
        elif any(key in lines[0].casefold() for key in ORG_KEYS):
            dialect = csv.Sniffer().sniff("\n".join(lines), delimiters=",;\t")
            rows = list(csv.DictReader(io.StringIO("\n".join(lines)), dialect=dialect))
        else:
            rows = lines
    if not rows:
        raise ValueError("The supplied manifest has no records")
    return [normalize_input(row) for row in rows]


def _boolean(value):
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "false"}:
        return str(value).lower() == "true"
    return None


def registry_record(row):
    if "organisasjonsnummer" in row and isinstance(row.get("organisasjonsform"), dict):
        return row
    def pick(*keys):
        return next((row[k] for k in keys if row.get(k) not in (None, "")), None)
    out = {"organisasjonsnummer": str(pick(*ORG_KEYS) or ""),
           "navn": pick("legal_name", "name", "navn", "Navn"),
           "hjemmeside": pick("website", "hjemmeside", "Hjemmeside"),
           "organisasjonsform": {"kode": pick("legal_form", "organisasjonsform.kode", "Organisasjonsform.kode")},
           "naeringskode1": {"kode": pick("industry_code", "naeringskode1.kode", "Næringskode1.kode"), "beskrivelse": pick("industry_label", "naeringskode1.beskrivelse", "Næringskode1.beskrivelse")},
           "forretningsadresse": {"kommune": pick("municipality", "forretningsadresse.kommune", "Forretningsadresse.kommune"),
                                 "postnummer": pick("forretningsadresse.postnummer", "Forretningsadresse.postnummer"),
                                 "poststed": pick("forretningsadresse.poststed", "Forretningsadresse.poststed"),
                                 "adresse": pick("forretningsadresse.adresse", "Forretningsadresse.adresse")}}
    employees = pick("employees", "employee_count", "antallAnsatte", "Antall ansatte")
    if employees is not None:
        try:
            out["antallAnsatte"] = int(employees)
        except (ValueError, TypeError):
            pass
    for key, aliases in (("konkurs", ("bankrupt", "konkurs", "Konkurs")), ("underAvvikling", ("liquidating", "underAvvikling", "Under avvikling"))):
        val = _boolean(pick(*aliases))
        if val is not None:
            out[key] = val
    return out


def load_snapshot(path, wanted):
    if path is None:
        return {}, []
    p = Path(path)
    warnings = []
    records = {}
    try:
        # Preserve the exact supplied file as evidence; never pretend a generated
        # row is a fresh network response. A file URL explicitly declares its origin.
        source_url = p.resolve().as_uri()
        from datetime import datetime, timezone
        retrieved = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        suffixes = "".join(p.suffixes).lower()
        opener = gzip.open if p.suffix.lower() == ".gz" else open
        def accept(row, raw_text, line_number):
            record = registry_record(row)
            org = str(record.get("organisasjonsnummer") or "")
            if org in wanted:
                response = Response(source_url, 200, raw_text.encode("utf-8"), {"content-type": "text/plain; charset=utf-8"}, retrieved, state="available")
                response.snapshot_line = line_number
                records[org] = (record, response)
        with opener(p, "rt", encoding="utf-8-sig", newline="") as handle:
            if ".csv" in suffixes or ".tsv" in suffixes:
                sample = handle.read(32768)
                handle.seek(0)
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                # Capture actual CSV record bytes, including quoted multiline cells.
                captured = []
                def lines():
                    for line in handle:
                        captured.append(line)
                        yield line
                reader = csv.DictReader(lines(), dialect=dialect)
                reader.fieldnames
                header = "".join(captured)
                captured.clear()
                for row in reader:
                    raw_text = header + "".join(captured)
                    captured.clear()
                    accept(row, raw_text, reader.line_num)
                    if len(records) == len(wanted):
                        break
            else:
                first = handle.read(1)
                handle.seek(0)
                if first == "[":
                    # Arrays are supported for small explicit snapshots; JSONL streams
                    # avoid retaining the 411,160-record universe in memory.
                    text = handle.read()
                    for row in json.loads(text):
                        accept(row, text, 1)
                else:
                    for line_number, line in enumerate(handle, 1):
                        if line.strip():
                            accept(json.loads(line), line, line_number)
                        if len(records) == len(wanted):
                            break
    except (OSError, ValueError, UnicodeError, csv.Error, TypeError) as exc:
        warnings.append(f"Optional snapshot unavailable ({type(exc).__name__}); live registry fallback will run")
    return records, warnings
