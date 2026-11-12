"""Validation and approval-time commits for the public places JSON directory."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EDITABLE_FIELDS = frozenset({
    "name", "type", "address", "instagram", "description", "google-search-name",
    "google-search-id",
})
NEW_RECORD_FIELDS = EDITABLE_FIELDS | {"id", "hours", "hoursUpdatedAt"}
OPTIONAL_STRINGS = frozenset({"instagram", "description", "google-search-name", "google-search-id"})
_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")


class DirectoryConflict(ValueError):
    """The reviewed change no longer matches the current directory."""


def _string(value: Any, label: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValueError(f"{label} must be a{' non-empty' if nonempty else ''} string")


def validate_hours(hours: Any) -> None:
    if not isinstance(hours, list) or len(hours) != 7:
        raise ValueError("hours must contain seven Sunday-through-Saturday slots")
    for day, slot in enumerate(hours):
        if slot is None:
            continue
        if not isinstance(slot, list):
            raise ValueError(f"hours slot {day} must be null or an interval list")
        for interval in slot:
            if not isinstance(interval, dict) or set(interval) != {"opens", "closes"}:
                raise ValueError(f"hours slot {day} contains an invalid interval")
            if any(not isinstance(interval[key], str) or not _TIME.fullmatch(interval[key]) for key in ("opens", "closes")):
                raise ValueError(f"hours slot {day} times must use HH:MM")


def validate_place(place: Any) -> None:
    if not isinstance(place, dict):
        raise ValueError("each place must be an object")
    _string(place.get("id"), "id", nonempty=True)
    _string(place.get("name"), "name", nonempty=True)
    _string(place.get("type"), "type", nonempty=True)
    _string(place.get("address"), "address")
    for key in OPTIONAL_STRINGS:
        if key in place and place[key] is not None:
            _string(place[key], key)
    if "hoursUpdatedAt" in place and place["hoursUpdatedAt"] is not None:
        _string(place["hoursUpdatedAt"], "hoursUpdatedAt")
        try:
            parsed = datetime.fromisoformat(place["hoursUpdatedAt"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("hoursUpdatedAt must be an ISO timestamp or null") from exc
        if parsed.utcoffset() is None:
            raise ValueError("hoursUpdatedAt must include a timezone")
    validate_hours(place.get("hours"))


def validate_places(places: Any) -> list[dict[str, Any]]:
    if not isinstance(places, list):
        raise ValueError("places JSON must be an array")
    ids: set[str] = set()
    for place in places:
        validate_place(place)
        if place["id"] in ids:
            raise ValueError("place IDs must be unique")
        ids.add(place["id"])
    return places


def load_places(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as stream:
        return validate_places(json.load(stream))


def validate_patch(patch: dict[str, Any]) -> None:
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    forbidden = set(patch) - EDITABLE_FIELDS - {"hours_days"}
    if forbidden:
        raise ValueError(f"fields are not editable: {', '.join(sorted(forbidden))}")
    for key, value in patch.items():
        if key == "hours_days":
            if not isinstance(value, dict) or not value:
                raise ValueError("hours_days must map weekday indexes to schedules")
            for day, schedule in value.items():
                if isinstance(day, str) and day.isdigit():
                    day = int(day)
                if type(day) is not int or not 0 <= day <= 6:
                    raise ValueError("weekday indexes must be 0 through 6 (Sunday first)")
                validate_hours([schedule] + [None] * 6)
            continue
        if key in {"name", "type"}:
            _string(value, key, nonempty=True)
        elif key == "address":
            _string(value, key)
        elif key in OPTIONAL_STRINGS and value is not None:
            _string(value, key)


def normalize_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def duplicate_name(places: list[dict[str, Any]], name: str, exclude_id: str | None = None) -> dict[str, Any] | None:
    normalized = normalize_name(name)
    return next((p for p in places if p["id"] != exclude_id and normalize_name(p["name"]) == normalized), None)


def readable_id(name: str, existing_ids: set[str] | list[str] | tuple[str, ...] = ()) -> str:
    normalized = unicodedata.normalize("NFKC", name)
    chunks: list[str] = []
    separator = False
    for char in normalized:
        if unicodedata.category(char)[0] in {"L", "N"}:
            if separator and chunks:
                chunks.append("-")
            chunks.append(char.casefold())
            separator = False
        else:
            separator = True
    base = "place-" + "".join(chunks).strip("-")
    if base == "place-":
        raise ValueError("name must include at least one letter or digit")
    occupied = set(existing_ids)
    if base not in occupied:
        return base
    suffix = 2
    while f"{base}-{suffix}" in occupied:
        suffix += 1
    return f"{base}-{suffix}"


def build_new_place(name: str, type: str, places: list[dict[str, Any]], reserved_ids: set[str] | list[str] = ()) -> dict[str, Any]:
    _string(name, "name", nonempty=True)
    _string(type, "type", nonempty=True)
    ids = {p["id"] for p in places} | set(reserved_ids)
    return {
        "id": readable_id(name, ids), "name": name, "type": type, "address": "",
        "instagram": None, "description": None, "hoursUpdatedAt": None,
        "hours": [None] * 7,
    }


def prepare_change(places: list[dict[str, Any]], operations: list[dict[str, Any]], reserved_ids: set[str] | list[str] = ()) -> dict[str, Any]:
    """Capture a complete proposal and the exact values it depends on.

    Existing operation: {"place_id": id, "patch": {...}, "hours_days": {weekday: slot}}.
    New operation: {"new_place": complete_record}.
    """
    validate_places(places)
    by_id = {p["id"]: p for p in places}
    prepared: list[dict[str, Any]] = []
    ids: set[str] = set()
    new_ids = {p["id"] for p in places} | set(reserved_ids)
    for operation in operations:
        if "new_place" in operation or (operation.get("kind") == "new" and "place" in operation):
            place = deepcopy(operation.get("new_place", operation.get("place")))
            if not isinstance(place, dict) or set(place) - NEW_RECORD_FIELDS:
                raise ValueError("new place contains unsupported fields")
            validate_place(place)
            if place["id"] in new_ids:
                raise ValueError(f"business ID is already in use: {place['id']}")
            duplicate = duplicate_name(places, place["name"])
            if not duplicate:
                duplicate = next((prior["place"] for prior in prepared if prior["kind"] == "new"
                                  and normalize_name(prior["place"]["name"]) == normalize_name(place["name"])), None)
            if duplicate:
                raise ValueError(f"a business with the same normalized name already exists: {duplicate['id']}")
            new_ids.add(place["id"])
            ids.add(place["id"])
            prepared.append({"kind": "new", "place": place})
            continue
        place_id = operation.get("place_id")
        if place_id not in by_id:
            raise ValueError(f"unknown place ID: {place_id}")
        if place_id in ids:
            raise ValueError(f"duplicate operation for place ID: {place_id}")
        ids.add(place_id)
        patch = deepcopy(operation.get("patch", {}))
        hours_days = deepcopy(operation.get("hours_days", {}))
        if hours_days:
            patch["hours_days"] = hours_days
        validate_patch(patch)
        if not patch:
            raise ValueError("an operation must change at least one field")
        current = by_id[place_id]
        field_originals = {key: {"exists": key in current, "value": deepcopy(current.get(key))}
                           for key in patch if key != "hours_days"}
        hour_originals = {str(day): deepcopy(current["hours"][int(day)]) for day in patch.get("hours_days", {})}
        candidate = deepcopy(current)
        for key, value in patch.items():
            if key == "hours_days":
                for day, schedule in value.items():
                    candidate["hours"][int(day)] = deepcopy(schedule)
            else:
                candidate[key] = deepcopy(value)
        validate_place(candidate)
        if "name" in patch:
            duplicate = duplicate_name(places, patch["name"], exclude_id=place_id)
            if duplicate:
                raise ValueError(f"a business with the same normalized name already exists: {duplicate['id']}")
        prepared.append({"kind": "edit", "place_id": place_id, "patch": patch,
                         "originals": field_originals, "hours_originals": hour_originals})
    if not prepared:
        raise ValueError("change set is empty")
    return {"version": 1, "operations": prepared}


def write_places(path: str | Path, places: list[dict[str, Any]]) -> None:
    path = Path(path)
    validate_places(places)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".places-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(places, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def commit_change(path: str | Path, prepared: dict[str, Any], committed_at: datetime | None = None) -> list[dict[str, Any]]:
    if prepared.get("version") != 1 or not isinstance(prepared.get("operations"), list):
        raise ValueError("unsupported prepared change")
    places = load_places(path)
    by_id = {p["id"]: p for p in places}
    result = deepcopy(places)
    result_by_id = {p["id"]: p for p in result}
    has_hours = False
    for operation in prepared["operations"]:
        if operation["kind"] == "new":
            place = deepcopy(operation["place"])
            validate_place(place)
            if place["id"] in result_by_id or duplicate_name(result, place["name"]):
                raise DirectoryConflict("new business ID or normalized name is now in use")
            result.append(place)
            result_by_id[place["id"]] = place
            continue
        place_id = operation["place_id"]
        if place_id not in by_id:
            raise DirectoryConflict(f"place was removed: {place_id}")
        current = by_id[place_id]
        for key, original in operation["originals"].items():
            if (key in current) != original["exists"] or current.get(key) != original["value"]:
                raise DirectoryConflict(f"field changed since proposal: {place_id}.{key}")
        for day, original in operation["hours_originals"].items():
            if current["hours"][int(day)] != original:
                raise DirectoryConflict(f"hours changed since proposal: {place_id}, weekday {day}")
        target = result_by_id[place_id]
        for key, value in operation["patch"].items():
            if key == "hours_days":
                for day, schedule in value.items():
                    target["hours"][int(day)] = deepcopy(schedule)
                has_hours = True
            else:
                target[key] = deepcopy(value)
        validate_place(target)
    now = committed_at or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("commit time must include a timezone")
    if has_hours:
        timestamp = now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        for operation in prepared["operations"]:
            if operation["kind"] == "edit" and "hours_days" in operation["patch"]:
                result_by_id[operation["place_id"]]["hoursUpdatedAt"] = timestamp
    validate_places(result)
    write_places(path, result)
    return result
