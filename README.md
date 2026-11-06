# FFExport

Read-only exporters for member data exposed by the Fitness First UK app and linked services.

This repo currently includes:

- `FFExport.py` for the Fitness First UK app's Netpulse / EGYM backend
- `EvoltExport.py` for linked Evolt Active body-composition data
- `GoogleHealthExport.py` for read-only personal exports from the Google Health API

`FFExport.py` logs in with your member credentials, saves raw JSON responses, and writes CSV summaries for:

- gym check-ins
- daily attendance
- booked classes
- connected workout apps
- workout history
- per-exercise workout rows
- per-set EGYM machine workout rows

## Requirements

- Python 3.10+
- no third-party dependencies

## Usage

```bash
export FFEXPORT_EMAIL="you@example.com"
export FFEXPORT_PASSWORD="your-password"
python3 FFExport.py
```

Useful options:

```bash
python3 FFExport.py --lookback-days 90
python3 FFExport.py --start-date 2026-01-01 --end-date 2026-08-02
python3 FFExport.py --workout-chunk-days 31
python3 FFExport.py --output-dir my_FFExport
python3 FFExport.py --base-url https://dwfitnessfirstcore.netpulse.com
```

If the login response does not expose the member UUID automatically:

```bash
python3 FFExport.py --user-uuid "<your-member-uuid>"
```

The workout-history endpoint can silently truncate very wide date-range requests. `FFExport.py` fetches workouts in date chunks and merges them, which makes older history available when the app can display it.

## Main outputs

- `login.json`: raw login response, including the member UUID when present
- `summary.json`: resolved base URL, UUID, and email
- `checkins_history.json` / `checkins_history.csv`: raw and flattened gym-visit history
- `daily_attendance_summary.csv`: one row per day with visit counts and first/last check-in
- `schedule.json` / `schedule.csv`: booked classes
- `workouts.json`: merged raw workout history across chunked requests
- `workouts_meta.json`: fetch window, chunk size, and row counts per chunk
- `workouts.csv`: one row per workout
- `workout_exercises.csv`: one row per exercise within a workout
- `machine_workout_sets.csv`: one row per EGYM machine set with reps, weight, duration, and recovery when available

## Evolt export

`EvoltExport.py` exports the body-composition data available through the linked Evolt Active account, including scan history and scan assets.

If your Fitness First UK account is linked to Evolt, you generally use the same email address for both systems, but you still need Evolt-specific credentials. In practice that means:

- log into the Evolt app or website separately
- use the same email address as your Fitness First UK account
- use your Evolt password, not your Fitness First UK password unless you have explicitly set them to match

The exporter talks to the Evolt Active web backend directly and currently pulls the data exposed by the member-facing web app, including:

- member profile metadata
- scan history
- scan notes
- macro-calculation responses
- supplementary questionnaire/reference endpoints
- scan PDFs or manual scan images when available

The most useful file for day-to-day analysis is usually `scans_summary.csv`, which flattens each scan into a simpler table with metrics such as scan date, body weight, lean mass, skeletal muscle mass, body fat, visceral fat, BMR, and total energy expenditure when those values are present in the API response.

Usage:

```bash
export EVOLT_EMAIL="you@example.com"
export EVOLT_PASSWORD="your-password"
python3 EvoltExport.py
```

Useful options:

```bash
python3 EvoltExport.py --output-dir my_EvoltExport
python3 EvoltExport.py --skip-assets
python3 EvoltExport.py --base-url https://webservice.evoltactive.com
```

If you only want the tabular data and do not care about attached PDFs/images, use `--skip-assets` to keep the export smaller and faster.

Main outputs:

- `login.json`: login response with tokens redacted before saving
- `summary.json`: resolved base URL, email, member ID, and scan count
- `member.json`: profile and account metadata
- `scans.json` / `scans.csv`: raw and flattened scan history
- `scans_summary.csv`: scan-focused summary with body weight, lean mass, body fat, BMR, and related metrics when exposed
- `scannotes.json` / `scannotes.csv`: scan notes where available
- `macronutrientcalculations.json`: macro-calculation response when available
- `scan_assets/`: downloaded scan PDFs or manual-scan images unless `--skip-assets` is used

## Google Health export

`GoogleHealthExport.py` exports your personal Google Health data through the current `health.googleapis.com` API using OAuth 2.0 and refresh tokens. It is designed for repeatable personal exports and follows the same read-only pattern as the other scripts in this repo.

The script always fetches:

- identity
- profile
- settings
- paired devices

It also exports selected data types from `users/me/dataTypes/*/dataPoints`.

By default it exports:

- `steps`
- `weight`
- `heart-rate`
- `sleep`
- `exercise`

Supported `--data-type` values today are:

- `active-energy-burned`
- `body-fat`
- `daily-resting-heart-rate`
- `distance`
- `exercise`
- `heart-rate`
- `sleep`
- `steps`
- `weight`

### Google Cloud setup

Before running the script, set up the Google Health API in Google Cloud:

- enable the Google Health API
- create an OAuth 2.0 client
- add your own Google account as a test user while the OAuth app is still in Testing
- add the Google Health scopes you need on the Data Access page

For this script, the simplest setup is to register a local redirect URI and download the credentials JSON. A practical redirect URI for the script is:

```text
http://127.0.0.1:8765/callback
```

### Usage

Using the downloaded credentials file:

```bash
export GOOGLE_HEALTH_CREDENTIALS_JSON="/path/to/credentials.json"
python3 GoogleHealthExport.py
```

Or with explicit client credentials:

```bash
export GOOGLE_HEALTH_CLIENT_ID="your-client-id"
export GOOGLE_HEALTH_CLIENT_SECRET="your-client-secret"
python3 GoogleHealthExport.py
```

Useful options:

```bash
python3 GoogleHealthExport.py --lookback-days 90
python3 GoogleHealthExport.py --start-date 2026-01-01 --end-date 2026-08-03
python3 GoogleHealthExport.py --data-type steps --data-type sleep
python3 GoogleHealthExport.py --chunk-days 90
python3 GoogleHealthExport.py --output-dir my_GoogleHealthExport
python3 GoogleHealthExport.py --token-path my_GoogleHealthExport/token.json
python3 GoogleHealthExport.py --force-consent
python3 GoogleHealthExport.py --no-browser
```

The first run opens a browser for OAuth consent, receives a refresh token, and saves it locally. Later runs refresh the access token automatically from the saved token file.

Main outputs:

- `identity.json`: Google Health user identity, including the Google Health user ID
- `profile.json`: profile fields exposed by the API
- `settings.json`: account settings such as units and timezone
- `paired_devices.json` / `paired_devices.csv`: paired tracker and smartwatch metadata
- `<data-type>.json`: raw export payload for each requested data type
- `<data-type>.csv`: flattened rows for each requested data type
- `<data-type>_meta.json`: chunk and paging metadata for each data type
- `summary.json`: overall export window and exported data-type counts

Additional informational endpoints such as FAQs, countries, supplement questions, and health-lifestyle questions are also exported when the account exposes them.

If some Evolt endpoints fail, the script still writes the successful responses and continues. That makes it useful both as a personal export tool and as a lightweight way to inspect what a particular account currently exposes.

## Notes

- Both scripts are intentionally read-only.
- It currently targets the Fitness First UK Netpulse host by default.
- Connected-app workouts and machine workouts can both appear in the workout history.
- If some endpoints fail, the successful JSON files are still useful for inspecting what your account exposes.
- EvoltExport is also intentionally read-only and currently targets the public Evolt Active webservice host used by the app.
- GoogleHealthExport is also intentionally read-only and stores its token file under the export directory by default.
- Export folders such as `FFExport/`, `EvoltExport/`, and `GoogleHealthExport/` are git-ignored so local data and tokens do not get committed by accident.
