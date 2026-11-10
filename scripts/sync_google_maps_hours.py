"""Preview or update one static directory entry using Google Places (New)."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.regularOpeningHours"
PLACE_FIELD_MASK = "id,displayName,formattedAddress,regularOpeningHours"
KEY_NAME = "GOOGLE_MAPS_PLACES_API_KEY"


class SyncError(Exception):
    pass


def load_api_key() -> str:
    key = os.environ.get(KEY_NAME, "").strip()
    if key:
        return key
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"\s*(?:export\s+)?" + KEY_NAME + r"\s*=\s*(.*?)\s*", line)
            if match:
                value = match.group(1)
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if value:
                    return value
    raise SyncError(f"{KEY_NAME} is not configured in the environment or .env")


def search_places(place: dict, api_key: str) -> list[dict]:
    query = ", ".join(str(place.get(key, "")).strip() for key in ("name", "google-search-name", "address") if place.get(key))
    request = Request(SEARCH_URL, data=json.dumps({"textQuery": query, "languageCode": "he", "regionCode": "IL", "pageSize": 5}).encode(),
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK}, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            data = json.load(response)
    except HTTPError as exc:
        raise SyncError(f"Places API request failed (HTTP {exc.code})") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SyncError(f"Places API request failed: {getattr(exc, 'reason', type(exc).__name__)}") from exc
    except (ValueError, UnicodeError) as exc:
        raise SyncError("Places API returned invalid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("places", []), list):
        raise SyncError("Places API returned an unexpected response")
    return data.get("places", [])


def get_place(google_place_id: str, api_key: str) -> dict:
    url = f"https://places.googleapis.com/v1/places/{quote(google_place_id, safe='')}"
    request = Request(url, headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": PLACE_FIELD_MASK})
    try:
        with urlopen(request, timeout=30) as response:
            data = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise SyncError(f"Places API place lookup failed (HTTP {exc.code}) for {google_place_id}{suffix}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SyncError(f"Places API request failed: {getattr(exc, 'reason', type(exc).__name__)}") from exc
    except (ValueError, UnicodeError) as exc:
        raise SyncError("Places API returned invalid JSON") from exc
    if not isinstance(data, dict) or data.get("id") != google_place_id:
        raise SyncError("Places API returned an unexpected place")
    return data


def normalize_name(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", unicodedata.normalize("NFKC", text).casefold()).split())


def select_match(place: dict, candidates: list[dict], google_place_id: str | None) -> dict:
    if google_place_id:
        matches = [candidate for candidate in candidates if candidate.get("id") == google_place_id]
    else:
        name = normalize_name(place["name"])
        locality = normalize_name(str(place.get("address", "")).split(",")[-1])
        matches = []
        for candidate in candidates:
            display = candidate.get("displayName") or {}
            candidate_name = normalize_name(display.get("text", "")) if isinstance(display, dict) else ""
            address = normalize_name(candidate.get("formattedAddress", ""))
            if name and name in candidate_name and locality and locality in address:
                matches.append(candidate)
    if len(matches) == 0:
        raise SyncError(f"could not identify any matching places; inspect candidates and pass --google-place-id: {'\n'.join(map(str, candidates))}")
    if len(matches) != 1:
        raise SyncError(f"could not identify exactly one matching place; inspect candidates and pass --google-place-id: {'\n'.join(map(str, matches))}")
    return matches[0]


def point_value(point: object) -> tuple[int, int]:
    if not isinstance(point, dict):
        raise SyncError("invalid opening-hours point")
    # Proto3 JSON omits fields whose integer value is zero (Sunday or midnight).
    day, hour, minute = (point.get(name, 0) for name in ("day", "hour", "minute"))
    if any(type(value) is not int for value in (day, hour, minute)) or not (0 <= day <= 6 and 0 <= hour <= 23 and 0 <= minute <= 59):
        raise SyncError("invalid opening-hours day or time")
    if point.get("truncated"):
        raise SyncError("truncated hours cannot represent a regular weekly schedule")
    return day, hour * 60 + minute


def convert_periods(opening_hours: object) -> list[list[dict[str, str]]]:
    if not isinstance(opening_hours, dict) or not isinstance(opening_hours.get("periods"), list):
        raise SyncError("regular weekly hours are missing; existing hours were left unchanged")
    periods = opening_hours["periods"]
    schedule: list[list[dict[str, str]]] = [[] for _ in range(7)]
    if len(periods) == 1 and isinstance(periods[0], dict) and "close" not in periods[0]:
        if point_value(periods[0].get("open")) != (0, 0):
            raise SyncError("unsupported open-ended schedule")
        return [[{"opens": "00:00", "closes": "00:00"}] for _ in range(7)]
    intervals: list[tuple[int, int]] = []
    for period in periods:
        if not isinstance(period, dict):
            raise SyncError("invalid opening-hours period")
        day, start = point_value(period.get("open"))
        end_day, end = point_value(period.get("close"))
        day_delta = (end_day - day) % 7
        if not ((day_delta == 0 and end > start) or (day_delta == 1 and end <= start)):
            raise SyncError("schedule has a period longer than 24 hours or an inconsistent close day")
        absolute_start = day * 1440 + start
        absolute_end = absolute_start + day_delta * 1440 + end - start
        intervals.append((absolute_start, absolute_end))
        schedule[day].append({"opens": f"{start // 60:02d}:{start % 60:02d}", "closes": f"{end // 60:02d}:{end % 60:02d}"})
    # Check for overlaps across midnight and the Saturday/Sunday boundary.
    week = 7 * 1440
    ordered = sorted(intervals + [(start + week, end + week) for start, end in intervals])
    if any(ordered[i][0] < ordered[i - 1][1] for i in range(1, len(ordered))):
        raise SyncError("overlapping weekly hours")
    for day in schedule:
        day.sort(key=lambda period: period["opens"])
    return schedule


def write_update(path: Path, original: str, places: list[dict]) -> None:
    if path.read_text(encoding="utf-8") != original:
        raise SyncError("directory changed while fetching hours; rerun to avoid overwriting edits")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".places-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(places, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--place", help="Directory place id (for example cafe-bela)")
    selection.add_argument("--all", action="store_true", help="Sync every directory entry with a saved Google Place ID")
    parser.add_argument("--google-place-id", help="Use this Google Place ID instead of the entry's saved google-search-id")
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "places.json", help="Directory JSON path")
    parser.add_argument("--apply", action="store_true", help="Save the matched regular schedule (default is preview only)")
    args = parser.parse_args()
    try:
        if args.all and args.google_place_id:
            raise SyncError("--google-place-id can only be used with --place")
        original = args.data.read_text(encoding="utf-8")
        places = json.loads(original)
        if not isinstance(places, list):
            raise SyncError("directory JSON must be an array")
        api_key = load_api_key()
        if args.all:
            selected = [
                place for place in places
                if isinstance(place, dict) and isinstance(place.get("google-search-id"), str) and place["google-search-id"].strip()
            ]
            if not selected:
                raise SyncError("no directory entries have a saved google-search-id")
        else:
            selected = [place for place in places if isinstance(place, dict) and place.get("id") == args.place]
            if len(selected) != 1 or not isinstance(selected[0].get("name"), str):
                raise SyncError(f"directory place id {args.place!r} must match exactly one named entry")

        updated = 0
        failed = 0
        updated_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        for place in selected:
            place_id = place.get("id", "?")
            google_place_id = args.google_place_id or place.get("google-search-id")
            try:
                if google_place_id:
                    match = get_place(google_place_id, api_key)
                    print(f"Google Places direct lookup for {place_id}:")
                else:
                    candidates = search_places(place, api_key)
                    if not candidates:
                        raise SyncError("Places API found no matching places")
                    print(f"Google Places search results for {place_id}:")
                    for candidate in candidates:
                        display = candidate.get("displayName") or {}
                        print(f"  {candidate.get('id', '?')}: {display.get('text', '?') if isinstance(display, dict) else '?'} — {candidate.get('formattedAddress', '?')}")
                    match = select_match(place, candidates, None)
                hours = convert_periods(match.get("regularOpeningHours"))
            except SyncError as exc:
                failed += 1
                print(f"Skipped {place_id}: {exc}", file=sys.stderr)
                continue

            display = match.get("displayName") or {}
            display_name = display.get("text", "?") if isinstance(display, dict) else "?"
            print(f"Selected: {match.get('id')} — {display_name}")
            print("Weekly hours (Sunday first): " + json.dumps(hours, ensure_ascii=False))
            if args.apply:
                place["hours"] = hours
                place["hoursUpdatedAt"] = updated_at
            updated += 1

        if args.apply and updated:
            write_update(args.data, original, places)
            print(f"Updated {updated} place(s) in {args.data}")
        elif args.apply:
            print("No places were updated.")
        else:
            print(f"Preview only; {updated} place(s) ready to update. Use --apply to save.")
        if failed:
            print(f"{failed} place(s) could not be synced.", file=sys.stderr)
        return 1 if failed else 0
    except (SyncError, OSError, ValueError) as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
