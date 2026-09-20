import hashlib
import json
import os
import re
import secrets
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
ROOMS_FILE = DATA_DIR / "rooms.json"

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
MEMBER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MEMBER_CODE_LENGTH = 6
MEMBER_COLORS = [
    "light-blue",
    "blue",
    "deep-blue",
    "light-green",
    "green",
    "deep-green",
    "light-orange",
    "orange",
    "deep-orange",
    "light-red",
    "red",
    "deep-red",
    "light-purple",
    "purple",
    "deep-purple",
    "light-pink",
    "pink",
    "deep-pink",
    "light-teal",
    "teal",
    "deep-teal",
    "light-gray",
    "gray",
    "deep-gray",
]
TIME_STEP_MINUTES = 30
MAX_RANGE_DAYS = 45
MAX_PARTICIPANTS = 20
MAX_CANDIDATES = 60
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini").strip()
LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "4000"))
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

rooms_lock = threading.RLock()
rooms = {}


def minutes_from_str(value):
    m = re.fullmatch(r"(\d{2}):(\d{2})", value)
    if not m:
        raise ValueError("time format")
    hours, minutes = int(m.group(1)), int(m.group(2))
    if hours > 24 or minutes not in (0, 30) or (hours == 24 and minutes != 0):
        raise ValueError("time step")
    return hours * 60 + minutes


def str_from_minutes(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def validate_date_text(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("date format") from exc
    return parsed


def interval_length(start_text, end_text):
    return minutes_from_str(end_text) - minutes_from_str(start_text)


def merge_interval_list(intervals):
    clean = []
    for start, end in intervals:
        start_min = minutes_from_str(start)
        end_min = minutes_from_str(end)
        if end_min - start_min >= 30:
            clean.append((start_min, end_min))
    clean.sort()
    merged = []
    for start_min, end_min in clean:
        if merged and start_min <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_min))
        else:
            merged.append((start_min, end_min))
    return [(str_from_minutes(start), str_from_minutes(end)) for start, end in merged]


def validate_availability(
    availability,
    start_date_text,
    end_date_text,
    day_start_text=None,
    day_end_text=None,
    end_time_text=None,
):
    if not isinstance(availability, list) or len(availability) > 200:
        raise ValueError("availability")
    start_date = validate_date_text(start_date_text)
    end_date = validate_date_text(end_date_text)
    day_start_min = minutes_from_str(day_start_text) if day_start_text else 0
    day_end_min = minutes_from_str(day_end_text) if day_end_text else 24 * 60
    end_time_min = minutes_from_str(end_time_text) if end_time_text else 24 * 60
    if day_end_min <= day_start_min:
        raise ValueError("day window")
    grouped = {}
    for item in availability:
        if not isinstance(item, dict):
            raise ValueError("availability")
        item_date = validate_date_text(item.get("date"))
        if not start_date <= item_date <= end_date:
            raise ValueError("date out of range")
        start = item.get("start")
        end = item.get("end")
        try:
            if interval_length(start, end) < TIME_STEP_MINUTES:
                raise ValueError("interval too short")
            upper_min = day_end_min
            if item_date == end_date and end_time_min < upper_min:
                upper_min = end_time_min
            if minutes_from_str(start) < day_start_min or minutes_from_str(end) > upper_min:
                raise ValueError("time out of range")
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and exc.args and exc.args[0] in (
                "time out of range",
                "interval too short",
            ):
                raise
            raise ValueError("interval") from exc
        grouped.setdefault(item["date"], []).append((start, end))
    normalized = []
    for item_date, intervals in grouped.items():
        for start, end in merge_interval_list(intervals):
            normalized.append({"date": item_date, "start": start, "end": end})
    return normalized


def extract_json_object(text):
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        raise ValueError("ai response")
    try:
        return json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("ai response") from exc


def call_llm_json(system_prompt, user_prompt):
    if not LLM_API_KEY:
        raise ValueError("ai not configured")
    request_body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": LLM_MAX_TOKENS,
    }
    if "deepseek" in f"{LLM_BASE_URL} {LLM_MODEL}".lower():
        request_body["response_format"] = {"type": "json_object"}
    body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("ai key") from exc
        if exc.code in (429, 500, 502, 503, 504):
            raise ValueError("ai unavailable") from exc
        raise ValueError("ai request") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ValueError("ai unavailable") from exc
    try:
        content = payload["choices"][0]["message"]["content"]
        return extract_json_object(content)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("ai response") from exc


def parse_ai_free_time(payload):
    text = str(payload.get("text", "")).strip()
    start_date = str(payload.get("startDate", "") or date.today().isoformat())
    end_date = str(payload.get("endDate", ""))
    end_time = str(payload.get("endTime", "24:00"))
    if not text or len(text) > 500:
        raise ValueError("ai text")
    start_parsed = validate_date_text(start_date)
    end_parsed = validate_date_text(end_date)
    if start_parsed > end_parsed:
        raise ValueError("date range")
    minutes_from_str(end_time)
    today = date.today()
    system_prompt = (
        "你是会齐 MeetAlign 的会议时间解析助手。用户会描述自己在约定日期窗口内的空闲时间，"
        "常见说法包括“整天都可以”“上午/下午/晚上有空”“除了某时段有会/有事外都可以”。"
        "请把用户的意思转换成可直接勾选的空闲时间片段。"
    )
    user_prompt = (
        f"今天是 {today.isoformat()} {WEEKDAY_CN[today.weekday()]}。\n"
        f"约定窗口：{start_date} 至 {end_date}，窗口最后一天最晚到 {end_time}。\n"
        "用户的空闲描述：\n"
        f"{text}\n\n"
        "只返回 JSON，不要解释，格式如下：\n"
        '{"availability":[{"date":"YYYY-MM-DD","start":"HH:00","end":"HH:00"}],'
        '"note":"用一句话复述你的理解，包括明确排除掉的不可用时段"}。\n"'
        "规则：\n"
        "1. availability 只能包含落在约定窗口内、且不晚于截止时间的空闲时间。\n"
        "2. 每段时间至少 30 分钟，start 和 end 必须按 00 或 30 分钟对齐。\n"
        "3. 用户说“都可以/其他时间都可以”时，把窗口内时间视为空闲，"
        "并排除他明确提到没空的时段。\n"
        "4. 描述有歧义时优先保守，宁可少选，也不能把用户明确说不行的时间勾上；"
        "把不确定写在 note 里。\n"
        "5. 如果窗口内没有可约时间，availability 返回 []。"
    )
    result = call_llm_json(system_prompt, user_prompt)
    if not isinstance(result, dict):
        raise ValueError("ai response")
    raw_availability = result.get("availability", [])
    if not isinstance(raw_availability, list):
        raise ValueError("ai response")
    try:
        availability = validate_availability(
            raw_availability,
            start_date,
            end_date,
            end_time_text=end_time,
        )
    except ValueError as exc:
        if exc.args and exc.args[0] in ("availability", "date out of range", "interval"):
            raise ValueError("ai response") from exc
        raise
    availability.sort(key=lambda item: (item["date"], item["start"]))
    note = str(result.get("note") or result.get("summary") or "").strip()[:300]
    return {
        "availability": availability,
        "note": note,
        "selectedCount": len(availability),
    }


def load_rooms():
    global rooms
    if ROOMS_FILE.exists():
        try:
            rooms = json.loads(ROOMS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            rooms = {}
    else:
        rooms = {}
    for room in rooms.values():
        normalize_member_colors(room)


def save_rooms():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = ROOMS_FILE.with_suffix(".tmp")
    tmp_file.write_text(
        json.dumps(rooms, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp_file.replace(ROOMS_FILE)


def generate_code():
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if code not in rooms:
            return code


def generate_member_code(room):
    used = {
        participant.get("memberCode")
        for participant in room.get("participants", [])
        if participant.get("memberCode")
    }
    while True:
        code = "".join(
            secrets.choice(MEMBER_ALPHABET) for _ in range(MEMBER_CODE_LENGTH)
        )
        if code not in used:
            return code


def member_color(room, member_no):
    return MEMBER_COLORS[(member_no - 1) % len(MEMBER_COLORS)]


def participant_by_member_code(room, member_code):
    for participant in room.get("participants", []):
        if participant.get("memberCode") == member_code:
            return participant
    return None


def ensure_member_identity(room, participant):
    if not participant.get("memberCode"):
        participant["memberCode"] = generate_member_code(room)
    return participant


def normalize_member_colors(room):
    for index, participant in enumerate(room.get("participants", []), start=1):
        participant["color"] = member_color(room, index)
    return room


def authorize_member(room, nickname, member_code):
    if not nickname or not member_code:
        raise PermissionError("identity")
    participant = participant_by_nickname(room, nickname)
    if participant is None:
        raise PermissionError("not participant")
    if participant.get("memberCode") != member_code:
        raise PermissionError("identity")
    return participant


def participant_by_nickname(room, nickname):
    for participant in room.get("participants", []):
        if participant.get("nickname") == nickname:
            return participant
    return None


def filled_participants(room):
    return [p for p in room.get("participants", []) if p.get("filled")]


def all_filled(room):
    filled = filled_participants(room)
    return len(filled) >= room.get("participantCount", 0) and len(
        room.get("participants", [])
    ) == room.get("participantCount", 0)


def participant_availability_by_date(participant):
    result = {}
    for entry in participant.get("availability", []):
        result.setdefault(entry["date"], []).append(
            (entry["start"], entry["end"])
        )
    for item_date in result:
        result[item_date] = merge_interval_list(result[item_date])
    return result


def iter_dates(start_text, end_text):
    current = validate_date_text(start_text)
    end = validate_date_text(end_text)
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def date_segments(start_text, end_text, participants):
    by_participant = [
        participant_availability_by_date(participant) for participant in participants
    ]
    segments = []
    for item_date in iter_dates(start_text, end_text):
        boundaries = {0, 24 * 60}
        for availability in by_participant:
            for start, end in availability.get(item_date, []):
                boundaries.add(minutes_from_str(start))
                boundaries.add(minutes_from_str(end))
        sorted_bounds = sorted(boundaries)
        for index in range(len(sorted_bounds) - 1):
            seg_start = sorted_bounds[index]
            seg_end = sorted_bounds[index + 1]
            if seg_end <= seg_start:
                continue
            cover = []
            for participant_index, availability in enumerate(by_participant):
                if interval_covers(availability.get(item_date, []), seg_start, seg_end):
                    cover.append(participant_index)
            segments.append(
                {
                    "date": item_date,
                    "start": str_from_minutes(seg_start),
                    "end": str_from_minutes(seg_end),
                    "cover": cover,
                }
            )
    return segments


def interval_covers(intervals, start_min, end_min):
    for start, end in intervals:
        if minutes_from_str(start) <= start_min and end_min <= minutes_from_str(end):
            return True
    return False


def plan_id(prefix, item_date, start, end, names):
    digest = hashlib.sha1(
        f"{item_date}|{start}|{end}|{'|'.join(names)}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{prefix}-{digest}"


def full_overlap_plans(room):
    participants = filled_participants(room)
    if not participants:
        return []
    segments = date_segments(room["startDate"], room["endDate"], participants)
    result = []
    current = None
    for segment in segments:
        if len(segment["cover"]) == len(participants):
            seg_start = minutes_from_str(segment["start"])
            seg_end = minutes_from_str(segment["end"])
            if current and current["date"] == segment["date"] and minutes_from_str(
                current["end"]
            ) == seg_start:
                current["end"] = segment["end"]
                current["minutes"] += seg_end - seg_start
            else:
                current = {
                    "id": "",
                    "date": segment["date"],
                    "start": segment["start"],
                    "end": segment["end"],
                    "minutes": seg_end - seg_start,
                }
                result.append(current)
    names = [p["nickname"] for p in participants]
    for plan in result:
        plan["id"] = plan_id("full", plan["date"], plan["start"], plan["end"], names)
        plan["availableCount"] = len(participants)
        plan["unavailable"] = []
    result.sort(key=lambda plan: (-plan["minutes"], plan["date"], plan["start"]))
    return result


def plan_time_preference(plan):
    start = minutes_from_str(plan["start"])
    end = minutes_from_str(plan["end"])
    weekday = validate_date_text(plan["date"]).weekday()
    minutes = max(30, end - start)
    score = 0.0
    # 9:00-23:00 属于可工作/可娱乐时段；深夜和凌晨放到最末。
    if start >= 9 * 60 and end <= 23 * 60:
        score += 4.0
    if 18 * 60 <= start and end <= 23 * 60:
        score += 1.0
    if weekday >= 5:
        score += 0.3
    if start < 7 * 60 or end <= 7 * 60:
        score -= 8.0
    if start >= 23 * 60 or end > 23 * 60:
        score -= 4.0
    score += min(minutes / 1440, 1.0) * 0.5
    return score


def ai_plan_pool(room):
    plans = full_overlap_plans(room)
    plans.sort(
        key=lambda plan: (
            -plan_time_preference(plan),
            -plan["minutes"],
            plan["date"],
            plan["start"],
        )
    )
    return plans[:24]


def find_plan_by_suggestion(pool, item):
    plan_id = str(item.get("planId") or item.get("id") or "")
    for plan in pool:
        if plan["id"] == plan_id:
            return plan
    item_date = str(item.get("date", ""))
    item_start = str(item.get("start", ""))
    item_end = str(item.get("end", ""))
    for plan in pool:
        if (
            plan["date"] == item_date
            and plan["start"] == item_start
            and plan["end"] == item_end
        ):
            return plan
    return None


def generate_ai_plan_suggestions(room, pool):
    today = date.today()
    lines = []
    for index, plan in enumerate(pool, start=1):
        parsed_date = validate_date_text(plan["date"])
        weekday = WEEKDAY_CN[parsed_date.weekday()]
        lines.append(
            f"{index}. id={plan['id']} 日期={plan['date']} {weekday} "
            f"{plan['start']}-{plan['end']} 共{plan['minutes']}分钟 "
            f"{plan['availableCount']}/{room['participantCount']}人可到"
        )
    system_prompt = (
        "你是会齐 MeetAlign 的会议方案助手。请从给出的全员可约候选中，"
        "挑选 1 到 2 个最符合大家作息的时间方案。"
        "优先考虑 9:00-23:00 之间可工作、可聚会或可娱乐的时段；"
        "深夜、凌晨排在最后，尽量不选。"
        "只能从候选 id 中选择，不要自己编造时间。"
    )
    user_prompt = (
        f"今天是 {today.isoformat()} {WEEKDAY_CN[today.weekday()]}。\n"
        f"约定主题：{room.get('topic', '')}\n"
        f"参与人数：{room['participantCount']}\n"
        "全员可约候选：\n"
        + "\n".join(lines)
        + '\n\n只返回 JSON：{"suggestions":[{"planId":"候选 id","reason":"为什么推荐"}],"note":"一句话总结"}'
    )
    result = call_llm_json(system_prompt, user_prompt)
    if not isinstance(result, dict):
        raise ValueError("ai response")
    raw_suggestions = result.get("suggestions", [])
    if not isinstance(raw_suggestions, list):
        raise ValueError("ai response")
    suggestions = []
    for item in raw_suggestions:
        if not isinstance(item, dict):
            continue
        plan = find_plan_by_suggestion(pool, item)
        if plan is None:
            continue
        suggestions.append(
            {
                "planId": plan["id"],
                "date": plan["date"],
                "start": plan["start"],
                "end": plan["end"],
                "minutes": plan["minutes"],
                "availableCount": plan["availableCount"],
                "unavailable": [],
                "reason": str(item.get("reason") or "").strip()[:300],
            }
        )
        if len(suggestions) >= 2:
            break
    if not suggestions:
        raise ValueError("ai response")
    return suggestions


def ai_suggest_room(code, payload):
    nickname = str(payload.get("nickname", "")).strip()
    member_code = str(payload.get("memberCode", "")).strip().upper()
    with rooms_lock:
        room = rooms.get(code)
        if not room:
            raise KeyError("room")
        if room.get("chosen"):
            raise PermissionError("scheduled")
        if not all_filled(room):
            raise PermissionError("not ready")
        authorize_member(room, nickname, member_code)
        if room.get("aiSuggestions"):
            return room
        pool = ai_plan_pool(room)
        if not pool:
            room["aiSuggestions"] = []
            save_rooms()
            return room
    suggestions = generate_ai_plan_suggestions(room, pool)
    with rooms_lock:
        current = rooms.get(code)
        if current and not current.get("chosen"):
            current["aiSuggestions"] = suggestions
            save_rooms()
        else:
            current = room
        return current


def partial_candidates(room):
    participants = filled_participants(room)
    if not participants:
        return []
    segments = date_segments(room["startDate"], room["endDate"], participants)
    total = len(participants)
    runs = []
    current = None
    for segment in segments:
        cover = tuple(sorted(segment["cover"]))
        if not cover or len(cover) >= total:
            current = None
            continue
        seg_start = minutes_from_str(segment["start"])
        seg_end = minutes_from_str(segment["end"])
        if current and current["cover"] == cover and current["date"] == segment["date"] and (
            minutes_from_str(current["end"]) == seg_start
        ):
            current["end"] = segment["end"]
            current["minutes"] += seg_end - seg_start
        else:
            current = {
                "id": "",
                "date": segment["date"],
                "start": segment["start"],
                "end": segment["end"],
                "cover": cover,
                "minutes": seg_end - seg_start,
            }
            runs.append(current)

    names = [p["nickname"] for p in participants]
    candidates = []
    for run in runs:
        free_names = [names[index] for index in run["cover"]]
        unavailable = [
            name for name in names if name not in set(free_names)
        ]
        run_id = plan_id(
            "part", run["date"], run["start"], run["end"], sorted(free_names)
        )
        candidates.append(
            {
                "id": run_id,
                "date": run["date"],
                "start": run["start"],
                "end": run["end"],
                "minutes": minutes_from_str(run["end"])
                - minutes_from_str(run["start"]),
                "availableCount": len(free_names),
                "free": free_names,
                "unavailable": unavailable,
            }
        )
    candidates.sort(
        key=lambda item: (
            -item["availableCount"],
            -item["minutes"],
            item["date"],
            item["start"],
        )
    )
    return candidates[:MAX_CANDIDATES]


def active_candidate(room):
    if not all_filled(room) or room.get("chosen"):
        return None
    candidates = partial_candidates(room)
    rejected = set(room.get("rejectedCandidateIds", []))
    for candidate in candidates:
        if candidate["id"] not in rejected:
            return candidate
    return None


def set_status(room):
    if room.get("chosen"):
        room["status"] = "scheduled"
        return
    if not all_filled(room):
        room["status"] = "filling"
        return
    if full_overlap_plans(room):
        room["status"] = "deciding"
        return
    room["status"] = "coordinating" if active_candidate(room) else "need_round"


def normalize_room_participant_names(room):
    seen = set()
    kept = []
    for participant in room.get("participants", []):
        nickname = participant.get("nickname", "")
        if not nickname or nickname in seen:
            continue
        seen.add(nickname)
        kept.append(participant)
    room["participants"] = kept


def create_room(payload):
    topic = str(payload.get("topic", "")).strip()
    nickname = str(payload.get("nickname", "")).strip()
    participant_count = payload.get("participantCount")
    start_date = str(payload.get("startDate", "") or date.today().isoformat())
    end_date = str(payload.get("endDate", ""))
    end_time = str(payload.get("endTime", "24:00"))
    if not topic or len(topic) > 80:
        raise ValueError("topic")
    if not nickname or len(nickname) > 20:
        raise ValueError("nickname")
    try:
        count = int(participant_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("participantCount") from exc
    if count < 2 or count > MAX_PARTICIPANTS:
        raise ValueError("participantCount")
    start_parsed = validate_date_text(start_date)
    end_parsed = validate_date_text(end_date)
    if start_parsed > end_parsed:
        raise ValueError("date range")
    if (end_parsed - start_parsed).days + 1 > MAX_RANGE_DAYS:
        raise ValueError("date range too long")
    minutes_from_str(end_time)
    availability = validate_availability(
        payload.get("availability", []),
        start_date,
        end_date,
        end_time_text=end_time,
    )
    if not availability:
        raise ValueError("availability required")
    room = {
        "code": generate_code(),
        "topic": topic,
        "creator": nickname,
        "participantCount": count,
        "startDate": start_date,
        "endDate": end_date,
        "endTime": end_time,
        "round": 1,
        "status": "filling",
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "participants": [
            {
                "nickname": nickname,
                "isCreator": True,
                "filled": True,
                "availability": availability,
                "updatedAt": datetime.now().isoformat(timespec="seconds"),
                "memberCode": generate_member_code(
                    {
                        "participants": [],
                    }
                ),
                "color": MEMBER_COLORS[0],
            }
        ],
        "chosen": None,
        "rejectedCandidateIds": [],
    }
    creator_code = room["participants"][0]["memberCode"]
    creator_color = room["participants"][0]["color"]
    with rooms_lock:
        rooms[room["code"]] = room
        save_rooms()
    room["creatorMemberCode"] = creator_code
    room["creatorColor"] = creator_color
    return room


def identify_in_room(code, payload):
    nickname = str(payload.get("nickname", "")).strip()
    member_code = str(payload.get("memberCode", "")).strip().upper()
    if not nickname or len(nickname) > 20:
        raise ValueError("nickname")
    with rooms_lock:
        room = rooms.get(code)
        if not room:
            raise KeyError("room")
        if room.get("chosen"):
            raise PermissionError("scheduled")
        existing = participant_by_nickname(room, nickname)
        if existing is not None:
            if existing.get("memberCode"):
                if not member_code or existing["memberCode"] != member_code:
                    raise PermissionError("identity")
            else:
                existing["memberCode"] = generate_member_code(room)
                normalize_member_colors(room)
            participant = existing
        else:
            if len(room.get("participants", [])) >= room["participantCount"]:
                raise LookupError("room_full")
            next_no = len(room.get("participants", [])) + 1
            next_color = member_color(room, next_no)
            participant = {
                "nickname": nickname,
                "isCreator": nickname == room["creator"],
                "filled": False,
                "availability": [],
                "updatedAt": datetime.now().isoformat(timespec="seconds"),
                "memberCode": member_code or generate_member_code(room),
                "color": next_color,
            }
            if member_code and participant_by_member_code(room, member_code):
                raise PermissionError("identity")
            room["participants"].append(participant)
            normalize_member_colors(room)
        save_rooms()
        return {
            "code": room["code"],
            "nickname": participant["nickname"],
            "memberCode": participant["memberCode"],
            "color": participant["color"],
            "memberNo": room.get("participants", []).index(participant) + 1,
            "isCreator": participant.get("isCreator", False),
        }


def respond_to_room(code, payload):
    nickname = str(payload.get("nickname", "")).strip()
    member_code = str(payload.get("memberCode", "")).strip().upper()
    if not nickname or len(nickname) > 20:
        raise ValueError("nickname")
    with rooms_lock:
        room = rooms.get(code)
        if not room:
            raise KeyError("room")
        if room.get("chosen"):
            raise PermissionError("scheduled")
        participant = authorize_member(room, nickname, member_code)
        availability = validate_availability(
            payload.get("availability", []),
            room["startDate"],
            room["endDate"],
            room.get("dayStart"),
            room.get("dayEnd"),
            room.get("endTime"),
        )
        participant["availability"] = availability
        participant["filled"] = True
        participant["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        room["aiSuggestions"] = []
        room.pop("aiSuggestionError", None)
        room["rejectedCandidateIds"] = []
        set_status(room)
        save_rooms()
        return room


def choose_plan(code, payload):
    nickname = str(payload.get("nickname", "")).strip()
    member_code = str(payload.get("memberCode", "")).strip().upper()
    plan_id_value = str(payload.get("planId", ""))
    with rooms_lock:
        room = rooms.get(code)
        if not room:
            raise KeyError("room")
        if room.get("chosen"):
            raise PermissionError("already scheduled")
        if not all_filled(room):
            raise PermissionError("not ready")
        plans = full_overlap_plans(room)
        selected = next((plan for plan in plans if plan["id"] == plan_id_value), None)
        if selected is None:
            raise LookupError("plan")
        authorize_member(room, nickname, member_code)
        room["chosen"] = {
            "date": selected["date"],
            "start": selected["start"],
            "end": selected["end"],
            "chosenBy": nickname,
            "chosenAt": datetime.now().isoformat(timespec="seconds"),
        }
        room["status"] = "scheduled"
        save_rooms()
        return room


def adjust_for_candidate(code, payload):
    nickname = str(payload.get("nickname", "")).strip()
    member_code = str(payload.get("memberCode", "")).strip().upper()
    plan_id_value = str(payload.get("planId", ""))
    with rooms_lock:
        room = rooms.get(code)
        if not room:
            raise KeyError("room")
        candidate = active_candidate(room)
        if not candidate or candidate["id"] != plan_id_value:
            raise LookupError("candidate")
        if nickname not in candidate["unavailable"]:
            raise PermissionError("not blocker")
        participant = authorize_member(room, nickname, member_code)
        participant.setdefault("availability", []).append(
            {
                "date": candidate["date"],
                "start": candidate["start"],
                "end": candidate["end"],
            }
        )
        participant["availability"] = validate_availability(
            participant["availability"],
            room["startDate"],
            room["endDate"],
            room.get("dayStart"),
            room.get("dayEnd"),
            room.get("endTime"),
        )
        participant["filled"] = True
        participant["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        room["aiSuggestions"] = []
        room.pop("aiSuggestionError", None)
        room["rejectedCandidateIds"] = []
        set_status(room)
        save_rooms()
        return room


def skip_candidate(code, payload):
    nickname = str(payload.get("nickname", "")).strip()
    member_code = str(payload.get("memberCode", "")).strip().upper()
    with rooms_lock:
        room = rooms.get(code)
        if not room:
            raise KeyError("room")
        if room.get("chosen"):
            raise PermissionError("scheduled")
        if not all_filled(room):
            raise PermissionError("not ready")
        candidate = active_candidate(room)
        if not candidate:
            raise LookupError("candidate")
        participant = authorize_member(room, nickname, member_code)
        if nickname != room["creator"] and nickname not in candidate["unavailable"]:
            raise PermissionError("not blocker")
        rejected = room.setdefault("rejectedCandidateIds", [])
        if candidate["id"] not in rejected:
            rejected.append(candidate["id"])
        set_status(room)
        save_rooms()
        return room


def start_new_round(code, payload):
    nickname = str(payload.get("nickname", "")).strip()
    member_code = str(payload.get("memberCode", "")).strip().upper()
    start_date = str(payload.get("startDate", ""))
    end_date = str(payload.get("endDate", ""))
    with rooms_lock:
        room = rooms.get(code)
        if not room:
            raise KeyError("room")
        if nickname != room["creator"]:
            raise PermissionError("not creator")
        authorize_member(room, nickname, member_code)
        end_time = str(payload.get("endTime", room.get("endTime", "24:00")))
        start_parsed = validate_date_text(start_date)
        end_parsed = validate_date_text(end_date)
        if start_parsed > end_parsed:
            raise ValueError("date range")
        if (end_parsed - start_parsed).days + 1 > MAX_RANGE_DAYS:
            raise ValueError("date range too long")
        minutes_from_str(end_time)
        room["startDate"] = start_date
        room["endDate"] = end_date
        room["endTime"] = end_time
        room["round"] = int(room.get("round", 1)) + 1
        room["status"] = "filling"
        room["chosen"] = None
        room["rejectedCandidateIds"] = []
        room["aiSuggestions"] = []
        room.pop("aiSuggestionError", None)
        for participant in room.get("participants", []):
            participant["availability"] = []
            participant["filled"] = False
            participant["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        save_rooms()
        return room


def room_member_codes(code, payload):
    nickname = str(payload.get("nickname", "")).strip()
    member_code = str(payload.get("memberCode", "")).strip().upper()
    with rooms_lock:
        room = rooms.get(code)
        if not room:
            raise KeyError("room")
        if nickname != room["creator"]:
            raise PermissionError("not creator")
        authorize_member(room, nickname, member_code)
        members = []
        for index, participant in enumerate(room.get("participants", []), start=1):
            members.append(
                {
                    "memberNo": index,
                    "nickname": participant["nickname"],
                    "color": participant.get("color", "gray"),
                    "memberCode": participant.get("memberCode", ""),
                }
            )
        return {"members": members}


def public_room(room):
    with rooms_lock:
        set_status(room)
        filled = filled_participants(room)
        participants = []
        for index, participant in enumerate(room.get("participants", []), start=1):
            participants.append(
                {
                    "nickname": participant["nickname"],
                    "isCreator": participant.get("isCreator", False),
                    "filled": participant.get("filled", False),
                    "availability": participant.get("availability", []),
                    "updatedAt": participant.get("updatedAt", ""),
                    "memberNo": index,
                    "color": participant.get("color", "gray"),
                    "hasMemberCode": bool(participant.get("memberCode")),
                }
            )
        plans = full_overlap_plans(room) if all_filled(room) else []
        candidates = partial_candidates(room) if all_filled(room) else []
        active = active_candidate(room)
        payload = {
            "code": room["code"],
            "topic": room["topic"],
            "creator": room["creator"],
            "participantCount": room["participantCount"],
            "filledCount": len(filled),
            "startDate": room["startDate"],
            "endDate": room["endDate"],
            "dayStart": room.get("dayStart", "08:00"),
            "dayEnd": room.get("dayEnd", "22:00"),
            "endTime": room.get("endTime", "24:00"),
            "round": room["round"],
            "status": room["status"],
            "createdAt": room.get("createdAt", ""),
            "participants": participants,
            "plans": plans,
            "candidates": candidates,
            "activeCandidate": active,
            "aiSuggestions": room.get("aiSuggestions", []),
            "rejectedCount": len(room.get("rejectedCandidateIds", [])),
            "chosen": room.get("chosen"),
        }
        return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "MeetAlign/1.0"

    def log_message(self, format_string, *args):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("request") from exc
        if length <= 0 or length > 1024 * 1024:
            raise ValueError("request")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("request") from exc

    def send_static(self, path):
        if path in ("/", "/index.html"):
            path = "/index.html"
        elif path.startswith("/r/") or not path.startswith("/"):
            path = "/index.html"
        try:
            relative = Path(path[1:])
            full_path = (STATIC_DIR / relative).resolve()
            full_path.relative_to(STATIC_DIR.resolve())
        except (ValueError, OSError):
            self.send_error(404)
            return
        if not full_path.is_file():
            full_path = STATIC_DIR / "index.html"
        try:
            body = full_path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }
        self.send_response(200)
        self.send_header(
            "Content-Type",
            content_types.get(full_path.suffix.lower(), "application/octet-stream"),
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def handle_api_error(self, exc):
        if isinstance(exc, KeyError):
            self.send_json(404, {"error": "房间不存在或已失效", "code": "not_found"})
        elif isinstance(exc, LookupError):
            if exc.args and exc.args[0] == "room_full":
                self.send_json(409, {"error": "人数已满", "code": "room_full"})
            else:
                self.send_json(404, {"error": "请求的方案不存在", "code": "not_found"})
        elif isinstance(exc, PermissionError):
            messages = {
                "scheduled": "时间已确定，不能继续修改",
                "not ready": "还有人没有填写，暂不能计算",
                "not participant": "请先使用自己的昵称加入",
                "not creator": "只有发起人可以发起新一轮",
                "not blocker": "该时段没有排除你的时间",
                "already scheduled": "时间已经确定",
                "identity": "成员身份验证失败，请使用自己的成员访问码",
            }
            message = messages.get(exc.args[0] if exc.args else "", "操作不允许")
            self.send_json(403, {"error": message, "code": "forbidden"})
        elif isinstance(exc, ValueError):
            messages = {
                "topic": "请填写约定主题（80 字以内）",
                "nickname": "昵称不能为空且不超过 20 字",
                "participantCount": "人数需为 2 到 20",
                "date range": "结束日期不能早于开始日期",
                "date range too long": "可约日期最多 45 天",
                "date format": "日期格式不正确",
                "availability": "空闲时间格式不正确",
                "date out of range": "空闲时间超出了可约日期",
                "interval too short": "空闲时间最短 30 分钟",
                "interval": "时间格式不正确，需按 30 分钟对齐",
                "day window": "每天可约时间的结束需要晚于开始",
                "time out of range": "空闲时间超出了 DDL 时间",
                "availability required": "请先在日历中选择自己的空闲时间",
                "ai text": "请先填写一句空闲时间描述",
                "ai not configured": "AI 功能未启用，请先在服务器配置 LLM_API_KEY",
                "ai key": "AI 服务认证失败，请检查 LLM_API_KEY 或 LLM_BASE_URL",
                "ai unavailable": "AI 服务暂时不可用，请稍后重试或手动选择时间",
                "ai request": "AI 请求失败，请检查 LLM_BASE_URL 与 LLM_MODEL 配置",
                "ai response": "AI 返回的内容无法解析，请换个说法重试",
                "request": "请求内容不正确",
            }
            message = messages.get(exc.args[0] if exc.args else "", "请求参数有误")
            self.send_json(400, {"error": message, "code": "bad_request"})
        else:
            self.send_json(500, {"error": "服务器暂时开小差了", "code": "server_error"})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            try:
                if path == "/api/health":
                    self.send_json(200, {"ok": True})
                    return
                if path == "/api/config":
                    self.send_json(
                        200,
                        {
                            "publicBaseUrl": PUBLIC_BASE_URL,
                            "aiEnabled": bool(LLM_API_KEY),
                        },
                    )
                    return
                match = re.fullmatch(r"/api/room/([A-Z0-9]+)", path)
                if match:
                    code = match.group(1)
                    with rooms_lock:
                        room = rooms.get(code)
                        if not room:
                            raise KeyError("room")
                        self.send_json(200, public_room(room))
                    return
                self.send_json(404, {"error": "接口不存在", "code": "not_found"})
            except Exception as exc:  # noqa: BLE001
                self.handle_api_error(exc)
            return
        self.send_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            self.send_json(404, {"error": "接口不存在", "code": "not_found"})
            return
        try:
            payload = self.read_json()
            if path == "/api/rooms":
                room = create_room(payload)
                response = public_room(room)
                response["creatorMemberCode"] = room["participants"][0].get(
                    "memberCode", ""
                )
                response["creatorColor"] = room["participants"][0].get("color", "")
                self.send_json(200, response)
                return
            if path == "/api/ai/parse_free_time":
                self.send_json(200, parse_ai_free_time(payload))
                return
            match = re.fullmatch(r"/api/room/([A-Z0-9]+)/([a-z_]+)", path)
            if not match:
                self.send_json(404, {"error": "接口不存在", "code": "not_found"})
                return
            code, action = match.group(1), match.group(2)
            actions = {
                "identify": identify_in_room,
                "member_codes": room_member_codes,
                "respond": respond_to_room,
                "choose": choose_plan,
                "adjust": adjust_for_candidate,
                "skip": skip_candidate,
                "new_round": start_new_round,
                "ai_suggest": ai_suggest_room,
            }
            if action not in actions:
                self.send_json(404, {"error": "接口不存在", "code": "not_found"})
                return
            result = actions[action](code, payload)
            if action == "identify":
                self.send_json(200, result)
            elif action == "member_codes":
                self.send_json(200, result)
            else:
                self.send_json(200, public_room(result))
        except Exception as exc:  # noqa: BLE001
            self.handle_api_error(exc)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_rooms()
    port = int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"会齐 MeetAlign 已启动：http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
