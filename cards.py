"""Turn an answer's card spec into Slack Block Kit.

Claude may end its answer with a fenced ```card block: a JSON list of components from the small
catalog below. The relay checks it and renders native Slack blocks (tables, charts, fields,
buttons) under the answer's text. Anything off-catalog raises CardError, and the relay then posts
the text alone, so a bad spec never loses the answer.

    {"type": "header", "text": "Deploy status"}
    {"type": "text", "text": "Slack *mrkdwn*"}
    {"type": "fields", "fields": [["Branch", "`main`"], ["CI", "green"]]}
    {"type": "table", "caption": "Slow queries", "columns": ["Query", "p95 ms"], "rows": [["users", 120]]}
    {"type": "chart", "chart": "bar", "title": "Errors per day", "categories": ["Mon", "Tue"],
     "series": [{"name": "errors", "values": [3, 5]}], "x_label": "Day", "y_label": "Errors"}
    {"type": "chart", "chart": "pie", "title": "Plans", "segments": [{"label": "Pro", "value": 12}]}
    {"type": "buttons", "buttons": [{"text": "Merge it", "reply": "merge the PR", "style": "primary"},
                                    {"text": "Open PR", "url": "https://github.com/..."}]}
    {"type": "context", "text": "Checked 2 minutes ago"}
    {"type": "divider"}

Limits come from Slack's Block Kit reference (data_table, data_visualization, actions).
"""

import json
import re

CARD_FENCE = re.compile(r"\n?```card[ \t]*\n(.*?)\n```[ \t]*$", re.DOTALL)
REPLY_ACTION = "claudino_reply"  # action_id prefix of buttons that send a reply to the thread's session
LINK_ACTION = "claudino_link"  # action_id prefix of link buttons (Slack still sends their clicks)

MAX_BLOCKS = 50
MAX_SECTION = 3000
MAX_CHARTS = 2
MAX_TABLE_CHARS = 20000
CHART_KINDS = ("bar", "line", "area", "pie")


class CardError(ValueError):
    """The card spec is not valid; the reason says why."""


def split(answer: str) -> tuple[str, list | None]:
    """The answer's text and its card spec, when it ends with a ```card block that holds JSON."""
    match = CARD_FENCE.search(answer.rstrip())
    if not match:
        return answer, None
    text = answer.rstrip()[:match.start()].rstrip()
    try:
        spec = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise CardError(f"card is not JSON: {error}") from None
    if isinstance(spec, dict):
        spec = [spec]
    if not isinstance(spec, list):
        raise CardError("card must be a JSON list of components")
    return text, spec


def _str(value, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CardError(f"{name} must be a non-empty string")
    if len(value) > limit:
        raise CardError(f"{name} is longer than {limit} characters")
    return value


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CardError(f"{name} must be a number")
    return value


def _list(value, name: str, low: int, high: int) -> list:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise CardError(f"{name} must be a list of {low} to {high} items")
    return value


def text_blocks(text: str) -> list:
    """mrkdwn sections of at most 3000 characters, split between lines."""
    blocks, chunk = [], ""
    for line in text.split("\n"):
        while len(line) > MAX_SECTION:
            if chunk:
                blocks.append(chunk)
                chunk = ""
            blocks.append(line[:MAX_SECTION])
            line = line[MAX_SECTION:]
        if chunk and len(chunk) + 1 + len(line) > MAX_SECTION:
            blocks.append(chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk.strip():
        blocks.append(chunk)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": b}} for b in blocks if b.strip()]


def _cell(value) -> dict:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"type": "raw_number", "value": value, "text": f"{value:,}"}
    text = "" if value is None else str(value)
    return {"type": "raw_text", "text": text or "-"}


def _table(c: dict) -> dict:
    columns = _list(c.get("columns"), "table columns", 1, 20)
    rows = _list(c.get("rows"), "table rows", 1, 200)
    header = [_cell(_str(name, "column name", 200)) for name in columns]
    body = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise CardError(f"every table row needs {len(columns)} cells")
        body.append([_cell(value) for value in row])
    chars = sum(len(cell["text"]) for row in [header, *body] for cell in row)
    if chars > MAX_TABLE_CHARS:
        raise CardError(f"table has {chars} characters, more than {MAX_TABLE_CHARS}")
    return {"type": "data_table", "caption": _str(c.get("caption"), "table caption", 200),
            "rows": [header, *body], "page_size": min(len(body), int(c.get("page_size") or 10))}


def _chart(c: dict) -> dict:
    kind = c.get("chart")
    if kind not in CHART_KINDS:
        raise CardError(f"chart must be one of {', '.join(CHART_KINDS)}")
    title = _str(c.get("title"), "chart title", 50)
    if kind == "pie":
        segments = _list(c.get("segments"), "pie segments", 1, 12)
        chart = {"type": "pie", "segments": [
            {"label": _str(s.get("label"), "segment label", 20), "value": _number(s.get("value"), "segment value")}
            for s in segments]}
        if any(s["value"] <= 0 for s in chart["segments"]):
            raise CardError("pie segment values must be greater than 0")
        return {"type": "data_visualization", "title": title, "chart": chart}
    categories = [_str(x, "category", 20) for x in _list(c.get("categories"), "chart categories", 1, 20)]
    series, names = [], set()
    for s in _list(c.get("series"), "chart series", 1, 12):
        name = _str(s.get("name"), "series name", 20)
        if name in names:
            raise CardError(f"series name {name!r} is used twice")
        names.add(name)
        values = _list(s.get("values"), f"values of {name}", len(categories), len(categories))
        series.append({"name": name, "data": [{"label": label, "value": _number(v, f"value of {name}")}
                                              for label, v in zip(categories, values)]})
    axis = {"categories": categories}
    for key in ("x_label", "y_label"):
        if c.get(key):
            axis[key] = _str(c[key], key, 50)
    return {"type": "data_visualization", "title": title,
            "chart": {"type": kind, "series": series, "axis_config": axis}}


def _buttons(c: dict, index: int) -> dict:
    elements = []
    for i, b in enumerate(_list(c.get("buttons"), "buttons", 1, 5)):
        button = {"type": "button", "text": {"type": "plain_text", "text": _str(b.get("text"), "button text", 75)}}
        if b.get("url"):
            button["url"] = _str(b["url"], "button url", 3000)
            button["action_id"] = f"{LINK_ACTION}_{index}_{i}"
        else:
            button["value"] = _str(b.get("reply"), "button reply", 2000)
            button["action_id"] = f"{REPLY_ACTION}_{index}_{i}"
        if b.get("style") in ("primary", "danger"):
            button["style"] = b["style"]
        elements.append(button)
    return {"type": "actions", "elements": elements}


def component_blocks(c, index: int) -> list:
    if not isinstance(c, dict):
        raise CardError("every component must be a JSON object")
    kind = c.get("type")
    if kind == "header":
        return [{"type": "header", "text": {"type": "plain_text", "text": _str(c.get("text"), "header", 150)}}]
    if kind == "text":
        return text_blocks(_str(c.get("text"), "text", 12000))
    if kind == "context":
        return [{"type": "context", "elements": [{"type": "mrkdwn", "text": _str(c.get("text"), "context", 2000)}]}]
    if kind == "divider":
        return [{"type": "divider"}]
    if kind == "fields":
        fields = _list(c.get("fields"), "fields", 1, 10)
        if not all(isinstance(f, list) and len(f) == 2 for f in fields):
            raise CardError("each field must be a [label, value] pair")
        return [{"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*{_str(label, 'field label', 200)}*\n{_str(str(value), 'field value', 1700)}"}
            for label, value in fields]}]
    if kind == "table":
        return [_table(c)]
    if kind == "chart":
        return [_chart(c)]
    if kind == "buttons":
        return [_buttons(c, index)]
    raise CardError(f"unknown component type {kind!r}")


def blocks(text: str, spec: list) -> list:
    """The answer's text as sections, then the card's blocks. Raises CardError for a bad spec."""
    out = text_blocks(text)
    for index, component in enumerate(spec):
        out += component_blocks(component, index)
    charts = sum(b["type"] == "data_visualization" for b in out)
    if charts > MAX_CHARTS:
        raise CardError(f"{charts} charts; Slack shows at most {MAX_CHARTS} per message")
    tables = [b for b in out if b["type"] == "data_table"]
    if sum(len(cell["text"]) for t in tables for row in t["rows"] for cell in row) > MAX_TABLE_CHARS:
        raise CardError(f"tables hold more than {MAX_TABLE_CHARS} characters in all")
    if len(out) > MAX_BLOCKS:
        raise CardError(f"{len(out)} blocks; Slack takes at most {MAX_BLOCKS}")
    return out


def summary(spec: list) -> str:
    """One line per component the plain-text fallback leaves out, so a later turn knows what it sent."""
    parts = []
    for c in spec:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "table":
            parts.append(f"table \"{c.get('caption')}\" ({len(c.get('rows') or [])} rows)")
        elif c.get("type") == "chart":
            parts.append(f"{c.get('chart')} chart \"{c.get('title')}\"")
        elif c.get("type") == "buttons":
            parts.append("buttons: " + ", ".join(str(b.get("text")) for b in c.get("buttons") or [] if isinstance(b, dict)))
    return f"[card: {'; '.join(parts)}]" if parts else ""
