"""Authorized WhatsApp transport for conversational directory proposals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gowa_sdk import GoWaClient, SendMessageRequest
from gowa_sdk.webhooks import WebhookMessagePayload

from .conversation import interpret_message
from .directory import EDITABLE_FIELDS, build_new_place, duplicate_name, load_places, prepare_change, readable_id
from .proposals import ProposalStore

logger = logging.getLogger(__name__)
WEEKDAY_NAMES = ("ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת")
_state_lock = asyncio.Lock()
_MODEL_SEMAPHORE: asyncio.Semaphore | None = None
_MODEL_SEMAPHORE_SIZE: int | None = None
_MODEL_REQUEST_LOOP: asyncio.AbstractEventLoop | None = None
_LIMIT_MESSAGE = "הגעת למגבלת השימוש של הבוט. לא נשמר שינוי. אפשר לנסות שוב מאוחר יותר."
_FAILURE_MESSAGE = "לא הצלחתי להכין הצעה בטוחה. לא נשמר שינוי. אפשר לנסח מחדש את הבקשה."


def _event_ref(value: Any) -> str:
    """Correlate logs without exposing the WhatsApp event identifier."""
    if not isinstance(value, str) or not value:
        return "unknown"
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _safe_token(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
        return value
    return "unknown"


def _log_failure(stage: str, event_id: Any, exc: Exception, *, level: int = logging.ERROR) -> None:
    # Exceptions and provider response bodies can contain messages, phone numbers,
    # credentials, or request bodies. Log only safe metadata and stack locations.
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(response, "status_code", None)
    status = status if isinstance(status, int) else "unknown"
    provider_request_id = getattr(exc, "request_id", None)
    if not provider_request_id and response is not None:
        provider_request_id = response.headers.get("x-request-id")
    frames = ",".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)
    ) or "unknown"
    detail = "unknown"
    if isinstance(exc, sqlite3.Error):
        detail = _safe_token(getattr(exc, "sqlite_errorname", None))
        missing = re.fullmatch(r"no such column: ([A-Za-z_][A-Za-z0-9_]*)", str(exc))
        if missing and missing.group(1) in {"message_id", "request_id", "group_id", "revision", "status"}:
            detail += ":missing_column:" + missing.group(1)
    elif isinstance(exc, LimitConfigurationError):
        for variable in CAP_ENV.values():
            if str(exc) == f"{variable} must be a positive integer":
                detail = variable + ":must_be_positive_integer"
                break
    logger.log(
        level,
        "Directory event failed event=%s stage=%s error=%s status=%s code=%s detail=%s provider_request_id=%s frames=%s",
        _event_ref(event_id), stage, type(exc).__name__, status,
        _safe_token(getattr(exc, "code", None)), detail, _safe_token(provider_request_id), frames,
    )

CAP_ENV = {
    "turns": "OPENHOURS_MAX_TURNS",
    "revisions": "OPENHOURS_MAX_REVISIONS",
    "tool_calls": "OPENHOURS_MAX_TOOL_CALLS",
    "concurrent_model_requests": "OPENHOURS_MAX_CONCURRENT_MODEL_REQUESTS",
}


class LimitConfigurationError(ValueError):
    pass


class WithdrawalError(ValueError):
    """A requested withdrawal does not match a change in the active proposal."""


def _limits() -> dict[str, int]:
    values: dict[str, int] = {}
    for key, variable in CAP_ENV.items():
        raw = os.environ.get(variable)
        try:
            value = int(raw) if raw is not None else 0
        except ValueError:
            value = 0
        if value <= 0:
            raise LimitConfigurationError(f"{variable} must be a positive integer")
        values[key] = value
    return values


def _normalized_phone(value: str | None) -> str:
    if not value:
        return ""
    phone = value.split("@", 1)[0].split(":", 1)[0]
    digits = re.sub(r"\D", "", phone)
    return digits


def _authorized(message: WebhookMessagePayload) -> bool:
    configured = os.environ.get("OPENHOURS_AUTHORIZED_PHONE_NUMBER", "")
    if not re.fullmatch(r"\+[1-9]\d{7,14}", configured):
        return False
    expected = _normalized_phone(configured)
    sender = _normalized_phone(message.from_)
    return bool(expected and sender and expected == sender)


def _from_me(message: WebhookMessagePayload, payload: dict[str, Any] | None = None) -> bool:
    extra = payload or getattr(message, "model_extra", None) or {}
    return bool(
        getattr(message, "is_from_me", False)
        or extra.get("is_from_me")
        or extra.get("from_me")
        or extra.get("isFromMe")
    )


def is_openhours_post(message: WebhookMessagePayload, payload: dict[str, Any] | None = None) -> bool:
    if os.environ.get("OPENHOURS_CONVERSATION_ENABLED", "false").lower() != "true":
        return False
    group = os.environ.get("OPENHOURS_GROUP_ID")
    return bool(group and message.chat_id == group and (message.text or message.reacted_message_id))


def _paths() -> tuple[Path, str]:
    places = os.environ.get("OPENHOURS_PLACES_PATH")
    if not places:
        raise ValueError("OPENHOURS_PLACES_PATH must point to the directory JSON file")
    path = Path(places)
    return path, os.environ.get("OPENHOURS_PENDING_DB", str(path) + ".pending.sqlite3")


def _client() -> GoWaClient:
    raw = os.environ.get("WHATSAPP", ":")
    username, separator, password = raw.partition(":")
    if not separator or not username or not password:
        raise ValueError("WHATSAPP must contain the GoWa username and password")
    return GoWaClient(
        base_url=os.environ.get("WHATSAPP_BASE_URL", "http://localhost:3001"),
        username=username,
        password=password,
    )


async def _send(chat_id: str, text: str, reply_message_id: str | None = None) -> str | None:
    client = _client()
    try:
        response = await client.send_message(SendMessageRequest(
            phone=chat_id, message=text, reply_message_id=reply_message_id,
        ))
        return response.results.message_id if response.results else None
    except Exception as exc:
        _log_failure("whatsapp_send", reply_message_id, exc)
        raise
    finally:
        await client.close()


async def _react(chat_id: str, message_id: str, emoji: str) -> None:
    client = _client()
    try:
        await client.react_to_message(message_id=message_id, phone=chat_id or "", emoji=emoji)
    except Exception as exc:
        _log_failure("whatsapp_reaction", message_id, exc)
        raise
    finally:
        await client.close()


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _format_change(prepared: Any, places: list[dict[str, Any]], note: str | None = None) -> str:
    data = _plain(prepared)
    operations = data.get("operations", []) if isinstance(data, dict) else []
    if not operations:
        operations = data.get("changes", []) if isinstance(data, dict) else []
    known = {place["id"]: place for place in places}
    lines = ["הצעה לעדכון מדריך העסקים (טרם נשמרה):"]
    if note:
        lines.extend(["", note])
    for operation in operations:
        place = operation.get("place") or operation.get("new_place")
        place_id = operation.get("place_id") or (place or {}).get("id", "")
        name = (place or {}).get("name") or operation.get("name") or known.get(place_id, {}).get("name") or place_id
        lines.extend(["", f"*{name}*"])
        if place or operation.get("new_place"):
            lines.append("רשומה חדשה:")
            lines.append(json.dumps(place or operation["new_place"], ensure_ascii=False, indent=2))
        patch = operation.get("patch", {})
        if patch:
            originals = operation.get("originals", {})
            for field, after in patch.items():
                if field == "hours_days":
                    continue
                original = originals.get(field, {})
                old = original.get("value") if original.get("exists") else known.get(place_id, {}).get(field, "לא צוין")
                lines.append(f"{field}: {old!r} ← {after!r}")
        hour_days = operation.get("hours_days", patch.get("hours_days", {}))
        old_hours = operation.get("hours_originals", {})
        for day, intervals in sorted(hour_days.items(), key=lambda item: int(item[0])):
            day_index = int(day)
            hours = "לא ידוע" if intervals is None else (", ".join(f"{item['opens']}–{item['closes']}" for item in intervals) or "סגור")
            before = old_hours.get(str(day), known.get(place_id, {}).get("hours", [None] * 7)[day_index])
            old_text = "לא ידוע" if before is None else ("סגור" if before == [] else json.dumps(before, ensure_ascii=False))
            lines.append(f"יום {WEEKDAY_NAMES[day_index]}: {old_text} ← {hours}")
    lines.extend(["", "לאישור ההצעה הזו בלבד, הגיבו להודעה ב-👍. תיקון ייצור הצעה חדשה שתדרוש אישור נוסף."])
    return "\n".join(lines)


def _format_google(preview: dict[str, Any]) -> str:
    lines = ["תצוגה מקדימה של שעות Google (טרם נשמרה):"]
    for result in preview.get("results", []):
        lines.extend([
            "", f"*{result.get('display_name') or result['place_id']}*",
            f"מזהה Google: {result.get('google_place_id') or 'לא צוין'}",
            f"כתובת: {result.get('address') or 'לא צוין'}",
        ])
        hours = result.get("hours") or []
        for day, value in enumerate(hours):
            lines.append(f"יום {WEEKDAY_NAMES[day]}: {json.dumps(value, ensure_ascii=False)}")
    skipped = preview.get("skipped", [])
    if skipped:
        lines.extend(["", "לא נמצאו תוצאות:"])
        lines.extend(f"• {item.get('place_id')}: {item.get('reason')}" for item in skipped)
    if preview.get("results"):
        lines.extend(["", "לאישור, הגיבו להצעה המלאה הזו ב-👍."])
    else:
        lines.extend(["", "לא נוצרה הצעה לאישור כי לא נמצאו שעות מתאימות."])
    return "\n".join(lines)


def _google_note(preview: dict[str, Any]) -> str:
    lines = ["פרטי התאמת Google:"]
    for item in preview.get("results", []):
        lines.append(
            f"{item.get('display_name') or item['place_id']} (ID מדריך: {item['place_id']}; "
            f"ID Google: {item.get('google_place_id') or 'לא צוין'}; "
            f"כתובת: {item.get('address') or 'לא צוין'})"
        )
    skipped = preview.get("skipped", [])
    if skipped:
        lines.append("עסקים שדולגו:")
        lines.extend(f"{item.get('place_id')}: {item.get('reason')}" for item in skipped)
    return "\n".join(lines)


def _operation_dict(operation: dict[str, Any], places: list[dict[str, Any]], reserved_ids: list[str] | None = None,
                    pending_new: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    kind = operation["kind"]
    if kind == "propose_hours":
        return {
            "place_id": operation["place_id"],
            "hours_days": {str(int(day)): intervals for day, intervals in operation["hours_days"].items()},
        }
    if kind == "propose_edit":
        return {"place_id": operation["place_id"], "patch": operation["patch"]}
    if kind == "propose_create":
        fields = operation.get("fields", {})
        allowed_optional = {"address", "instagram", "description", "google-search-name", "google-search-id"}
        if set(fields) - allowed_optional:
            raise ValueError("new place fields include a system-managed or unsupported field")
        place = next((item for item in (pending_new or []) if item.get("name", "").casefold() == operation["name"].casefold()), None)
        if place is None and len(pending_new or []) == 1:
            place = pending_new[0]
        place = dict(place) if place else build_new_place(operation["name"], operation["type"], places, reserved_ids or ())
        place["name"] = operation["name"]
        place["type"] = operation["type"]
        place.update(fields)
        return {"new_place": place}
    raise ValueError(f"unsupported operation kind {kind}")


def _combine_revision(previous: dict[str, Any] | None, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a complete replacement change set, retaining unchanged proposal edits."""
    combined: dict[str, dict[str, Any]] = {}
    for item in (previous or {}).get("operations", []):
        if item.get("kind") == "new":
            place = dict(item["place"])
            combined[place["id"]] = {"new_place": place}
        else:
            combined[item["place_id"]] = {
                "place_id": item["place_id"],
                "patch": dict(item.get("patch", {})),
            }
    for change in changes:
        if "new_place" in change:
            place = dict(change["new_place"])
            combined[place["id"]] = {"new_place": place}
            continue
        place_id = change["place_id"]
        current = combined.get(place_id)
        if current and "new_place" in current:
            place = current["new_place"]
            for field, value in change.get("patch", {}).items():
                if field == "hours_days":
                    for day, schedule in value.items():
                        place["hours"][int(day)] = schedule
                else:
                    place[field] = value
            for day, schedule in change.get("hours_days", {}).items():
                place["hours"][int(day)] = schedule
            continue
        target = current or {"place_id": place_id, "patch": {}}
        patch = target.setdefault("patch", {})
        for field, value in change.get("patch", {}).items():
            if field == "hours_days":
                days = patch.setdefault("hours_days", {})
                days.update(value)
            else:
                patch[field] = value
        days = patch.setdefault("hours_days", {}) if change.get("hours_days") else None
        if days is not None:
            days.update(change["hours_days"])
        combined[place_id] = target
    return list(combined.values())


def _rekey_pending_new(previous: dict[str, Any] | None, changes: list[dict[str, Any]],
                       places: list[dict[str, Any]], reserved_ids: list[str]) -> dict[str, str]:
    old_new = {
        item["place"]["id"]: item["place"]
        for item in (previous or {}).get("operations", [])
        if item.get("kind") == "new"
    }
    used = {place["id"] for place in places} | set(reserved_ids)
    aliases: dict[str, str] = {}
    for item in changes:
        place = item.get("new_place")
        if not place:
            continue
        old = old_new.get(place["id"])
        if old and old.get("name") != place.get("name"):
            used.discard(place["id"])
            old_id = place["id"]
            place["id"] = readable_id(place["name"], used)
            used.add(place["id"])
            aliases[old_id] = place["id"]
    return aliases


def _apply_withdrawals(previous: dict[str, Any] | None, operations: list[dict[str, Any]],
                       withdrawals: list[dict[str, Any]], places: list[dict[str, Any]],
                       proposed_operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not withdrawals:
        return operations
    if not previous:
        raise WithdrawalError("there is no active proposal to correct")
    if any(item["target"] == "proposal" for item in withdrawals):
        if len(withdrawals) != 1:
            raise WithdrawalError("whole-proposal cancellation cannot be combined with item withdrawals")
        return []
    proposed_by_place: dict[str, dict[str, Any]] = {}
    for operation in proposed_operations:
        if operation.get("kind") == "withdraw_proposed_change":
            continue
        if "new_place" in operation:
            proposed_by_place[operation["new_place"]["id"]] = operation
        elif operation.get("place_id"):
            proposed_by_place[operation["place_id"]] = operation
        elif operation.get("kind") == "propose_create":
            pending_new = [item["place"] for item in previous.get("operations", []) if item.get("kind") == "new"]
            match = next((item for item in pending_new if item.get("name", "").casefold() == operation["name"].casefold()), None)
            if match is None and len(pending_new) == 1:
                match = pending_new[0]
            if match:
                proposed_by_place[match["id"]] = {"patch": {
                    "name": operation["name"], "type": operation["type"], **operation.get("fields", {}),
                }}
    active_ids = {
        item.get("place_id") or item.get("place", {}).get("id")
        for item in previous.get("operations", [])
    }
    for withdrawal in withdrawals:
        if withdrawal["target"] == "proposal":
            continue
        place_id = withdrawal["place_id"]
        if place_id not in active_ids:
            raise WithdrawalError("withdrawal target is not in the active proposal")
        operation = next((item for item in operations
                          if item.get("place_id") == place_id
                          or item.get("new_place", {}).get("id") == place_id), None)
        if operation is None:
            raise WithdrawalError("withdrawal target is no longer in the proposal")
        target = withdrawal["target"]
        if target == "business":
            operations.remove(operation)
            continue
        current_change = proposed_by_place.get(place_id)
        if target == "field":
            field = withdrawal["field"]
            if field not in EDITABLE_FIELDS:
                raise WithdrawalError("field cannot be withdrawn")
            if current_change and field in current_change.get("patch", {}):
                raise WithdrawalError("field has a conflicting change in this correction")
            if "new_place" in operation:
                if field in {"name", "type"}:
                    raise WithdrawalError("a new listing name or type cannot be withdrawn by field")
                default = build_new_place(operation["new_place"]["name"], operation["new_place"]["type"], places)
                if operation["new_place"].get(field) == default.get(field):
                    raise WithdrawalError("field has no proposed change")
                operation["new_place"][field] = default.get(field)
            else:
                patch = operation.get("patch", {})
                if field not in patch:
                    raise WithdrawalError("field has no proposed change")
                del patch[field]
                if not patch:
                    operations.remove(operation)
            continue
        day = str(withdrawal["weekday"])
        proposed_days = {
            **(current_change or {}).get("hours_days", {}),
            **(current_change or {}).get("patch", {}).get("hours_days", {}),
        }
        if day in proposed_days or int(day) in proposed_days:
            raise WithdrawalError("weekday has a conflicting change in this correction")
        if "new_place" in operation:
            hours = operation["new_place"].get("hours", [None] * 7)
            if hours[int(day)] is None:
                raise WithdrawalError("weekday has no proposed change")
            hours[int(day)] = None
        else:
            patch = operation.get("patch", {})
            days = patch.get("hours_days", {})
            if day not in days and int(day) not in days:
                raise WithdrawalError("weekday has no proposed change")
            days.pop(day, None)
            days.pop(int(day), None)
            if not days:
                patch.pop("hours_days", None)
            if not patch:
                operations.remove(operation)
    return operations


async def _handle_reaction(message: WebhookMessagePayload, store: ProposalStore, path: Path) -> None:
    if message.reaction != "👍" or not message.reacted_message_id:
        return
    result = store.approve(message.chat_id or "", message.reacted_message_id, str(path))
    if asyncio.iscoroutine(result):
        result = await result
    status = result.get("status") if isinstance(result, dict) else result
    if status == "conflict":
        await _send(message.chat_id or "", "הנתונים השתנו מאז הכנת ההצעה. לא נשמר שינוי; יש לבדוק את המדריך ולהכין הצעה חדשה.")
    elif status not in {"committed", "approved", "applied", True}:
        await _send(message.chat_id or "", "ההצעה כבר אינה פעילה או שאינה עדכנית. לא בוצע שינוי.")
    else:
        await _send(message.chat_id or "", "האישור התקבל והשינוי נשמר.")


async def handle_openhours_post(message: WebhookMessagePayload, raw_payload: dict[str, Any] | None = None) -> None:
    if not is_openhours_post(message, raw_payload) or _from_me(message, raw_payload):
        return
    # Authorization is required for both ordinary messages and reactions.
    if not _authorized(message):
        logger.info("Ignoring unauthorized WhatsApp event")
        return
    logger.info("Directory event accepted event=%s kind=%s", _event_ref(message.id),
                "reaction" if message.reacted_message_id else "message")
    try:
        path, db_path = _paths()
        store = ProposalStore(db_path)
    except Exception as exc:
        _log_failure("proposal_store_init", message.id, exc)
        raise
    limits: dict[str, int] | None = None

    if message.reacted_message_id:
        # Approval does not invoke the model, but bounded-operation settings
        # must still be valid.
        try:
            _limits()
        except LimitConfigurationError as exc:
            _log_failure("limits", message.id, exc)
            await _send(message.chat_id or "", _LIMIT_MESSAGE)
            return
        try:
            async with _state_lock:
                await _handle_reaction(message, store, path)
        except Exception as exc:
            _log_failure("reaction", message.id, exc)
            raise
        return
    body = message.text or ""
    if not body.strip() or not message.id or not message.timestamp:
        return
    try:
        limits = _limits()
    except LimitConfigurationError as exc:
        _log_failure("limits", message.id, exc)
        # No model work and no write when any required cap is misconfigured.
        await _send(message.chat_id or "", _LIMIT_MESSAGE, message.id)
        return
    if message.timestamp.utcoffset() is None:
        return

    timestamp = message.timestamp.astimezone(timezone.utc)
    stage = "load_conversation"
    try:
        reply_proposal = store.get_active_proposal(message.chat_id or "", message.replied_to_id) if message.replied_to_id else None
        linked_proposal = store.proposal_for_turn_message(message.chat_id or "", message.replied_to_id) if message.replied_to_id else None
        if linked_proposal and linked_proposal.get("status") != "pending":
            await _send(message.chat_id or "", "ההצעה שאליה הגבת כבר אינה פעילה. לא נוצר שינוי.", message.id)
            return
        if not reply_proposal and linked_proposal and linked_proposal.get("status") == "pending":
            reply_proposal = linked_proposal
        if message.replied_to_id and not reply_proposal:
            any_proposal = store.proposal_for_message(message.chat_id or "", message.replied_to_id)
            if any_proposal:
                await _send(message.chat_id or "", "ההצעה שאליה הגבת כבר אינה פעילה. לא נוצר שינוי.", message.id)
                return
        request_id = (reply_proposal.get("request_id") if isinstance(reply_proposal, dict) else None)
        if not request_id and message.replied_to_id:
            request_id = store.request_for_message(message.chat_id or "", message.replied_to_id)
        request_id = request_id or message.id
        if not reply_proposal and request_id != message.id:
            active_for_request = [
                proposal for proposal in store.active_proposals_for(message.chat_id or "")
                if proposal["request_id"] == request_id
            ]
            if active_for_request:
                reply_proposal = active_for_request[-1]
            elif store.revision_count(request_id):
                await _send(message.chat_id or "", "ההצעה שאליה הגבת כבר אינה פעילה. לא נוצר שינוי.", message.id)
                return
        async with _state_lock:
            turns = store.record_user_turn(
                message.chat_id or "", _normalized_phone(message.from_), message.id,
                body, timestamp, limits["turns"], request_id=request_id,
            )
            request_turns = store.conversation_turns(
                message.chat_id or "", _normalized_phone(message.from_),
                limits["turns"] + 1, request_id=request_id,
            )
            if sum(turn.get("role") == "user" for turn in request_turns) > limits["turns"]:
                await _send(message.chat_id or "", _LIMIT_MESSAGE, message.id)
                return
            places = load_places(str(path))
            active = [reply_proposal] if reply_proposal else []
        global _MODEL_SEMAPHORE, _MODEL_SEMAPHORE_SIZE, _MODEL_REQUEST_LOOP
        concurrency = limits["concurrent_model_requests"]
        current_loop = asyncio.get_running_loop()
        if _MODEL_SEMAPHORE is None or _MODEL_SEMAPHORE_SIZE != concurrency or _MODEL_REQUEST_LOOP is not current_loop:
            _MODEL_SEMAPHORE = asyncio.Semaphore(concurrency)
            _MODEL_SEMAPHORE_SIZE = concurrency
            _MODEL_REQUEST_LOOP = current_loop
        stage = "model"
        logger.info("Directory model request event=%s", _event_ref(message.id))
        async with _MODEL_SEMAPHORE:
            result = await interpret_message(
                text=body, places=places, timestamp=message.timestamp,
                turns=turns[:-1], active_proposals=active,
                max_tool_calls=limits["tool_calls"],
            )
        stage = "process_model_result"
        logger.info("Directory model response event=%s operations=%d", _event_ref(message.id), len(result.operations))
        if not result.operations:
            reply = result.reply.strip() or _FAILURE_MESSAGE
            sent_id = await _send(message.chat_id or "", reply, message.id)
            store.record_assistant_turn(message.chat_id or "", _normalized_phone(message.from_), reply,
                                        request_id=request_id, message_id=sent_id,
                                        proposal_id=reply_proposal.get("id") if reply_proposal else None)
            return
        if len(result.operations) > limits["tool_calls"]:
            await _send(message.chat_id or "", _LIMIT_MESSAGE, message.id)
            return

        operations: list[dict[str, Any]] = []
        withdrawals: list[dict[str, Any]] = []
        google_note: str | None = None
        google_preview_data: dict[str, Any] | None = None
        previous_prepared = reply_proposal.get("prepared") if isinstance(reply_proposal, dict) else None
        pending_new = [
            op["place"] for op in (previous_prepared or {}).get("operations", [])
            if op.get("kind") == "new"
        ]
        active_proposals = store.active_proposals_for(message.chat_id or "")
        reserved_ids = [business_id for proposal in active_proposals for business_id in proposal["business_ids"]]
        if any(item["kind"] == "withdraw_proposed_change" for item in result.operations) and any(item["kind"] == "preview_google_hours" for item in result.operations):
            clarification = "צריך לבקש תצוגת Google או לבטל שינוי קיים בנפרד. ההצעה הנוכחית נשארת פעילה."
            sent_id = await _send(message.chat_id or "", clarification, message.id)
            store.record_assistant_turn(message.chat_id or "", _normalized_phone(message.from_), clarification,
                                        request_id=request_id, message_id=sent_id,
                                        proposal_id=reply_proposal.get("id") if reply_proposal else None)
            return
        for operation in result.operations:
            if operation["kind"] == "preview_google_hours":
                from .google_preview import preview_hours
                google_preview_data = preview_hours(
                    places,
                    place_id=operation.get("place_id"),
                    google_place_id=operation.get("google_place_id"),
                    all_places=bool(operation.get("all_places")),
                )
                for item in google_preview_data.get("results", []):
                    operations.append({"place_id": item["place_id"], "hours_days": {str(i): v for i, v in enumerate(item["hours"])}})
                google_note = _google_note(google_preview_data)
            elif operation["kind"] == "withdraw_proposed_change":
                withdrawals.append(operation)
            else:
                prepared_operation = _operation_dict(operation, places, reserved_ids, pending_new)
                operations.append(prepared_operation)
                if "new_place" in prepared_operation:
                    reserved_ids.append(prepared_operation["new_place"]["id"])
        if not operations and not withdrawals:
            reply = _format_google(google_preview_data) if google_preview_data is not None else (result.reply.strip() or _FAILURE_MESSAGE)
            sent_id = await _send(message.chat_id or "", reply, message.id)
            store.record_assistant_turn(message.chat_id or "", _normalized_phone(message.from_), reply,
                                        request_id=request_id, message_id=sent_id)
            return

        operations = _combine_revision(previous_prepared, operations)
        try:
            operations = _apply_withdrawals(previous_prepared, operations, withdrawals, places, result.operations)
        except WithdrawalError:
            clarification = "איזה שינוי בהצעה לבטל? ציינו את השדה, היום או העסק המדויקים. ההצעה הנוכחית נשארת פעילה."
            sent_id = await _send(message.chat_id or "", clarification, message.id)
            store.record_assistant_turn(
                message.chat_id or "", _normalized_phone(message.from_), clarification,
                request_id=request_id, message_id=sent_id,
                proposal_id=reply_proposal.get("id") if reply_proposal else None,
            )
            return
        stage = "prepare_proposal"
        aliases = _rekey_pending_new(previous_prepared, operations, places, reserved_ids)
        async with _state_lock:
            if reply_proposal:
                still_active = any(
                    proposal["id"] == reply_proposal["id"]
                    for proposal in store.active_proposals_for(message.chat_id or "")
                )
                if not still_active:
                    await _send(message.chat_id or "", "ההצעה שאליה הגבת כבר אינה פעילה. לא נוצר שינוי.", message.id)
                    return
            if operations and store.revision_count(request_id) >= limits["revisions"]:
                await _send(message.chat_id or "", _LIMIT_MESSAGE, message.id)
                return
            if not operations:
                cancelled = store.cancel_proposal(message.chat_id or "", reply_proposal["message_id"])
                if not cancelled or cancelled.get("status") != "cancelled":
                    await _send(message.chat_id or "", "ההצעה כבר אינה פעילה. לא נוצר שינוי.", message.id)
                    return
                cancel_message_id = cancelled.get("message_id")
                proposal_id = reply_proposal["id"]
                prepared = None
            else:
                current_places = load_places(str(path))
                for operation in operations:
                    new_place = operation.get("new_place")
                    if not new_place:
                        continue
                    duplicate = duplicate_name(current_places, new_place["name"])
                    if duplicate:
                        clarification = (
                            f"כבר קיים במדריך העסק *{duplicate['name']}* (מזהה {duplicate['id']}). "
                            "האם התכוונת לעסק הקיים הזה? לא נוצרה הצעה להוספה. "
                            "אם כן, כתבו את השינוי המבוקש לעסק הקיים."
                        )
                        sent_id = await _send(message.chat_id or "", clarification, message.id)
                        store.record_assistant_turn(
                            message.chat_id or "", _normalized_phone(message.from_), clarification,
                            request_id=request_id, message_id=sent_id,
                            proposal_id=reply_proposal.get("id") if reply_proposal else None,
                        )
                        return
                prepared = prepare_change(current_places, operations)
                prepared_json = _plain(prepared)
                if google_preview_data and isinstance(prepared_json, dict):
                    prepared_json["google_preview"] = google_preview_data
                text = _format_change(prepared, current_places, google_note)
                current_ids = [operation.get("place_id") or operation["new_place"]["id"] for operation in operations]
                business_ids = list(dict.fromkeys(current_ids + list(aliases)))
                proposal_id, superseded = store.create_proposal(
                    message.chat_id or "", request_id, prepared_json,
                    business_ids, text, source_message_id=message.id,
                )
        if not operations:
            if cancel_message_id:
                try:
                    await _react(message.chat_id or "", cancel_message_id, "❌")
                except Exception as exc:
                    _log_failure("mark_cancelled_proposal", message.id, exc, level=logging.WARNING)
            reply = "ההצעה בוטלה. לא נשמר שינוי במדריך."
            sent_id = await _send(message.chat_id or "", reply, message.id)
            store.record_assistant_turn(message.chat_id or "", _normalized_phone(message.from_), reply,
                                        request_id=request_id, message_id=sent_id)
            return
        # State changes happen before best-effort X reactions, so old messages
        # are no longer approvable even if WhatsApp rejects a reaction.
        for stale_id in superseded:
            try:
                await _react(message.chat_id or "", stale_id, "❌")
            except Exception as exc:
                _log_failure("mark_superseded_proposal", message.id, exc, level=logging.WARNING)
        try:
            stage = "send_proposal"
            sent_id = await _send(message.chat_id or "", text, message.id)
            store.record_delivery(proposal_id, status="sent" if sent_id else "delivery-unknown", message_id=sent_id)
            if sent_id:
                store.record_assistant_turn(message.chat_id or "", _normalized_phone(message.from_), text,
                                            request_id=request_id, message_id=sent_id,
                                            proposal_id=proposal_id)
            logger.info("Directory proposal delivery event=%s proposal=%s status=%s", _event_ref(message.id),
                        proposal_id, "sent" if sent_id else "unknown")
        except Exception as exc:
            store.record_delivery(proposal_id, status="delivery-unknown", message_id=None)
            _log_failure("send_proposal", message.id, exc)
            await _send(message.chat_id or "", _FAILURE_MESSAGE, message.id)
    except Exception as exc:
        _log_failure(stage, message.id, exc)
        try:
            await _send(message.chat_id or "", _FAILURE_MESSAGE, message.id)
        except Exception as send_exc:
            _log_failure("send_failure_response", message.id, send_exc)


def is_relevant(event: str, payload: dict[str, Any]) -> bool:
    try:
        message = WebhookMessagePayload.model_validate(payload)
        if os.environ.get("OPENHOURS_CONVERSATION_ENABLED", "false").lower() != "true":
            reason = "disabled"
        elif not os.environ.get("OPENHOURS_GROUP_ID"):
            reason = "group_not_configured"
        elif message.chat_id != os.environ.get("OPENHOURS_GROUP_ID"):
            reason = "different_group"
        elif not (message.text or message.reacted_message_id):
            reason = "no_text_or_reaction"
        elif _from_me(message, payload):
            reason = "self_authored"
        else:
            reason = ""
        if reason:
            logger.info("Directory event ignored event=%s reason=%s", _event_ref(message.id), reason)
            return False
        if not _authorized(message):
            configured = os.environ.get("OPENHOURS_AUTHORIZED_PHONE_NUMBER", "")
            reason = "unauthorized" if re.fullmatch(r"\+[1-9]\d{7,14}", configured) else "invalid_authorized_phone_format"
            logger.info("Directory event ignored event=%s reason=%s", _event_ref(message.id), reason)
            return False
        return True
    except Exception as exc:
        _log_failure("validate_event", payload.get("id") if isinstance(payload, dict) else None, exc)
        return False


async def handle(event: str, payload: dict[str, Any]) -> None:
    await handle_openhours_post(WebhookMessagePayload.model_validate(payload), payload)
