"""Propose opening-hour updates and apply them after a group reaction."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from gowa_sdk.webhooks import WebhookMessagePayload
from gowa_sdk import GoWaClient, SendMessageRequest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)
_lock = asyncio.Lock()
PROMPT_DIR = Path(__file__).parent / "prompts" / "openhours"
POSITIVE_REACTIONS = {"👍", "✅", "❤️", "❤", "🙌", "👌"}
WEEKDAYS = ("יום ראשון", "יום שני", "יום שלישי", "יום רביעי", "יום חמישי", "יום שישי", "שבת")


class Interval(BaseModel):
    opens: str
    closes: str

    @field_validator("opens", "closes")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("time must be HH:MM")
        return value


class DayUpdate(BaseModel):
    day: int = Field(ge=0, le=6, description="Sunday=0, Monday=1, ..., Saturday=6")
    intervals: list[Interval] = Field(description="Empty only if explicitly closed")


class PlaceUpdate(BaseModel):
    place_id: str = Field(description="An exact ID from the supplied places")
    days: list[DayUpdate]


class Extraction(BaseModel):
    updates: list[PlaceUpdate]


async def extract_hours(text: str, places: list[dict], timestamp: datetime) -> Extraction:
    llm = ChatOpenAI(
        base_url=os.environ.get("CLASSIFIER_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
        api_key=os.environ.get("CLASSIFIER_API_KEY"),
        model=os.environ.get("CLASSIFIER_MODEL", "Qwen/Qwen3-30B-A3B-Instruct-2507"),
        temperature=0,
        timeout=90,
        max_retries=0,
    )
    known_places = "\n".join(f"- {p['name']} (ID: {p['id']})" for p in places)
    system_prompt = (PROMPT_DIR / "system.txt").read_text(encoding="utf-8")
    human_prompt = (PROMPT_DIR / "human.txt").read_text(encoding="utf-8").format(
        places=known_places,
        message_datetime=timestamp.astimezone(ZoneInfo("Asia/Jerusalem")).isoformat(),
        message=text,
    )
    return await llm.with_structured_output(Extraction).ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt),
    ])


def _load_places(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as file:
        places = json.load(file)
    if not isinstance(places, list):
        raise ValueError("places JSON must be an array")
    ids = set()
    for place in places:
        if not isinstance(place, dict) or not isinstance(place.get("id"), str) or not isinstance(place.get("name"), str):
            raise ValueError("each place must have string id and name")
        if place["id"] in ids:
            raise ValueError("place IDs must be unique")
        ids.add(place["id"])
        if not isinstance(place.get("hours"), list) or len(place["hours"]) != 7:
            raise ValueError("each place must have seven hours entries")
    return places


def _write_places(path: Path, places: list[dict]) -> None:
    # Replace in the same directory so readers never observe a partial JSON file.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temp_path = Path(file.name)
            json.dump(places, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def is_openhours_post(message: WebhookMessagePayload) -> bool:
    group = os.environ.get("OPENHOURS_GROUP_ID")
    return bool(group and message.chat_id == group and (message.text or message.reacted_message_id))


@contextmanager
def _pending_db(path: Path):
    db_path = os.environ.get("OPENHOURS_PENDING_DB", str(path) + ".pending.sqlite3")
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS proposals (
                group_id TEXT NOT NULL, source_id TEXT NOT NULL, reply_id TEXT NOT NULL,
                timestamp TEXT NOT NULL, extraction TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, reply_id), UNIQUE (group_id, source_id)
            )""")
            yield conn
    finally:
        conn.close()


def _format_proposal(extraction: Extraction, places: list[dict]) -> str:
    names = {place["id"]: place["name"] for place in places}
    lines = ["זיהיתי את שעות הפעילות הבאות:"]
    for update in extraction.updates:
        lines.append(f"\n*{names[update.place_id]}*")
        for day in sorted(update.days, key=lambda item: item.day):
            hours = ", ".join(f"{interval.opens}–{interval.closes}" for interval in day.intervals) or "סגור"
            lines.append(f"{WEEKDAYS[day.day]}: {hours}")
    lines.append("\nלאישור ועדכון השעות, הגיבו להודעה הזו ב-👍 (או ✅, ❤️, 🙌, 👌).")
    return "\n".join(lines)


async def _send_proposal(message: WebhookMessagePayload, text: str) -> str:
    username, password = os.environ.get("WHATSAPP", ":").split(":", 1)
    client = GoWaClient(base_url=os.environ.get("WHATSAPP_BASE_URL", "http://localhost:3001"), username=username, password=password)
    try:
        response = await client.send_message(SendMessageRequest(phone=message.chat_id or "", message=text, reply_message_id=message.id))
    finally:
        await client.close()
    if response.results is None or not response.results.message_id:
        raise ValueError("GoWa did not return the proposal message ID")
    return response.results.message_id


def _valid_updates(extraction: Extraction, places: list[dict]) -> Extraction:
    known = {place["id"] for place in places}
    return Extraction(updates=[update for update in extraction.updates
        if update.place_id in known and update.days
        and len({day.day for day in update.days}) == len(update.days)])


async def handle_openhours_post(message: WebhookMessagePayload) -> None:
    if not is_openhours_post(message):
        return
    path_string = os.environ.get("OPENHOURS_PLACES_PATH")
    if not path_string:
        raise ValueError("OPENHOURS_PLACES_PATH must point to an existing places JSON file")
    if getattr(message, "is_from_me", False):
        return
    path = Path(path_string)
    if message.reacted_message_id:
        if message.reaction not in POSITIVE_REACTIONS:
            return
        async with _lock:
            with _pending_db(path) as conn:
                row = conn.execute("SELECT timestamp, extraction FROM proposals WHERE group_id = ? AND reply_id = ? AND applied = 0", (message.chat_id, message.reacted_message_id)).fetchone()
                if row is None:
                    return
                timestamp = datetime.fromisoformat(row[0])
                extraction = Extraction.model_validate_json(row[1])
                _apply_updates(path, extraction, timestamp)
                conn.execute("UPDATE proposals SET applied = 1 WHERE group_id = ? AND reply_id = ?", (message.chat_id, message.reacted_message_id))
        return
    if not message.id or message.timestamp is None or message.timestamp.utcoffset() is None:
        logger.warning("Skipping opening-hours message without an ID or timezone-aware timestamp")
        return
    places = _load_places(path)
    extraction = _valid_updates(await extract_hours(message.text or "", places, message.timestamp), places)
    if not extraction.updates:
        return
    timestamp = message.timestamp.astimezone(timezone.utc)
    async with _lock:
        with _pending_db(path) as conn:
            existing = conn.execute("SELECT 1 FROM proposals WHERE group_id = ? AND source_id = ?", (message.chat_id, message.id)).fetchone()
            if existing:
                return
            reply_id = await _send_proposal(message, _format_proposal(extraction, places))
            conn.execute("INSERT INTO proposals (group_id, source_id, reply_id, timestamp, extraction) VALUES (?, ?, ?, ?, ?)", (message.chat_id, message.id, reply_id, timestamp.isoformat(), extraction.model_dump_json()))


def _apply_updates(path: Path, extraction: Extraction, timestamp: datetime) -> None:
    places = _load_places(path)
    by_id = {place["id"]: place for place in places}
    changed = False
    for update in _valid_updates(extraction, places).updates:
        place = by_id[update.place_id]
        previous = place.get("hoursUpdatedAt")
        if previous:
            previous_time = datetime.fromisoformat(previous.replace("Z", "+00:00"))
            if previous_time.utcoffset() is None:
                raise ValueError("hoursUpdatedAt must have a timezone")
            if timestamp <= previous_time:
                continue
        for day in update.days:
            place["hours"][day.day] = [interval.model_dump() for interval in day.intervals]
        place["hoursUpdatedAt"] = timestamp.isoformat().replace("+00:00", "Z")
        changed = True
    if changed:
        _write_places(path, places)

def is_relevant(event: str, payload: dict) -> bool:
    return is_openhours_post(WebhookMessagePayload.model_validate(payload))


async def handle(event: str, payload: dict) -> None:
    await handle_openhours_post(WebhookMessagePayload.model_validate(payload))
