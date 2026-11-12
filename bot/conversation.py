"""Bounded LangChain interpreter for directory editing requests.

The model can request typed operations, but this module has no directory or
proposal write path. The caller validates and turns the returned operations
into reviewable proposals.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROMPT_DIR = Path(__file__).parent / "prompts"
JERUSALEM = ZoneInfo("Asia/Jerusalem")


class HoursOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_id: str
    # Keys are Sunday=0 through Saturday=6. Values are [] for explicitly
    # closed or a list of {opens, closes}; omitted weekdays remain untouched.
    hours_days: dict[str, list[dict[str, str]] | None]


class EditOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_id: str
    patch: dict[str, Any]


class CreateOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: str
    fields: dict[str, Any] = Field(default_factory=dict)


class GooglePreviewOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_id: str | None = None
    google_place_id: str | None = None
    all_places: bool = False


class WithdrawOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_id: str | None = None
    target: Literal["field", "weekday", "business", "proposal"]
    field: str | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)

    @model_validator(mode="after")
    def target_is_well_formed(self) -> "WithdrawOperation":
        if self.target == "proposal":
            if self.place_id is not None or self.field is not None or self.weekday is not None:
                raise ValueError("proposal withdrawal takes no place, field, or weekday")
            return self
        if not self.place_id:
            raise ValueError("business-scoped withdrawal requires place_id")
        if self.target == "field" and (not self.field or self.weekday is not None):
            raise ValueError("field withdrawal requires field and no weekday")
        if self.target == "weekday" and (self.weekday is None or self.field is not None):
            raise ValueError("weekday withdrawal requires weekday and no field")
        if self.target == "business" and (self.field is not None or self.weekday is not None):
            raise ValueError("business withdrawal takes no field or weekday")
        return self


_OPERATION_MODELS: dict[str, type[BaseModel]] = {
    "propose_hours": HoursOperation,
    "propose_edit": EditOperation,
    "propose_create": CreateOperation,
    "preview_google_hours": GooglePreviewOperation,
    "withdraw_proposed_change": WithdrawOperation,
}

_TOOL_DESCRIPTIONS = {
    "propose_hours": "Prepare an hours change for explicitly stated recurring weekdays. Sunday is 0. Only include changed days; [] means explicitly closed and null means unknown. Do not call unless place and hours are clear.",
    "propose_edit": "Prepare changes to allowed fields of one existing place. Include only explicitly requested fields; use an empty string only when the user explicitly asks to clear a field. Never include id or hoursUpdatedAt.",
    "propose_create": "Prepare a new place. Require a clear name and type. Put optional explicitly supplied fields in fields; do not invent values.",
    "preview_google_hours": "Request a Google hours preview for one identified place, or all eligible places. A preview does not apply changes.",
    "withdraw_proposed_change": "Withdraw an item from the active proposal being corrected. Use only for an explicit request to cancel or undo a previously proposed change. target=field removes one changed field; target=weekday removes one changed weekday; target=business removes that whole listing/change; target=proposal cancels the whole proposal. This does not clear a directory value. Call only when correcting an active proposal.",
}


class ConversationResult(BaseModel):
    reply: str = ""
    operations: list[dict[str, Any]] = Field(default_factory=list)


def _render_turn(turn: Any) -> str:
    if isinstance(turn, dict):
        role = str(turn.get("role", "user"))
        text = str(turn.get("text", turn.get("content", "")))
    else:
        role, text = "user", str(turn)
    return f"{role}: {text}"


async def interpret_message(
    *,
    text: str,
    places: list[dict[str, Any]],
    timestamp: datetime,
    turns: list[Any],
    active_proposals: list[dict[str, Any]],
    max_tool_calls: int,
) -> ConversationResult:
    """Interpret one message and return validated operation requests.

    This function only returns tool arguments. The caller must validate each
    operation, persist a proposal, and send that proposal for approval.
    """
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("message timestamp must be timezone-aware")
    if max_tool_calls <= 0:
        raise ValueError("max_tool_calls must be positive")

    llm = ChatOpenAI(
        base_url=os.environ.get("CLASSIFIER_BASE_URL"),
        api_key=os.environ.get("CLASSIFIER_API_KEY"),
        model=os.environ.get("CLASSIFIER_MODEL", "Qwen/Qwen3-30B-A3B-Instruct-2507"),
        temperature=0,
        timeout=90,
        max_retries=0,
    )
    tools = [
        {
            "name": name,
            "description": _TOOL_DESCRIPTIONS[name],
            "parameters": model.model_json_schema(),
        }
        for name, model in _OPERATION_MODELS.items()
    ]
    system = (PROMPT_DIR / "conversation_system.txt").read_text(encoding="utf-8")
    history_limit = max(0, int(os.environ.get("OPENHOURS_MAX_TURNS", "8")) - 1)
    recent_turns = turns[-history_limit:] if history_limit else []
    history = "\n".join(_render_turn(turn) for turn in recent_turns)
    human = (PROMPT_DIR / "conversation_human.txt").read_text(encoding="utf-8").format(
        message_datetime=timestamp.astimezone(JERUSALEM).isoformat(),
        places=json.dumps(places, ensure_ascii=False, separators=(",", ":")),
        conversation=history or "(אין הודעות קודמות בשיחה הפעילה)",
        active_proposals=json.dumps(active_proposals, ensure_ascii=False, separators=(",", ":")),
        message=text,
    )
    model = llm.bind_tools(tools, tool_choice="auto")
    response = await model.ainvoke([SystemMessage(content=system), HumanMessage(content=human)])
    raw_calls = list(getattr(response, "tool_calls", []) or [])
    if len(raw_calls) > max_tool_calls:
        raise ValueError("model exceeded the configured tool-call limit")
    operations: list[dict[str, Any]] = []
    for call in raw_calls:
        name = call.get("name")
        schema = _OPERATION_MODELS.get(name)
        if schema is None:
            raise ValueError("model requested an unsupported operation")
        try:
            operation = schema.model_validate(call.get("args", {}))
        except Exception:
            if name == "withdraw_proposed_change":
                return ConversationResult(reply="איזה שדה, יום או עסק לבטל מההצעה הפעילה? ההצעה נשארת ללא שינוי.")
            raise
        operations.append({"kind": name, **operation.model_dump()})
    return ConversationResult(
        reply=str(getattr(response, "content", "") or ""),
        operations=operations,
    )
