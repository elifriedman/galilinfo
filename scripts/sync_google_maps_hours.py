"""Preview or update static directory hours using Google Places (New)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from bot.google_preview import (SyncError, convert_periods, get_place, load_api_key,
                                normalize_name, point_value, preview_hours, search_places, select_match)

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
        original = args.data.read_text(encoding="utf-8")
        places = json.loads(original)
        if not isinstance(places, list):
            raise SyncError("directory JSON must be an array")
        preview = preview_hours(places, place_id=args.place, google_place_id=args.google_place_id,
                                all_places=args.all, _load_key=load_api_key, _search=search_places, _get=get_place)
        updated_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        by_id = {p.get("id"): p for p in places if isinstance(p, dict)}
        for result in preview["results"]:
            place_id = result["place_id"]
            if result["direct"]:
                print(f"Google Places direct lookup for {place_id}:")
            else:
                print(f"Google Places search results for {place_id}:")
                for candidate in result["candidates"]:
                    display = candidate.get("displayName") or {}
                    print(f"  {candidate.get('id', '?')}: {display.get('text', '?') if isinstance(display, dict) else '?'} — {candidate.get('formattedAddress', '?')}")
            print(f"Selected: {result['google_place_id']} — {result['display_name']}")
            print("Weekly hours (Sunday first): " + json.dumps(result["hours"], ensure_ascii=False))
            if args.apply:
                by_id[place_id]["hours"] = result["hours"]
                by_id[place_id]["hoursUpdatedAt"] = updated_at
        for skip in preview["skipped"]:
            print(f"Skipped {skip['place_id']}: {skip['reason']}", file=sys.stderr)
        updated, failed = len(preview["results"]), len(preview["skipped"])
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
