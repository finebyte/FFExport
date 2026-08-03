#!/usr/bin/env python3
"""Export read-only member data from the Fitness First UK Evolt Active backend."""

import argparse
import csv
import json
import mimetypes
import os
import sys
import uuid
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


DEFAULT_BASE_URL = "https://webservice.evoltactive.com"
DEFAULT_OUTPUT_DIR = Path("EvoltExport")
DEFAULT_USER_AGENT = "Mozilla/5.0"
JSON_HEADERS = {"Accept": "application/json"}


@dataclass
class ApiResponse:
    url: str
    status: int
    payload: Any


class EvoltExportClient:
    def __init__(self, base_url: str, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.access_token: str | None = None
        self.refresh_token: str | None = None

    def _headers(self, extra: dict[str, str] | None = None, auth: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self.user_agent,
        }
        if auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = False,
        expect_json: bool = True,
    ) -> ApiResponse:
        url = f"{self.base_url}{path}"
        request = Request(url, data=body, headers=self._headers(headers, auth=auth), method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
                status = response.status
                content_type = response.headers.get("Content-Type", "")
        except HTTPError as exc:
            raw = exc.read()
            detail = raw.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc

        if not expect_json:
            return ApiResponse(url=url, status=status, payload={"bytes": raw, "content_type": content_type})

        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            payload = {"raw": raw.decode("utf-8", errors="replace")}
        return ApiResponse(url=url, status=status, payload=payload)

    def login(self, username: str, password: str) -> ApiResponse:
        body, content_type = encode_multipart_formdata({"username": username, "password": password})
        response = self._request(
            "/api/login",
            method="POST",
            body=body,
            headers={"Content-Type": content_type},
        )
        self.access_token = string_or_none(response.payload.get("access_token"))
        self.refresh_token = string_or_none(response.payload.get("refresh_token"))
        return response

    def get_json(self, path: str, *, auth: bool = True) -> ApiResponse:
        return self._request(path, auth=auth)

    def get_binary(self, path: str, *, auth: bool = True) -> ApiResponse:
        return self._request(path, auth=auth, expect_json=False)


def string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def env_default(primary: str, legacy: str | None = None) -> str | None:
    value = os.getenv(primary)
    if value:
        return value
    if legacy:
        return os.getenv(legacy)
    return None


def encode_multipart_formdata(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----EvoltExport{uuid.uuid4().hex}"
    lines: list[bytes] = []
    for name, value in fields.items():
        lines.extend(
            [
                f"--{boundary}".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"),
                b"",
                str(value).encode("utf-8"),
            ]
        )
    lines.append(f"--{boundary}--".encode("utf-8"))
    lines.append(b"")
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


def ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def redact_sensitive_payload(value: Any) -> Any:
    sensitive_keys = {
        "access_token",
        "refresh_token",
        "token",
        "id_token",
        "authorization",
        "password",
    }
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            if key.lower() in sensitive_keys:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redact_sensitive_payload(child)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_payload(item) for item in value]
    return value


def normalize_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=True)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: normalize_scalar(row.get(key)) for key in fieldnames})


def flatten_object(value: Any, prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(flatten_object(child, child_prefix))
        return flattened
    if isinstance(value, list):
        if all(not isinstance(item, (dict, list)) for item in value):
            flattened[prefix] = "|".join("" if item is None else str(item) for item in value)
            return flattened
        flattened[prefix] = json.dumps(value, ensure_ascii=True)
        return flattened
    flattened[prefix] = value
    return flattened


def guess_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "results", "data", "entries", "scans", "notes", "countries"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            nested = guess_rows(value)
            if nested:
                return nested
    return []


def first_present(obj: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def summarize_scans(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary_rows: list[dict[str, Any]] = []
    for scan in rows:
        flat = flatten_object(scan)
        summary_rows.append(
            {
                "id": first_present(flat, ["id"]),
                "type": first_present(flat, ["type"]),
                "scan_datetime": first_present(
                    flat,
                    [
                        "utc_created",
                        "created_at",
                        "createdAt",
                        "scan_date",
                        "scanned_at",
                    ],
                ),
                "bwi_result": first_present(flat, ["total_score", "bwi", "bwi_score"]),
                "total_body_weight_kg": first_present(flat, ["weight", "total_body_weight", "weight_kg"]),
                "lean_body_mass_kg": first_present(flat, ["lbm_quantity", "lean_body_mass", "lbm"]),
                "skeletal_muscle_mass_kg": first_present(flat, ["smm_quantity", "skeletal_muscle_mass"]),
                "total_fat_mass_kg": first_present(flat, ["bfm_quantity", "total_fat_mass"]),
                "total_body_fat_percentage": first_present(flat, ["pbf_rate", "total_body_fat_percentage"]),
                "visceral_fat_level": first_present(flat, ["whr_level", "visceral_fat_level"]),
                "visceral_fat_area_cm2": first_present(flat, ["visceral_fat_area", "vfa"]),
                "bio_age": first_present(flat, ["biological_age", "bio_age"]),
                "basal_metabolic_rate_kcal": first_present(flat, ["bmr", "basal_metabolic_rate"]),
                "total_energy_expenditure_kcal": first_present(flat, ["tee", "total_energy_expenditure"]),
                "notes": first_present(flat, ["notes"]),
            }
        )
    return summary_rows


def download_scan_assets(
    client: EvoltExportClient,
    output_dir: Path,
    scans: list[dict[str, Any]],
) -> list[str]:
    failures: list[str] = []
    assets_dir = output_dir / "scan_assets"
    ensure_output_dir(assets_dir)
    for scan in scans:
        scan_id = scan.get("id")
        if scan_id in (None, ""):
            continue
        scan_type = str(scan.get("type") or "")
        endpoint = f"/api/manualscanimage/{scan_id}" if scan_type == "manual" else f"/api/scanpdf/{scan_id}"
        try:
            response = client.get_binary(endpoint)
        except RuntimeError as exc:
            failures.append(f"{endpoint}: {exc}")
            continue
        content_type = str(response.payload.get("content_type") or "")
        extension = guess_extension(content_type, scan_type)
        target = assets_dir / f"{scan_id}{extension}"
        target.write_bytes(response.payload["bytes"])
    return failures


def guess_extension(content_type: str, scan_type: str) -> str:
    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
    if guessed:
        return guessed
    if scan_type == "manual":
        return ".bin"
    return ".pdf"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export read-only member data from the Fitness First UK Evolt Active backend."
    )
    parser.add_argument("--email", default=env_default("EVOLT_EMAIL"), help="Member login email")
    parser.add_argument("--password", default=env_default("EVOLT_PASSWORD"), help="Member password")
    parser.add_argument("--base-url", default=env_default("EVOLT_BASE_URL") or DEFAULT_BASE_URL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--skip-assets",
        action="store_true",
        help="Skip downloading scan PDFs and manual scan images",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.email or not args.password:
        print("Set EVOLT_EMAIL and EVOLT_PASSWORD, or pass --email and --password.", file=sys.stderr)
        return 2

    ensure_output_dir(args.output_dir)
    client = EvoltExportClient(base_url=args.base_url)

    print(f"Logging into {args.base_url} ...", file=sys.stderr)
    login_response = client.login(args.email, args.password)
    write_json(args.output_dir / "login.json", redact_sensitive_payload(login_response.payload))

    if not client.access_token:
        print("Login did not return an access token. Inspect login.json for details.", file=sys.stderr)
        return 1

    endpoints = {
        "member": "/api/member",
        "scans": "/api/scans",
        "scannotes": "/api/scannotes",
        "macronutrientcalculations": "/api/macronutrientcalculations",
        "supplementresponse": "/api/supplementresponse",
        "hlresponse": "/api/hlresponse",
        "supplementquestions": "/api/supplementquestions",
        "hlquestions": "/api/hlquestions",
        "faqs": "/api/faqs",
        "scanfaqs": "/api/scanfaqs",
        "countries": "/api/countries",
    }

    failures: list[str] = []
    collected_payloads: dict[str, Any] = {}
    for name, endpoint in endpoints.items():
        try:
            response = client.get_json(endpoint)
        except RuntimeError as exc:
            failures.append(f"{name}: {exc}")
            continue
        collected_payloads[name] = response.payload
        write_json(args.output_dir / f"{name}.json", response.payload)
        rows = guess_rows(response.payload)
        if rows:
            write_csv(args.output_dir / f"{name}.csv", [flatten_object(row) for row in rows])

    scans_rows = guess_rows(collected_payloads.get("scans", []))
    if scans_rows:
        write_csv(args.output_dir / "scans_summary.csv", summarize_scans(scans_rows))
        if not args.skip_assets:
            failures.extend(download_scan_assets(client, args.output_dir, scans_rows))

    summary = {
        "base_url": args.base_url,
        "email": args.email,
        "member_id": first_present(flatten_object(collected_payloads.get("member", {})), ["id"]),
        "scan_count": len(scans_rows),
        "downloaded_assets": not args.skip_assets,
    }
    write_json(args.output_dir / "summary.json", summary)

    if failures:
        print("Some endpoints or asset downloads failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)

    print(f"Export complete in {args.output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
