#!/usr/bin/env python3
"""FFExport: export read-only member data from the Fitness First UK app backend."""

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


DEFAULT_BASE_URL = "https://dwfitnessfirstcore.netpulse.com"
DEFAULT_OUTPUT_DIR = Path("FFExport")
DEFAULT_LOOKBACK_DAYS = 365
DEFAULT_APP_VERSION = "106175"
DEFAULT_API_VERSION = "1.5"
DEFAULT_USER_AGENT = "Android/com.dwfitnessfirstcore.core"
DEFAULT_WORKOUT_CHUNK_DAYS = 31
WORKOUTS_APP_VERSION = "3.11"
WORKOUTS_USER_AGENT = "NetpulseFitness/3.11"
WORKOUTS_X_NP_USER_AGENT = (
    "clientType=MOBILE_DEVICE; "
    "devicePlatform=IOS; "
    "deviceUid=fitness-first-export; "
    "applicationName=NetpulseFitness; "
    "applicationVersion=3.11"
)


@dataclass
class ApiResponse:
    url: str
    status: int
    payload: Any


class FFExportClient:
    def __init__(
        self,
        base_url: str,
        app_version: str = DEFAULT_APP_VERSION,
        api_version: str = DEFAULT_API_VERSION,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.app_version = app_version
        self.api_version = api_version
        self.user_agent = user_agent
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-NP-API-Version": self.api_version,
            "X-NP-APP-Version": self.app_version,
            "X-NP-User-Agent": self.user_agent,
            "User-Agent": self.user_agent,
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(self, path: str, *, method: str = "GET", data: dict[str, Any] | None = None) -> ApiResponse:
        url = f"{self.base_url}{path}"
        body = None
        headers = self._headers()
        if data is not None:
            body = urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {raw[:500]}") from exc
        except URLError as exc:
            raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return ApiResponse(url=url, status=status, payload=payload)

    def login(self, username: str, password: str) -> ApiResponse:
        return self._request(
            "/np/exerciser/login",
            method="POST",
            data={"username": username, "password": password},
        )

    def get(self, path: str) -> ApiResponse:
        return self._request(path)

    def get_absolute(self, url: str, headers: dict[str, str]) -> ApiResponse:
        request = Request(url, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {raw[:500]}") from exc
        except URLError as exc:
            raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return ApiResponse(url=url, status=status, payload=payload)


def nested_get(data: Any, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current = data
        found = True
        for key in path:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                found = False
                break
        if found and current not in (None, ""):
            return current
    return None


def extract_user_uuid(login_payload: Any) -> str | None:
    candidates = [
        ("uuid",),
        ("userId",),
        ("id",),
        ("exerciser", "uuid"),
        ("exerciser", "userId"),
        ("exerciser", "id"),
        ("data", "uuid"),
        ("data", "userId"),
        ("data", "exerciser", "uuid"),
    ]
    value = nested_get(login_payload, *candidates)
    return str(value) if value else None


def extract_email(login_payload: Any) -> str | None:
    candidates = [
        ("email",),
        ("username",),
        ("exerciser", "email"),
        ("data", "email"),
    ]
    value = nested_get(login_payload, *candidates)
    return str(value) if value else None


def guess_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "items",
        "results",
        "data",
        "checkIns",
        "checkins",
        "visits",
        "entries",
        "bookings",
        "classes",
        "workouts",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            nested = guess_rows(value)
            if nested:
                return nested
    return []


def normalize_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=True)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


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


def iso_utc_range(start_date: date, end_date: date) -> tuple[str, str]:
    completed_after = datetime.combine(start_date, time.min, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    completed_before = datetime.combine(end_date, time(23, 59, 59), tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return completed_after, completed_before


def summarize_checkins(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        check_in_at = row.get("checkInDate") or row.get("checkinDate") or row.get("startDate")
        if not isinstance(check_in_at, str) or len(check_in_at) < 10:
            continue
        day = check_in_at[:10]
        club_name = row.get("gymLocationName") or row.get("clubName") or ""
        entry = by_day.setdefault(
            day,
            {
                "date": day,
                "visit_count": 0,
                "first_check_in": check_in_at,
                "last_check_in": check_in_at,
                "clubs": set(),
            },
        )
        entry["visit_count"] += 1
        entry["first_check_in"] = min(entry["first_check_in"], check_in_at)
        entry["last_check_in"] = max(entry["last_check_in"], check_in_at)
        if club_name:
            entry["clubs"].add(club_name)

    summary_rows = []
    for day in sorted(by_day):
        entry = by_day[day]
        summary_rows.append(
            {
                "date": entry["date"],
                "visit_count": entry["visit_count"],
                "first_check_in": entry["first_check_in"],
                "last_check_in": entry["last_check_in"],
                "clubs": " | ".join(sorted(entry["clubs"])),
            }
        )
    return summary_rows


def summarize_workouts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary_rows = []
    for row in rows:
        exercises = row.get("exercises")
        exercise_count = len(exercises) if isinstance(exercises, list) else ""
        summary_rows.append(
            {
                "id": row.get("id") or row.get("uuid"),
                "completed_at": row.get("completedAt") or row.get("completedDate") or row.get("workoutDate"),
                "started_at": row.get("startedAt") or row.get("startDate"),
                "title": row.get("title") or row.get("name") or row.get("trainingPlanLabel"),
                "training_plan": row.get("trainingPlanLabel") or row.get("planName"),
                "activity_points": row.get("activityPoints"),
                "exercise_count": exercise_count,
            }
        )
    return sorted(summary_rows, key=lambda item: item.get("completed_at") or "")


def flatten_workout_exercises(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened = []
    for workout in rows:
        for exercise in workout.get("exercises") or []:
            source = exercise.get("source") or {}
            attrs = exercise.get("attributes") or {}
            flattened.append(
                {
                    "workout_code": workout.get("code"),
                    "workout_completed_at": workout.get("completedAt"),
                    "exercise_code": exercise.get("exerciseCode"),
                    "exercise_name": exercise.get("name") or (exercise.get("exercise") or {}).get("label"),
                    "source_label": source.get("label"),
                    "source_code": source.get("code"),
                    "calories": nested_get(attrs, ("calories", "value")),
                    "activity_points": nested_get(attrs, ("activity_points", "value")),
                    "set_count": len(attrs.get("sets_of_reps_and_weight_or_duration_and_weight") or []),
                }
            )
    return sorted(flattened, key=lambda item: (item.get("workout_completed_at") or "", item.get("exercise_name") or ""))


def flatten_machine_workout_sets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened = []
    for workout in rows:
        for exercise in workout.get("exercises") or []:
            source = exercise.get("source") or {}
            if source.get("code") != "fitness_machine":
                continue
            sets = (exercise.get("attributes") or {}).get("sets_of_reps_and_weight_or_duration_and_weight") or []
            for index, set_row in enumerate(sets, start=1):
                flattened.append(
                    {
                        "workout_code": workout.get("code"),
                        "workout_completed_at": workout.get("completedAt"),
                        "exercise_code": exercise.get("exerciseCode"),
                        "exercise_name": exercise.get("name") or (exercise.get("exercise") or {}).get("label"),
                        "set_number": index,
                        "reps": nested_get(set_row, ("reps", "value")),
                        "reps_unit": nested_get(set_row, ("reps", "unit")),
                        "weight": nested_get(set_row, ("weight", "value")),
                        "weight_unit": nested_get(set_row, ("weight", "unit")),
                        "duration": nested_get(set_row, ("duration", "value")),
                        "duration_unit": nested_get(set_row, ("duration", "unit")),
                        "recovery_time": nested_get(set_row, ("recovery_time", "value")),
                        "recovery_time_unit": nested_get(set_row, ("recovery_time", "unit")),
                    }
                )
    return sorted(flattened, key=lambda item: (item.get("workout_completed_at") or "", item.get("exercise_name") or "", item.get("set_number") or 0))


def flatten_checkins(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened = []
    for row in rows:
        club = row.get("club") if isinstance(row.get("club"), dict) else {}
        flattened.append(
            {
                "id": row.get("id") or row.get("uuid") or row.get("checkinId"),
                "check_in_at": row.get("checkInDate") or row.get("checkinDate") or row.get("startDate"),
                "check_out_at": row.get("checkOutDate") or row.get("endDate"),
                "duration_minutes": row.get("durationMinutes"),
                "duration_millis": row.get("duration"),
                "club_name": row.get("clubName") or row.get("gymLocationName") or club.get("name"),
                "club_address": row.get("clubAddress") or row.get("gymLocationAddress") or club.get("address1"),
                "club_city": club.get("city"),
                "timezone": row.get("timezone"),
                "raw_status": row.get("status"),
            }
        )
    return sorted(flattened, key=lambda item: item.get("check_in_at") or "")


def flatten_schedule(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened = []
    for row in rows:
        club = row.get("club") if isinstance(row.get("club"), dict) else {}
        instructor = row.get("instructor") if isinstance(row.get("instructor"), dict) else {}
        flattened.append(
            {
                "id": row.get("id") or row.get("uuid"),
                "title": row.get("name") or row.get("title") or row.get("activityName"),
                "starts_at": row.get("startDate") or row.get("startTime"),
                "ends_at": row.get("endDate") or row.get("endTime"),
                "status": row.get("bookingStatus") or row.get("status"),
                "club_name": row.get("clubName") or club.get("name"),
                "instructor": row.get("instructorName") or instructor.get("name"),
            }
        )
    return flattened


def ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def workouts_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": WORKOUTS_USER_AGENT,
        "X-NP-APP-Version": WORKOUTS_APP_VERSION,
        "X-NP-User-Agent": WORKOUTS_X_NP_USER_AGENT,
    }


def build_checkins_path(user_uuid: str, start_date: date, end_date: date, date_format: str) -> str:
    start_value = start_date.strftime(date_format)
    end_value = end_date.strftime(date_format)
    if "%H" not in date_format and "%M" not in date_format and "%S" not in date_format:
        start_value = f"{start_value}T00:00:00"
        end_value = f"{end_value}T23:59:59"
    return (
        f"/np/exercisers/{user_uuid}/check-ins/history"
        f"?startDate={start_value}&endDate={end_value}"
    )


def build_workouts_url(base_url: str, user_uuid: str, start_date: date, end_date: date) -> str:
    completed_after, completed_before = iso_utc_range(start_date, end_date)
    return (
        f"{base_url.rstrip('/')}/workouts/api/workouts/v2.3/exercisers/{user_uuid}/workouts"
        f"?completedAfter={completed_after}&completedBefore={completed_before}"
    )


def daterange_chunks(start_date: date, end_date: date, chunk_days: int) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    current = start_date
    while current <= end_date:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end_date)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return chunks


def env_default(primary: str, legacy: str | None = None) -> str | None:
    value = os.getenv(primary)
    if value:
        return value
    if legacy:
        return os.getenv(legacy)
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FFExport: export read-only member data from the Fitness First UK app backend."
    )
    parser.add_argument(
        "--email",
        default=env_default("FFEXPORT_EMAIL", "FITNESS_FIRST_EMAIL"),
        help="Member login email",
    )
    parser.add_argument(
        "--password",
        default=env_default("FFEXPORT_PASSWORD", "FITNESS_FIRST_PASSWORD"),
        help="Member password",
    )
    parser.add_argument(
        "--base-url",
        default=env_default("FFEXPORT_BASE_URL", "FITNESS_FIRST_BASE_URL") or DEFAULT_BASE_URL,
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--user-uuid", help="Override the member UUID if login response does not include it")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--start-date", help="Override query start date in YYYY-MM-DD format")
    parser.add_argument("--end-date", help="Override query end date in YYYY-MM-DD format")
    parser.add_argument(
        "--workout-chunk-days",
        type=int,
        default=DEFAULT_WORKOUT_CHUNK_DAYS,
        help="Fetch workouts in chunks of N days to avoid truncated wide-range responses",
    )
    return parser.parse_args()


def parse_date_arg(value: str, flag_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"{flag_name} must be in YYYY-MM-DD format") from exc


def export_simple_endpoints(
    client: FFExportClient,
    output_dir: Path,
    endpoints: dict[str, str],
) -> list[str]:
    failures: list[str] = []
    for name, path in endpoints.items():
        try:
            response = client.get(path)
        except RuntimeError as exc:
            failures.append(f"{name}: {exc}")
            continue
        write_json(output_dir / f"{name}.json", response.payload)
        rows = guess_rows(response.payload)
        if name == "schedule":
            write_csv(output_dir / "schedule.csv", flatten_schedule(rows))
        elif rows:
            write_csv(output_dir / f"{name}.csv", rows)
    return failures


def export_checkins(
    client: FFExportClient,
    output_dir: Path,
    user_uuid: str,
    start_date: date,
    end_date: date,
) -> list[str]:
    failures: list[str] = []
    checkins_date_formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%d-%m-%Y",
        "%m-%d-%Y",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
    ]
    for date_format in checkins_date_formats:
        path = build_checkins_path(user_uuid, start_date, end_date, date_format)
        try:
            response = client.get(path)
        except RuntimeError as exc:
            failures.append(f"checkins_history [{date_format}]: {exc}")
            continue
        rows = guess_rows(response.payload)
        write_json(output_dir / "checkins_history.json", response.payload)
        write_csv(output_dir / "checkins_history.csv", flatten_checkins(rows))
        write_csv(output_dir / "daily_attendance_summary.csv", summarize_checkins(rows))
        write_json(
            output_dir / "checkins_history_meta.json",
            {
                "date_format": date_format,
                "start_date": start_date.strftime(date_format),
                "end_date": end_date.strftime(date_format),
                "row_count": len(rows),
            },
        )
        return failures

    print("Visit history endpoint did not accept any attempted date format.", file=sys.stderr)
    return failures


def export_workouts(
    client: FFExportClient,
    output_dir: Path,
    base_url: str,
    user_uuid: str,
    start_date: date,
    end_date: date,
    chunk_days: int,
) -> None:
    workout_chunks = []
    merged_workouts: list[dict[str, Any]] = []
    seen_workouts: set[tuple[str, str]] = set()
    for chunk_start, chunk_end in daterange_chunks(start_date, end_date, max(1, chunk_days)):
        workouts_url = build_workouts_url(base_url, user_uuid, chunk_start, chunk_end)
        workouts_response = client.get_absolute(workouts_url, headers=workouts_headers())
        chunk_rows = guess_rows(workouts_response.payload)
        completed_after, completed_before = iso_utc_range(chunk_start, chunk_end)
        workout_chunks.append(
            {
                "completed_after": completed_after,
                "completed_before": completed_before,
                "row_count": len(chunk_rows),
            }
        )
        for workout in chunk_rows:
            key = (str(workout.get("code") or ""), str(workout.get("completedAt") or ""))
            if key in seen_workouts:
                continue
            seen_workouts.add(key)
            merged_workouts.append(workout)

    merged_workouts.sort(key=lambda item: item.get("completedAt") or "", reverse=True)
    workouts_payload = {"workouts": merged_workouts}
    completed_after, completed_before = iso_utc_range(start_date, end_date)
    write_json(output_dir / "workouts.json", workouts_payload)
    write_csv(output_dir / "workouts.csv", summarize_workouts(merged_workouts))
    write_csv(output_dir / "workout_exercises.csv", flatten_workout_exercises(merged_workouts))
    write_csv(output_dir / "machine_workout_sets.csv", flatten_machine_workout_sets(merged_workouts))
    write_json(
        output_dir / "workouts_meta.json",
        {
            "completed_after": completed_after,
            "completed_before": completed_before,
            "row_count": len(merged_workouts),
            "chunk_days": max(1, chunk_days),
            "chunks": workout_chunks,
        },
    )


def main() -> int:
    args = parse_args()
    if not args.email or not args.password:
        print(
            "Set FFEXPORT_EMAIL and FFEXPORT_PASSWORD, or pass --email and --password.",
            file=sys.stderr,
        )
        return 2

    ensure_output_dir(args.output_dir)

    client = FFExportClient(base_url=args.base_url)
    print(f"Logging into {args.base_url} ...", file=sys.stderr)
    login_response = client.login(args.email, args.password)
    write_json(args.output_dir / "login.json", login_response.payload)

    user_uuid = args.user_uuid or extract_user_uuid(login_response.payload)
    if not user_uuid:
        print(
            "Login succeeded but I could not find a member UUID in the response. "
            f"Inspect {args.output_dir / 'login.json'} and re-run with --user-uuid.",
            file=sys.stderr,
        )
        return 1

    summary = {
        "base_url": args.base_url,
        "user_uuid": user_uuid,
        "email": extract_email(login_response.payload) or args.email,
    }
    write_json(args.output_dir / "summary.json", summary)

    if args.end_date:
        end_date = parse_date_arg(args.end_date, "--end-date")
    else:
        end_date = date.today()

    if args.start_date:
        start_date = parse_date_arg(args.start_date, "--start-date")
    else:
        start_date = end_date - timedelta(days=args.lookback_days)

    if start_date > end_date:
        raise SystemExit("--start-date must be on or before --end-date")

    endpoints = {
        "profile": f"/np/exerciser/{user_uuid}",
        "membership": f"/np/exerciser/{user_uuid}/membership",
        "membership_barcode": f"/np/exerciser/{user_uuid}/membership-barcode",
        "schedule": f"/np/exerciser/{user_uuid}/schedule",
        "active_challenges": f"/np/exerciser/{user_uuid}/challenges/active",
        "activity_levels": f"/analysis/api/v1.0/exercisers/{user_uuid}/activitylevels",
        "ranking": f"/analysis/api/v1.0/exercisers/{user_uuid}/ranking",
        "egym_user_by_email": f"/np/egym/v1.0/users?email={args.email}",
        "egym_linking_status": f"/np/egym/v1.0/users/{user_uuid}/linking-status",
        "egym_opt_ins": f"/np/egym/v1.0/users/{user_uuid}/opt-ins",
        "external_workout_apps": f"/np/workouts/v2.1/exercisers/{user_uuid}/external-workouts/connected-apps",
    }

    failures = export_simple_endpoints(client, args.output_dir, endpoints)
    failures.extend(export_checkins(client, args.output_dir, user_uuid, start_date, end_date))
    try:
        export_workouts(
            client,
            args.output_dir,
            args.base_url,
            user_uuid,
            start_date,
            end_date,
            args.workout_chunk_days,
        )
    except RuntimeError as exc:
        failures.append(f"workouts: {exc}")

    if failures:
        print("Some endpoints failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)

    print(f"Export complete in {args.output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
