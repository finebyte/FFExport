# FFExport

Read-only exporter for member data exposed by the Fitness First UK app's Netpulse / EGYM backend.

It logs in with your member credentials, saves raw JSON responses, and writes CSV summaries for:

- gym check-ins
- daily attendance
- booked classes
- connected workout apps
- workout history
- per-exercise workout rows
- per-set EGYM machine workout rows

## Why chunked workout fetches matter

The workout-history endpoint can silently truncate very wide date-range requests. This exporter fetches workouts in date chunks and merges them, which makes older history available when the app can display it.

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

## Notes

- FFExport is intentionally read-only.
- It currently targets the Fitness First UK Netpulse host by default.
- Connected-app workouts and machine workouts can both appear in the workout history.
- If some endpoints fail, the successful JSON files are still useful for inspecting what your account exposes.
