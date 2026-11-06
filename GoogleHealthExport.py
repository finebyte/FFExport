#!/usr/bin/env python3
"""Export read-only personal data from the Google Health API."""

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
from export_storage import atomic_write_text, atomic_csv
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen


AUTH_BASE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
API_BASE_URL = "https://health.googleapis.com"
DEFAULT_OUTPUT_DIR = Path("GoogleHealthExport")
DEFAULT_TOKEN_PATH = DEFAULT_OUTPUT_DIR / "token.json"
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_CHUNK_DAYS = 30
MAX_CHUNK_DAYS = 90
DEFAULT_PAGE_SIZE = 1000
DEFAULT_SLEEP_PAGE_SIZE = 25
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
LOCAL_SERVER_TIMEOUT_SECONDS = 300

SCOPE_ACTIVITY = "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"
SCOPE_HEALTH = "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
SCOPE_SLEEP = "https://www.googleapis.com/auth/googlehealth.sleep.readonly"
SCOPE_PROFILE = "https://www.googleapis.com/auth/googlehealth.profile.readonly"
SCOPE_SETTINGS = "https://www.googleapis.com/auth/googlehealth.settings.readonly"
SCOPE_OPENID = "openid"
SCOPE_EMAIL = "https://www.googleapis.com/auth/userinfo.email"


@dataclass(frozen=True)
class DataTypeSpec:
    name: str
    scope: str
    filter_field: str
    filter_value_kind: str
    page_size: int = DEFAULT_PAGE_SIZE


DATA_TYPE_SPECS: dict[str, DataTypeSpec] = {
    "steps": DataTypeSpec("steps", SCOPE_ACTIVITY, "steps.interval.start_time", "rfc3339"),
    "distance": DataTypeSpec("distance", SCOPE_ACTIVITY, "distance.interval.start_time", "rfc3339"),
    "active-energy-burned": DataTypeSpec(
        "active-energy-burned",
        SCOPE_ACTIVITY,
        "active_energy_burned.interval.start_time",
        "rfc3339",
    ),
    "exercise": DataTypeSpec("exercise", SCOPE_ACTIVITY, "exercise.interval.civil_start_time", "civil", 25),
    "heart-rate": DataTypeSpec("heart-rate", SCOPE_HEALTH, "heart_rate.sample_time.physical_time", "rfc3339"),
    "weight": DataTypeSpec("weight", SCOPE_HEALTH, "weight.sample_time.physical_time", "rfc3339"),
    "body-fat": DataTypeSpec("body-fat", SCOPE_HEALTH, "body_fat.sample_time.physical_time", "rfc3339"),
    "blood-glucose": DataTypeSpec("blood-glucose", SCOPE_HEALTH, "blood_glucose.sample_time.physical_time", "rfc3339"),
    "sleep": DataTypeSpec("sleep", SCOPE_SLEEP, "sleep.interval.end_time", "rfc3339", DEFAULT_SLEEP_PAGE_SIZE),
    "daily-resting-heart-rate": DataTypeSpec(
        "daily-resting-heart-rate",
        SCOPE_HEALTH,
        "daily_resting_heart_rate.date",
        "date",
    ),
}


def env_default(primary: str, legacy: str | None = None) -> str | None:
    value = os.getenv(primary)
    if value:
        return value
    if legacy:
        return os.getenv(legacy)
    return None


def ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=True))


def normalize_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=True)


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
    atomic_csv(path, fieldnames, ({key: normalize_scalar(row.get(key)) for key in fieldnames} for row in rows))


def parse_date_arg(value: str, flag_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"{flag_name} must be in YYYY-MM-DD format") from exc


def chunk_ranges(start_date: date, end_date: date, chunk_days: int) -> list[tuple[date, date]]:
    if chunk_days < 1 or chunk_days > MAX_CHUNK_DAYS:
        raise SystemExit(f"--chunk-days must be between 1 and {MAX_CHUNK_DAYS}")
    chunks: list[tuple[date, date]] = []
    current = start_date
    while current <= end_date:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end_date)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return chunks


def format_range_value(kind: str, current_date: date, end_of_range: bool) -> str:
    if kind == "date":
        return current_date.isoformat()
    if kind == "civil":
        suffix = "T23:59:59" if end_of_range else "T00:00:00"
        return f"{current_date.isoformat()}{suffix}"
    dt = datetime.combine(
        current_date,
        datetime.max.time().replace(microsecond=0) if end_of_range else datetime.min.time(),
        tzinfo=timezone.utc,
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_filter(spec: DataTypeSpec, start_date: date, end_date: date) -> str:
    start_value = format_range_value(spec.filter_value_kind, start_date, end_of_range=False)
    next_day = end_date + timedelta(days=1)
    end_value = format_range_value(spec.filter_value_kind, next_day, end_of_range=False)
    return f'{spec.filter_field} >= "{start_value}" AND {spec.filter_field} < "{end_value}"'


def to_google_camel(data_type: str) -> str:
    parts = data_type.split("-")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


def flatten_data_points(data_type: str, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    union_key = to_google_camel(data_type)
    rows: list[dict[str, Any]] = []
    for point in points:
        flat = flatten_object(point)
        preferred: dict[str, Any] = {
            "name": point.get("name"),
            "data_source.platform": flat.get("dataSource.platform"),
            "data_source.package_name": flat.get("dataSource.application.packageName"),
            "data_source.google_web_client_id": flat.get("dataSource.application.googleWebClientId"),
        }
        for key, value in flat.items():
            normalized_key = key
            if key.startswith(f"{union_key}."):
                normalized_key = key.replace(f"{union_key}.", "", 1)
            elif key.startswith("dataSource."):
                normalized_key = f"data_source.{key.split('.', 1)[1]}"
            preferred[normalized_key] = value
        rows.append(preferred)
    return rows


def load_credentials(args: argparse.Namespace) -> tuple[str, str]:
    client_id = args.client_id or env_default("GOOGLE_HEALTH_CLIENT_ID")
    client_secret = args.client_secret or env_default("GOOGLE_HEALTH_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret

    credentials_path = args.credentials_json or env_default("GOOGLE_HEALTH_CREDENTIALS_JSON")
    if not credentials_path:
        raise SystemExit(
            "Provide --client-id and --client-secret, or set GOOGLE_HEALTH_CLIENT_ID and "
            "GOOGLE_HEALTH_CLIENT_SECRET, or point GOOGLE_HEALTH_CREDENTIALS_JSON at the downloaded credentials JSON."
        )

    payload = json.loads(Path(credentials_path).read_text(encoding="utf-8"))
    for section in ("web", "installed"):
        if section in payload:
            credentials = payload[section]
            found_client_id = credentials.get("client_id")
            found_client_secret = credentials.get("client_secret")
            if found_client_id and found_client_secret:
                return str(found_client_id), str(found_client_secret)
    raise SystemExit("Could not find client_id/client_secret in the credentials JSON.")


def load_token(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_token(path: Path, payload: dict[str, Any]) -> None:
    serializable = dict(payload)
    if "expires_at" not in serializable and "expires_in" in serializable:
        serializable["expires_at"] = int(time.time()) + int(serializable["expires_in"])
    write_json(path, serializable)


def base64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    server_version = "GoogleHealthExport/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        self.server.auth_code = params.get("code", [None])[0]  # type: ignore[attr-defined]
        self.server.auth_error = params.get("error", [None])[0]  # type: ignore[attr-defined]

        if self.server.auth_code:  # type: ignore[attr-defined]
            body = "Authorization received. You can close this window."
            self.send_response(200)
        else:
            body = "Authorization failed. You can close this window and inspect the terminal."
            self.send_response(400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def request_authorization_code(
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    open_browser: bool,
    force_consent: bool,
) -> str:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port is None:
        raise SystemExit(
            "This script currently supports local OAuth redirects only. Use a redirect URI like "
            "http://127.0.0.1:8765/callback and add it to your Google Cloud OAuth client."
        )

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64url_sha256(code_verifier)
    state = secrets.token_urlsafe(24)

    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if force_consent:
        auth_params["prompt"] = "consent"
    auth_url = f"{AUTH_BASE_URL}?{urlencode(auth_params, quote_via=quote)}"

    server = HTTPServer((parsed.hostname, parsed.port), OAuthCallbackHandler)
    server.timeout = 1
    server.auth_code = None  # type: ignore[attr-defined]
    server.auth_error = None  # type: ignore[attr-defined]

    stop_event = threading.Event()

    def serve() -> None:
        while not stop_event.is_set() and not server.auth_code and not server.auth_error:  # type: ignore[attr-defined]
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    print("Open this URL in your browser to authorize Google Health access:", file=sys.stderr)
    print(auth_url, file=sys.stderr)
    if open_browser:
        webbrowser.open(auth_url)

    deadline = time.time() + LOCAL_SERVER_TIMEOUT_SECONDS
    while time.time() < deadline:
        if server.auth_error:  # type: ignore[attr-defined]
            stop_event.set()
            raise SystemExit(f"OAuth authorization failed: {server.auth_error}")  # type: ignore[attr-defined]
        if server.auth_code:  # type: ignore[attr-defined]
            code = str(server.auth_code)  # type: ignore[attr-defined]
            stop_event.set()
            return code, code_verifier
        time.sleep(0.25)

    stop_event.set()
    raise SystemExit("Timed out waiting for the OAuth redirect.")


def form_post(url: str, data: dict[str, str]) -> dict[str, Any]:
    body = urlencode(data).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed with HTTP {exc.code}: {raw[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"POST {url} failed: {exc.reason}") from exc
    return json.loads(raw)


def fetch_google_identity(id_token: str, client_id: str, allowed_email: str) -> dict[str, str]:
    """Validate the identity returned by OAuth and enforce the local allowlist."""
    request = Request(
        f"{TOKENINFO_URL}?{urlencode({'id_token': id_token})}",
        headers={"Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            identity = json.loads(response.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not validate the Google account identity: {exc}") from exc

    if identity.get("aud") != client_id:
        raise SystemExit("Google OAuth identity was issued for a different client.")
    if identity.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        raise SystemExit("Google OAuth identity has an unexpected issuer.")
    if str(identity.get("email_verified", "")).lower() != "true":
        raise SystemExit("The authorised Google email address is not verified.")

    email = str(identity.get("email", "")).strip().casefold()
    if not email or email != allowed_email.strip().casefold():
        raise SystemExit(f"Unauthorised Google account: {email or 'unknown account'}")

    return {"google_email": email, "google_sub": str(identity.get("sub", ""))}


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, Any]:
    payload = form_post(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    payload["refresh_token"] = payload.get("refresh_token") or refresh_token
    payload["expires_at"] = int(time.time()) + int(payload.get("expires_in", 3600))
    return payload


def exchange_code_for_tokens(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> dict[str, Any]:
    payload = form_post(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
        },
    )
    payload["expires_at"] = int(time.time()) + int(payload.get("expires_in", 3600))
    return payload


def scopes_cover(token_payload: dict[str, Any], required_scopes: list[str]) -> bool:
    granted = set(str(token_payload.get("scope", "")).split())
    return all(scope in granted for scope in required_scopes)


def ensure_access_token(
    client_id: str,
    client_secret: str,
    token_path: Path,
    redirect_uri: str,
    scopes: list[str],
    open_browser: bool,
    force_consent: bool,
    allowed_email: str,
) -> dict[str, Any]:
    token = load_token(token_path)
    now = int(time.time())
    token_identity_matches = bool(
        token
        and token.get("google_email")
        and str(token["google_email"]).strip().casefold() == allowed_email.strip().casefold()
    )
    if token and token.get("refresh_token") and token.get("google_email") and not token_identity_matches:
        raise SystemExit("The saved Google token belongs to a different account than GOOGLE_HEALTH_ALLOWED_EMAIL.")

    if token and token.get("refresh_token") and token_identity_matches:
        expires_at = int(token.get("expires_at", 0))
        if expires_at <= now + 60 or not scopes_cover(token, scopes):
            refreshed = refresh_access_token(client_id, client_secret, str(token["refresh_token"]))
            # Google refresh responses do not repeat our local account binding.
            # Preserve it so a later headless run does not mistake a valid token
            # for an unverified account and start interactive OAuth.
            for key in ("google_email", "google_sub"):
                if token.get(key):
                    refreshed[key] = token[key]
            if scopes_cover(refreshed, scopes):
                save_token(token_path, refreshed)
                return refreshed
            token = refreshed
        elif scopes_cover(token, scopes):
            return token

    code, code_verifier = request_authorization_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        open_browser=open_browser,
        force_consent=force_consent or token is None or not token_identity_matches,
    )
    token = exchange_code_for_tokens(client_id, client_secret, redirect_uri, code, code_verifier)
    id_token = str(token.get("id_token") or "")
    if not id_token:
        raise SystemExit("OAuth did not return an identity token; reauthorisation cannot be restricted safely.")
    token.update(fetch_google_identity(id_token, client_id, allowed_email))
    save_token(token_path, token)
    return token


class GoogleHealthClient:
    def __init__(self, access_token: str, base_url: str = API_BASE_URL) -> None:
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = f"?{urlencode(params, quote_via=quote)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        for attempt in range(4):
            delay = 2 ** (attempt + 1)
            try:
                with urlopen(request, timeout=60) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw)
            except HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                diagnostics = {
                    key: exc.headers[key]
                    for key in ("Retry-After", "X-Request-Id", "X-Goog-Request-Id", "X-Cloud-Trace-Context")
                    if exc.headers and exc.headers.get(key)
                }
                print(f"Google Health GET {path}: HTTP {exc.code}; attempt {attempt + 1}/4; diagnostics={json.dumps(diagnostics)}", file=sys.stderr, flush=True)
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError(f"GET {path} failed with HTTP {exc.code}: {raw[:500]}") from exc
                retry_after = diagnostics.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = min(120, max(delay, int(retry_after)))
            except (URLError, TimeoutError) as exc:
                if attempt == 3:
                    raise RuntimeError(f"GET {path} failed after 4 connection attempts") from exc
                print(f"Google Health GET {path}: connection failure; attempt {attempt + 1}/4", file=sys.stderr, flush=True)
            print(f"Retrying the same Google Health request in {delay}s", file=sys.stderr, flush=True)
            time.sleep(delay)

    def get_identity(self) -> Any:
        return self._request("/v4/users/me/identity")

    def get_profile(self) -> Any:
        return self._request("/v4/users/me/profile")

    def get_settings(self) -> Any:
        return self._request("/v4/users/me/settings")

    def list_paired_devices(self) -> Any:
        return self._request("/v4/users/me/pairedDevices", {"pageSize": 100})

    def list_data_points(
        self,
        data_type: str,
        filter_expression: str,
        page_size: int,
        page_token: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "filter": filter_expression,
            "pageSize": page_size,
        }
        if page_token:
            params["pageToken"] = page_token
        return self._request(f"/v4/users/me/dataTypes/{data_type}/dataPoints", params)


def collect_metadata(client: GoogleHealthClient, output_dir: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    identity = client.get_identity()
    metadata["identity"] = identity
    write_json(output_dir / "identity.json", identity)

    profile = client.get_profile()
    metadata["profile"] = profile
    write_json(output_dir / "profile.json", profile)

    settings = client.get_settings()
    metadata["settings"] = settings
    write_json(output_dir / "settings.json", settings)

    paired_devices = client.list_paired_devices()
    metadata["paired_devices"] = paired_devices
    write_json(output_dir / "paired_devices.json", paired_devices)
    devices = paired_devices.get("pairedDevices") or []
    if devices:
        write_csv(output_dir / "paired_devices.csv", [flatten_object(device) for device in devices if isinstance(device, dict)])

    return metadata


def export_data_type(
    client: GoogleHealthClient,
    output_dir: Path,
    spec: DataTypeSpec,
    start_date: date,
    end_date: date,
    chunk_days: int,
) -> dict[str, Any]:
    points: list[dict[str, Any]] = []
    chunks_meta: list[dict[str, Any]] = []

    for chunk_start, chunk_end in chunk_ranges(start_date, end_date, chunk_days):
        filter_expression = build_filter(spec, chunk_start, chunk_end)
        chunk_count = 0
        page_token: str | None = None
        page_count = 0
        seen_tokens: set[str] = set()
        while True:
            try:
                response = client.list_data_points(spec.name, filter_expression, spec.page_size, page_token)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"{spec.name} export failed on page {page_count + 1}, "
                    f"window {chunk_start.isoformat()} to {chunk_end.isoformat()}, "
                    f"after {chunk_count} records: {exc}"
                ) from exc
            batch = response.get("dataPoints", [])
            if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                raise RuntimeError(f"Google {spec.name}: malformed dataPoints response")
            points.extend(batch)
            chunk_count += len(batch)
            page_count += 1
            page_token = response.get("nextPageToken")
            if not page_token:
                break
            if page_token in seen_tokens or page_count >= 10000:
                raise RuntimeError(f"Google {spec.name}: repeated page token or pagination limit reached")
            seen_tokens.add(page_token)
        chunks_meta.append(
            {
                "start_date": chunk_start.isoformat(),
                "end_date": chunk_end.isoformat(),
                "filter": filter_expression,
                "page_size": spec.page_size,
                "pages": page_count,
                "data_points": chunk_count,
            }
        )

    raw_payload = {
        "data_type": spec.name,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "chunks": chunks_meta,
        "dataPoints": points,
    }
    write_json(output_dir / f"{spec.name}.json", raw_payload)
    flattened = flatten_data_points(spec.name, points)
    if flattened:
        write_csv(output_dir / f"{spec.name}.csv", flattened)
    write_json(output_dir / f"{spec.name}_meta.json", {"data_type": spec.name, "chunks": chunks_meta, "count": len(points)})
    return {"data_type": spec.name, "count": len(points), "chunks": len(chunks_meta)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export read-only personal data from the Google Health API.")
    parser.add_argument("--client-id", help="Google OAuth client ID")
    parser.add_argument("--client-secret", help="Google OAuth client secret")
    parser.add_argument("--credentials-json", help="Path to the downloaded Google OAuth credentials JSON")
    parser.add_argument("--redirect-uri", default=env_default("GOOGLE_HEALTH_REDIRECT_URI") or DEFAULT_REDIRECT_URI)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--token-path", type=Path, default=DEFAULT_TOKEN_PATH)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--start-date", help="Override query start date in YYYY-MM-DD format")
    parser.add_argument("--end-date", help="Override query end date in YYYY-MM-DD format")
    parser.add_argument(
        "--data-type",
        action="append",
        dest="data_types",
        help="Repeat for each data type to export. Supported: " + ", ".join(sorted(DATA_TYPE_SPECS)),
    )
    parser.add_argument("--chunk-days", type=int, default=DEFAULT_CHUNK_DAYS)
    parser.add_argument("--no-browser", action="store_true", help="Do not try to open the OAuth URL automatically")
    parser.add_argument("--force-consent", action="store_true", help="Force the Google consent screen")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_output_dir(args.output_dir)
    ensure_output_dir(args.token_path.parent)

    end_date = parse_date_arg(args.end_date, "--end-date") if args.end_date else date.today()
    start_date = (
        parse_date_arg(args.start_date, "--start-date")
        if args.start_date
        else end_date - timedelta(days=max(args.lookback_days - 1, 0))
    )
    if start_date > end_date:
        raise SystemExit("--start-date must be on or before --end-date")

    requested_types = args.data_types or [
        "steps",
        "weight",
        "heart-rate",
        "sleep",
        "exercise",
        "blood-glucose",
        "distance",
        "active-energy-burned",
        "body-fat",
        "daily-resting-heart-rate",
    ]
    unknown = [item for item in requested_types if item not in DATA_TYPE_SPECS]
    if unknown:
        raise SystemExit(f"Unsupported data type(s): {', '.join(unknown)}")

    specs = [DATA_TYPE_SPECS[name] for name in requested_types]
    allowed_email = env_default("GOOGLE_HEALTH_ALLOWED_EMAIL")
    if not allowed_email:
        raise SystemExit("Set GOOGLE_HEALTH_ALLOWED_EMAIL in .env before authorising Google Health.")

    scopes = [SCOPE_OPENID, SCOPE_EMAIL, SCOPE_PROFILE, SCOPE_SETTINGS]
    for spec in specs:
        if spec.scope not in scopes:
            scopes.append(spec.scope)

    client_id, client_secret = load_credentials(args)
    token = ensure_access_token(
        client_id=client_id,
        client_secret=client_secret,
        token_path=args.token_path,
        redirect_uri=args.redirect_uri,
        scopes=scopes,
        open_browser=not args.no_browser,
        force_consent=args.force_consent,
        allowed_email=allowed_email,
    )

    access_token = str(token.get("access_token") or "")
    if not access_token:
        print("OAuth token response did not contain an access_token.", file=sys.stderr)
        return 1

    client = GoogleHealthClient(access_token)

    print("Fetching identity, profile, settings, and paired devices ...", file=sys.stderr)
    metadata = collect_metadata(client, args.output_dir)

    summaries: list[dict[str, Any]] = []
    failures: list[str] = []
    for spec in specs:
        print(f"Exporting {spec.name} from {start_date} to {end_date} ...", file=sys.stderr)
        try:
            summaries.append(export_data_type(client, args.output_dir, spec, start_date, end_date, args.chunk_days))
        except (RuntimeError, ValueError, OSError) as exc:
            failures.append(f"{spec.name}: {exc}")
            print(failures[-1], file=sys.stderr)

    summary = {
        "base_url": API_BASE_URL,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "redirect_uri": args.redirect_uri,
        "data_types": summaries,
        "health_user_id": metadata.get("identity", {}).get("healthUserId"),
        "legacy_user_id": metadata.get("identity", {}).get("legacyUserId"),
        "token_path": str(args.token_path),
        "failures": failures,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(args.output_dir / "summary.json", summary)

    print(f"Export complete in {args.output_dir}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
