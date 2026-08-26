"""Audita etiquetas de calidad muscular almacenadas sin modificar producción.

Compara la etiqueta incluida en cada HTML de Storage con la clasificación
Tanita RD-545 correspondiente al sexo y edad del paciente.

Uso:
    python3 scripts/audit_muscle_quality_reports.py
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis_engine import classify_range  # noqa: E402
from db import DB  # noqa: E402


_MQ_PAYLOAD_RE = re.compile(
    r'"muscle_quality_total"\s*:\s*(?P<score>-?\d+(?:\.\d+)?)'
    r'\s*,\s*"muscle_quality_label"\s*:\s*"(?P<label>[^"]+)"'
    r'.{0,500}?"mq_ref_min"\s*:\s*(?P<ref_min>-?\d+(?:\.\d+)?)'
    r'\s*,\s*"mq_ref_max"\s*:\s*(?P<ref_max>-?\d+(?:\.\d+)?)'
)


def _age_on(date_of_birth, measurement_date) -> int:
    born = date.fromisoformat(str(date_of_birth)[:10])
    measured = datetime.fromisoformat(
        str(measurement_date).replace("Z", "+00:00")
    ).date()
    return measured.year - born.year - (
        (measured.month, measured.day) < (born.month, born.day)
    )


def audit_reports(db: DB, workers: int = 16) -> dict:
    reports = (
        db.client.table("reports")
        .select(
            "id,nutri_id,patient_id,measurement_date,generated_at,source"
        )
        .order("generated_at")
        .execute()
        .data
        or []
    )
    patients = (
        db.client.table("patients")
        .select("id,full_name,date_of_birth,sex")
        .execute()
        .data
        or []
    )
    nutris = (
        db.client.table("nutris")
        .select("id,full_name,email")
        .execute()
        .data
        or []
    )

    patient_by_id = {row["id"]: row for row in patients}
    nutri_by_id = {row["id"]: row for row in nutris}
    mismatches = []
    unreadable = []

    base_url = os.environ["SUPABASE_URL"].rstrip("/")
    service_key = os.environ["SUPABASE_SERVICE_KEY"]
    http = httpx.Client(
        headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
        timeout=httpx.Timeout(20.0),
        limits=httpx.Limits(max_connections=workers, max_keepalive_connections=workers),
        http2=False,
    )

    def inspect_report(report):
        report_id = report["id"]
        nutri_id = report["nutri_id"]
        storage_path = f"{nutri_id}/{report_id}.html"
        try:
            url = (
                f"{base_url}/storage/v1/object/authenticated/reports/"
                f"{quote(storage_path, safe='/')}"
            )
            response = http.get(url)
            response.raise_for_status()
            html = response.text
        except Exception as exc:
            return None, {"report_id": report_id, "error": str(exc)}

        match = _MQ_PAYLOAD_RE.search(html)
        if not match:
            return None, {"report_id": report_id, "error": "mq_not_found"}

        patient = patient_by_id.get(report["patient_id"])
        if not patient or not patient.get("date_of_birth"):
            return None, {"report_id": report_id, "error": "patient_data_missing"}

        score = float(match.group("score"))
        stored_label = match.group("label")
        age = _age_on(patient["date_of_birth"], report["measurement_date"])
        normal_range = (float(match.group("ref_min")), float(match.group("ref_max")))
        expected_label = classify_range(score, normal_range)
        if stored_label == expected_label:
            return None, None

        nutri = nutri_by_id.get(nutri_id) or {}
        return {
            "report_id": report_id,
            "nutri_id": nutri_id,
            "nutri_name": nutri.get("full_name") or "",
            "nutri_email": nutri.get("email") or "",
            "patient_id": report["patient_id"],
            "patient_name": patient.get("full_name") or "",
            "measurement_date": report.get("measurement_date"),
            "source": report.get("source") or "mytanita",
            "sex": patient.get("sex"),
            "age": age,
            "score": score,
            "normal_range": normal_range,
            "stored_label": stored_label,
            "expected_label": expected_label,
            "false_low": stored_label == "Bajo" and expected_label != "Bajo",
        }, None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(inspect_report, report) for report in reports]
        for index, future in enumerate(as_completed(futures), start=1):
            mismatch, error = future.result()
            if mismatch:
                mismatches.append(mismatch)
            if error:
                unreadable.append(error)
            if index % 100 == 0:
                print(f"Auditados {index}/{len(reports)}...", file=sys.stderr)

    http.close()

    by_nutri = defaultdict(lambda: {"total": 0, "false_low": 0})
    for item in mismatches:
        key = (item["nutri_id"], item["nutri_name"], item["nutri_email"])
        by_nutri[key]["total"] += 1
        by_nutri[key]["false_low"] += int(item["false_low"])

    grouped = [
        {
            "nutri_id": key[0],
            "nutri_name": key[1],
            "nutri_email": key[2],
            **counts,
        }
        for key, counts in by_nutri.items()
    ]
    grouped.sort(key=lambda row: (-row["total"], row["nutri_name"].lower()))

    return {
        "reports_audited": len(reports),
        "mismatches": len(mismatches),
        "false_low": sum(item["false_low"] for item in mismatches),
        "transition_counts": dict(
            Counter(
                f'{item["stored_label"]} -> {item["expected_label"]}'
                for item in mismatches
            )
        ),
        "unreadable": len(unreadable),
        "by_nutri": grouped,
        "reports": mismatches,
        "unreadable_reports": unreadable,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="Archivo JSON opcional con el detalle")
    parser.add_argument(
        "--summary-only", action="store_true", help="No imprimir cada reporte"
    )
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    load_dotenv()
    result = audit_reports(DB(), workers=max(1, args.workers))
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    printable = dict(result)
    if args.summary_only:
        printable.pop("reports")
        printable.pop("unreadable_reports")
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
