# FFExport

Read-only exporters for member data exposed by the Fitness First UK app and linked services.

This repo currently includes:

- `FFExport.py` for the Fitness First UK app's Netpulse / EGYM backend
- `EvoltExport.py` for linked Evolt Active body-composition data

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

Additional informational endpoints such as FAQs, countries, supplement questions, and health-lifestyle questions are also exported when the account exposes them.

If some Evolt endpoints fail, the script still writes the successful responses and continues. That makes it useful both as a personal export tool and as a lightweight way to inspect what a particular account currently exposes.

## Notes

- Both scripts are intentionally read-only.
- It currently targets the Fitness First UK Netpulse host by default.
- Connected-app workouts and machine workouts can both appear in the workout history.
- If some endpoints fail, the successful JSON files are still useful for inspecting what your account exposes.
- EvoltExport is also intentionally read-only and currently targets the public Evolt Active webservice host used by the app.
- Export folders such as `FFExport/` and `EvoltExport/` are git-ignored so local data and tokens do not get committed by accident.
