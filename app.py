from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from datetime import datetime
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request, jsonify, render_template_string, redirect, url_for

try:
    from twilio.rest import Client  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    Client = None  # type: ignore

try:
    from sklearn.cluster import KMeans  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    KMeans = None  # type: ignore


app = Flask(__name__)


# In-memory announcements store
announcements: List[Dict[str, Any]] = []
next_announcement_id: int = 1


EVENTS_FILE = "events.json"
SMS_RECIPIENTS_FILE = "sms_recipients.json"


def iso_utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def client_ip() -> str:
    # Prefer X-Forwarded-For if present (first hop), otherwise remote_addr
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or ""


def append_event(event: Dict[str, Any]) -> None:
    line = json.dumps(event, ensure_ascii=False)
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _dedupe_preserve_order(values: List[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def load_sms_recipients_from_file() -> List[str]:
    if not os.path.exists(SMS_RECIPIENTS_FILE):
        return []
    try:
        with open(SMS_RECIPIENTS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        return []
    if not content:
        return []
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = None
    numbers: List[str] = []
    if isinstance(data, list):
        numbers = [str(item).strip() for item in data if str(item).strip()]
    elif isinstance(data, str):
        numbers = [part.strip() for part in data.replace("\n", ",").split(",") if part.strip()]
    else:
        numbers = [part.strip() for part in content.replace("\n", ",").split(",") if part.strip()]
    return _dedupe_preserve_order(numbers)


def save_sms_recipients(raw_numbers: str) -> List[str]:
    flattened = raw_numbers.replace("\n", ",")
    numbers = _dedupe_preserve_order(flattened.split(","))
    if numbers:
        with open(SMS_RECIPIENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(numbers, f)
    else:
        if os.path.exists(SMS_RECIPIENTS_FILE):
            os.remove(SMS_RECIPIENTS_FILE)
    return numbers


def get_env_sms_recipients() -> List[str]:
    env_value = os.environ.get("SMS_RECIPIENTS", "")
    if not env_value:
        return []
    return _dedupe_preserve_order(env_value.replace("\n", ",").split(","))


def get_sms_recipients() -> List[str]:
    numbers = load_sms_recipients_from_file() + get_env_sms_recipients()
    return _dedupe_preserve_order(numbers)


def get_twilio_client() -> Optional[Client]:
    if Client is None:
        return None
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        return None
    try:
        return Client(account_sid, auth_token)
    except Exception:
        return None


def send_sms_alert(event: Dict[str, Any], announcement: Optional[Dict[str, Any]]) -> None:
    client = get_twilio_client()
    if client is None:
        return
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    if not from_number:
        return
    recipients = get_sms_recipients()
    if not recipients:
        return

    title = announcement["title"] if announcement else "Announcement"
    target = event.get("target") or (announcement["target"] if announcement else "")
    event_type = event.get("event", "event").replace("_", " ").title()
    timestamp = event.get("timestamp", iso_utc_now())
    user = event.get("user", "") or "anonymous"
    announcement_id = event.get("announcementId") or "N/A"

    message_lines = [
        f"{event_type}: {title}",
        f"User: {user}",
        f"Announcement ID: {announcement_id}",
        f"Time: {timestamp}",
    ]
    if target:
        message_lines.append(f"Target: {target}")
    body = "\n".join(message_lines)

    for number in recipients:
        try:
            client.messages.create(to=number, from_=from_number, body=body)
        except Exception:
            continue


def normalize_user(value: Optional[str]) -> str:
    user = (value or "").strip()
    return user or "anonymous"


def parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def build_user_stats(events: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
    user_stats: Dict[str, Dict[str, Any]] = {}
    open_events: Dict[Tuple[Optional[str], str], deque] = defaultdict(deque)

    for event in events:
        event_type = event.get("event")
        announcement_id = event.get("announcementId")
        user = normalize_user(event.get("user"))
        timestamp = parse_iso_timestamp(event.get("timestamp"))
        target = event.get("target") or ""

        stats = user_stats.setdefault(
            user,
            {
                "open_count": 0,
                "ack_count": 0,
                "ack_delays": [],
                "last_event_ts": None,
                "targets": set(),
            },
        )

        if timestamp and (stats["last_event_ts"] is None or timestamp > stats["last_event_ts"]):
            stats["last_event_ts"] = timestamp
        if target:
            stats["targets"].add(target)

        key = (announcement_id, user)

        if event_type == "opened":
            stats["open_count"] += 1
            if timestamp is not None:
                open_events[key].append(
                    {
                        "timestamp": timestamp,
                        "target": target,
                        "announcementId": announcement_id,
                    }
                )
        elif event_type == "acknowledged":
            stats["ack_count"] += 1
            if timestamp is not None and open_events[key]:
                opened_event = open_events[key].popleft()
                opened_ts = opened_event.get("timestamp")
                if opened_ts and timestamp >= opened_ts:
                    delay_seconds = (timestamp - opened_ts).total_seconds()
                    stats["ack_delays"].append(delay_seconds)

    outstanding_by_user: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for (announcement_id, user), queue in open_events.items():
        while queue:
            opened_event = queue.popleft()
            outstanding_by_user[user].append(
                {
                    "announcementId": announcement_id,
                    "opened_at": opened_event.get("timestamp"),
                    "target": opened_event.get("target"),
                }
            )

    return user_stats, outstanding_by_user


def calculate_engagement_score(stats: Dict[str, Any]) -> float:
    opens = stats.get("open_count", 0)
    acks = stats.get("ack_count", 0)
    ack_rate = acks / opens if opens else 0.0
    delays = stats.get("ack_delays", [])
    avg_delay_hours = mean(delays) / 3600 if delays else 12.0
    delay_score = max(0.0, min(1.0, 1 - (avg_delay_hours / 24)))
    activity = min(opens + acks, 20) / 20
    return round((0.6 * ack_rate) + (0.25 * delay_score) + (0.15 * activity), 4)


def profile_users(user_stats: Dict[str, Any], outstanding: Dict[str, List[Dict[str, Any]]]) -> Tuple[Dict[str, Dict[str, Any]], str]:
    profiles: Dict[str, Dict[str, Any]] = {}
    features: List[List[float]] = []
    ordered_users: List[str] = []

    for user, stats in user_stats.items():
        opens = stats.get("open_count", 0)
        acks = stats.get("ack_count", 0)
        ack_rate = acks / opens if opens else 0.0
        delays = stats.get("ack_delays", [])
        avg_delay_minutes = mean(delays) / 60 if delays else None
        score = calculate_engagement_score(stats)
        profiles[user] = {
            "opens": opens,
            "acks": acks,
            "ack_rate": round(ack_rate * 100, 1),
            "avg_delay_minutes": round(avg_delay_minutes, 1) if avg_delay_minutes is not None else None,
            "score": score,
            "classification": "",
            "outstanding": len(outstanding.get(user, [])),
        }

        feature_vector = [
            float(opens),
            float(acks),
            ack_rate,
            (avg_delay_minutes / 60) if avg_delay_minutes is not None else 0.5,
        ]
        features.append(feature_vector)
        ordered_users.append(user)

    engine = "heuristic"
    if KMeans is not None and len(features) >= 3:
        try:
            n_clusters = min(3, len(features))
            model = KMeans(n_clusters=n_clusters, n_init=10, random_state=0)
            labels = model.fit_predict(features)
            cluster_scores: Dict[int, float] = defaultdict(float)
            cluster_counts: Dict[int, int] = defaultdict(int)
            for idx, user in enumerate(ordered_users):
                label = labels[idx]
                cluster_scores[label] += profiles[user]["score"]
                cluster_counts[label] += 1
            averaged_scores = {
                label: cluster_scores[label] / cluster_counts[label]
                for label in cluster_scores
                if cluster_counts[label]
            }
            # Higher score => more engaged
            ordered_labels = sorted(averaged_scores, key=lambda lbl: averaged_scores[lbl], reverse=True)
            segment_names = ["Highly Engaged", "Steady", "Needs Attention"]
            mapping: Dict[int, str] = {}
            for idx, label in enumerate(ordered_labels):
                mapping[label] = segment_names[idx] if idx < len(segment_names) else segment_names[-1]
            for idx, user in enumerate(ordered_users):
                label = labels[idx]
                profiles[user]["classification"] = mapping.get(label, "Steady")
            engine = "sklearn-kmeans"
        except Exception:
            engine = "heuristic"

    if engine == "heuristic":
        for user, profile in profiles.items():
            score = profile["score"]
            ack_rate = profile["ack_rate"] / 100 if profile["ack_rate"] is not None else 0.0
            if score >= 0.7 and ack_rate >= 0.65:
                profile["classification"] = "Highly Engaged"
            elif score >= 0.45:
                profile["classification"] = "Steady"
            else:
                profile["classification"] = "Needs Attention"

    return profiles, engine


def generate_user_analytics(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    analytics = {
        "overall": {
            "total_events": len(events),
            "total_users": 0,
            "conversion_rate": 0.0,
            "avg_ack_minutes": None,
        },
        "leaders": [],
        "risks": [],
        "insights": [],
        "engine": "heuristic",
    }

    if not events:
        analytics["insights"].append("No engagement events recorded yet. Ask users to open and acknowledge announcements to gather data.")
        return analytics

    user_stats, outstanding = build_user_stats(events)
    profiles, engine = profile_users(user_stats, outstanding)
    analytics["engine"] = engine

    total_opens = sum(stats.get("open_count", 0) for stats in user_stats.values())
    total_acks = sum(stats.get("ack_count", 0) for stats in user_stats.values())
    conversion_rate = (total_acks / total_opens * 100) if total_opens else 0.0
    all_delays = [delay for stats in user_stats.values() for delay in stats.get("ack_delays", [])]
    avg_ack_minutes = mean(all_delays) / 60 if all_delays else None

    analytics["overall"].update(
        {
            "total_users": len(user_stats),
            "conversion_rate": round(conversion_rate, 1),
            "avg_ack_minutes": round(avg_ack_minutes, 1) if avg_ack_minutes is not None else None,
        }
    )

    outstanding_count = sum(len(items) for items in outstanding.values())
    if outstanding_count:
        analytics["insights"].append(
            f"{outstanding_count} pending acknowledgement(s) across {len(outstanding)} user(s). Prioritise follow-up."
        )

    sorted_profiles = sorted(profiles.items(), key=lambda item: item[1]["score"], reverse=True)
    for user, profile in sorted_profiles[:3]:
        analytics["leaders"].append(
            {
                "user": user,
                "score": profile["score"],
                "classification": profile["classification"],
                "ack_rate": profile["ack_rate"],
                "avg_delay_minutes": profile["avg_delay_minutes"],
            }
        )

    risk_candidates = []
    for user, profile in profiles.items():
        ack_rate = profile["ack_rate"] / 100 if profile["ack_rate"] is not None else 0.0
        avg_delay_hours = (profile["avg_delay_minutes"] or 90) / 60
        outstanding_tasks = profile["outstanding"]
        if ack_rate < 0.6 or outstanding_tasks:
            risk_score = (1 - ack_rate) + min(outstanding_tasks, 3) * 0.2 + min(avg_delay_hours / 12, 1)
            risk_candidates.append(
                {
                    "user": user,
                    "ack_rate": round(ack_rate * 100, 1),
                    "outstanding": outstanding_tasks,
                    "avg_delay_minutes": profile["avg_delay_minutes"],
                    "classification": profile["classification"],
                    "risk_score": round(risk_score, 3),
                }
            )

    risk_candidates.sort(key=lambda item: item["risk_score"], reverse=True)
    analytics["risks"] = risk_candidates[:3]

    if analytics["overall"]["conversion_rate"] < 60:
        analytics["insights"].append("Overall conversion rate is under 60%. Consider reinforcing acknowledgements in upcoming communications.")

    if analytics["overall"]["avg_ack_minutes"] and analytics["overall"]["avg_ack_minutes"] > 180:
        analytics["insights"].append(
            "Average acknowledgement time exceeds 3 hours. Send reminders or shorten tasks to encourage quicker responses."
        )

    if not analytics["insights"]:
        analytics["insights"].append("Engagement metrics look healthy. Continue monitoring for trends.")

    return analytics


@app.post("/announcement")
def create_announcement():
    global next_announcement_id
    data = request.get_json(silent=True) or {}
    title = data.get("title")
    details = data.get("details")
    target = data.get("target")

    if not title or not details or not target:
        return jsonify({"error": "Missing required fields: title, details, target"}), 400

    ann = {
        "id": next_announcement_id,
        "title": title,
        "details": details,
        "target": target,
        "createdAt": iso_utc_now(),
    }
    announcements.append(ann)
    next_announcement_id += 1

    # Build tracking link: http://localhost:5000/track?id=123&target=https://example.com
    base = request.host_url.rstrip("/")
    tracking_link = f"{base}/track?id={ann['id']}&target={target}"

    return jsonify({
        "announcement": ann,
        "track": tracking_link,
    }), 201


@app.get("/track")
def track_open():
    # Parameters
    ann_id = request.args.get("id")
    target = request.args.get("target")
    user = request.args.get("user") or request.args.get("username") or "anonymous"

    # Normalize/validate id
    try:
        announcement_id = int(ann_id) if ann_id is not None else None
    except ValueError:
        return ("Invalid id", 400)

    # Find announcement details if available
    ann = None
    if announcement_id is not None:
        for a in announcements:
            if a["id"] == announcement_id:
                ann = a
                break

    device = request.headers.get("User-Agent", "")
    event = {
        "event": "opened",
        "announcementId": announcement_id,
        "user": user,
        "target": target,
        "timestamp": iso_utc_now(),
        "device": device,
        "ip": client_ip(),
    }
    append_event(event)
    send_sms_alert(event, ann)

    prefill_user = request.args.get("user") or request.args.get("username") or ""

    # Render announcement details along with direct link to the task and an acknowledgement form
    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Announcement</title>
        <style>
          body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; }
          .card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; max-width: 800px; margin-bottom: 1.5rem; }
          .actions { margin-top: 1.25rem; display: flex; flex-direction: column; gap: 0.5rem; }
          .btn { display: inline-flex; align-items: center; justify-content: center; padding: 0.6rem 1.2rem; border-radius: 6px; background: #2e6bff; color: white; text-decoration: none; font-weight: 600; width: fit-content; }
          .note { color: #555; font-size: 0.9rem; }
          form { display: grid; gap: 0.5rem; max-width: 420px; }
          input[type=text] { padding: 0.5rem; border: 1px solid #ccc; border-radius: 6px; }
          button { padding: 0.5rem 1rem; border: 0; border-radius: 6px; background: #2e6bff; color: white; cursor: pointer; }
          label { font-weight: 600; }
          .meta { color: #666; font-size: 0.9rem; }
        </style>
      </head>
      <body>
        <div class="card">
          <h2>{{ title }}</h2>
          <p class="meta">Announcement ID: {{ announcement_id }}</p>
          <p>{{ details }}</p>
          {% if target_url %}
          <div class="actions">
            <a class="btn" href="{{ target_url }}" target="_blank" rel="noopener">Start Task</a>
            <p class="note">The task opens in a new tab. Once you have completed it, return here to acknowledge.</p>
          </div>
          {% else %}
          <p class="meta">This announcement does not have a target URL configured.</p>
          {% endif %}
        </div>

        <div class="card">
          <h3>Acknowledge Completion</h3>
          <p class="meta">Record your acknowledgement after finishing the task.</p>
          <form method="post" action="/acknowledge">
            <label for="user">Username</label>
            <input id="user" name="user" type="text" placeholder="employee@example.com" value="{{ default_user }}" required />
            <input type="hidden" name="announcementId" value="{{ announcement_id }}" />
            <input type="hidden" name="target" value="{{ target_url }}" />
            <button type="submit">Acknowledge</button>
          </form>
        </div>
      </body>
    </html>
    """

    return render_template_string(
        html,
        announcement_id=announcement_id,
        title=(ann["title"] if ann else "Announcement"),
        details=(ann["details"] if ann else "Please proceed to complete the task."),
        target_url=target or (ann["target"] if ann else ""),
        default_user=prefill_user if prefill_user != "anonymous" else "",
    )


@app.get("/proceed")
def proceed():
    # Legacy route kept for compatibility; redirect to /track with the same parameters.
    target_url = url_for("track_open")
    if request.query_string:
        target_url = f"{target_url}?{request.query_string.decode('utf-8')}"
    return redirect(target_url)


@app.post("/acknowledge")
def acknowledge():
    form = request.form
    try:
        announcement_id = int(form.get("announcementId")) if form.get("announcementId") else None
    except ValueError:
        return ("Invalid announcementId", 400)
    user = form.get("user") or "anonymous"
    target = form.get("target") or ""

    device = request.headers.get("User-Agent", "")
    event = {
        "event": "acknowledged",
        "announcementId": announcement_id,
        "user": user,
        "target": target,
        "timestamp": iso_utc_now(),
        "device": device,
        "ip": client_ip(),
    }
    append_event(event)
    ann = None
    if announcement_id is not None:
        for a in announcements:
            if a["id"] == announcement_id:
                ann = a
                break
    send_sms_alert(event, ann)

    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Thanks</title>
        <style>
          body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; }
          .card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; max-width: 700px; }
        </style>
      </head>
      <body>
        <div class="card">
          <h2>Thank you</h2>
          <p>Your acknowledgment is recorded.</p>
          <p><a href="/dashboard">Go to Dashboard</a></p>
        </div>
      </body>
    </html>
    """

    return render_template_string(html)


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    opened: List[Dict[str, Any]] = []
    acknowledged: List[Dict[str, Any]] = []
    sms_status = ""
    file_sms_numbers = load_sms_recipients_from_file()
    sms_text_value = "\n".join(file_sms_numbers)
    sms_env_numbers = get_env_sms_recipients()
    events: List[Dict[str, Any]] = []

    if request.method == "POST":
        form_name = request.form.get("form")
        if form_name == "sms":
            numbers = save_sms_recipients(request.form.get("sms_numbers", ""))
            file_sms_numbers = numbers
            sms_text_value = "\n".join(file_sms_numbers)
            if numbers:
                sms_status = f"Saved {len(numbers)} phone number(s)."
            else:
                sms_status = "SMS alerts disabled (no numbers saved)."
        else:
            sms_status = "Unrecognised form submission."

    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append(rec)
                if rec.get("event") == "opened":
                    opened.append(rec)
                elif rec.get("event") == "acknowledged":
                    acknowledged.append(rec)

    ai_insights = generate_user_analytics(events)

    # Simple table renderer
    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Dashboard</title>
        <style>
          body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; }
          table { border-collapse: collapse; width: 100%; margin-bottom: 2rem; }
          th, td { border: 1px solid #ddd; padding: 8px; }
          th { background: #f6f6f6; text-align: left; }
          h2 { margin-top: 2rem; }
          code { font-size: 0.9em; }
          .card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1.5rem; }
          input[type=text], input[type=url], textarea { width: 100%; padding: 0.5rem; border: 1px solid #ccc; border-radius: 6px; }
          label { font-weight: 600; margin-top: 0.5rem; display: block; }
          button { padding: 0.5rem 1rem; border: 0; border-radius: 6px; background: #2e6bff; color: white; cursor: pointer; }
          .row { display: grid; gap: 0.5rem; max-width: 840px; }
          .muted { color: #666; }
          .flex { display: flex; align-items: center; gap: 0.5rem; }
        </style>
      </head>
      <body>
        <h1>Events Dashboard</h1>

        <div class="card">
          <h2>SMS Alerts</h2>
          <form method="post" class="row">
            <input type="hidden" name="form" value="sms" />
            <label for="sms_numbers">Phone numbers (one per line or comma separated)</label>
            <textarea id="sms_numbers" name="sms_numbers" rows="3" placeholder="+15551234567">{{ sms_text_value }}</textarea>
            <button type="submit">Save Numbers</button>
            <span class="muted">{{ sms_status }}</span>
          </form>
          <p class="muted">Configure Twilio via environment variables: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER.</p>
          {% if sms_env_numbers %}
          <p class="muted">Additional numbers from SMS_RECIPIENTS env: {{ sms_env_numbers|join(', ') }}</p>
          {% endif %}
        </div>

        <div class="card">
          <h2>AI Insights</h2>
          {% if ai_insights.overall.total_events %}
          <p class="muted">Engine: {{ ai_insights.engine }} · Users: {{ ai_insights.overall.total_users }} · Conversion: {{ ai_insights.overall.conversion_rate }}%
            {% if ai_insights.overall.avg_ack_minutes is not none %}· Avg ack delay: {{ ai_insights.overall.avg_ack_minutes }} min{% endif %}
          </p>
          {% if ai_insights.leaders %}
          <h3>Top Contributors</h3>
          <ul>
            {% for item in ai_insights.leaders %}
            <li><strong>{{ item.user }}</strong> — {{ item.classification }} · Score {{ '%.2f'|format(item.score) }} · Ack rate {{ item.ack_rate }}%
              {% if item.avg_delay_minutes is not none %} · Avg delay {{ item.avg_delay_minutes }} min{% endif %}
            </li>
            {% endfor %}
          </ul>
          {% endif %}
          {% if ai_insights.risks %}
          <h3>At-Risk Users</h3>
          <ul>
            {% for item in ai_insights.risks %}
            <li><strong>{{ item.user }}</strong> — {{ item.classification }} · Ack rate {{ item.ack_rate }}% · Outstanding {{ item.outstanding }} · Risk score {{ '%.2f'|format(item.risk_score) }}
              {% if item.avg_delay_minutes is not none %} · Avg delay {{ item.avg_delay_minutes }} min{% endif %}
            </li>
            {% endfor %}
          </ul>
          {% endif %}
          {% if ai_insights.insights %}
          <h3>Highlights</h3>
          <ul>
            {% for message in ai_insights.insights %}
            <li>{{ message }}</li>
            {% endfor %}
          </ul>
          {% endif %}
          {% else %}
          <p class="muted">Not enough engagement data yet. Insights appear after users interact with announcements.</p>
          {% endif %}
        </div>

        <div class="card">
          <h2>Create Announcement</h2>
          <form id="ann-form" class="row">
            <div>
              <label for="title">Title</label>
              <input id="title" name="title" type="text" placeholder="Security Policy Update" required />
            </div>
            <div>
              <label for="details">Details</label>
              <textarea id="details" name="details" rows="3" placeholder="Please review and acknowledge." required></textarea>
            </div>
            <div>
              <label for="target">Target URL</label>
              <input id="target" name="target" type="url" placeholder="https://example.com/security-policy" required />
            </div>
            <div>
              <label for="user">Optional default user (query param)</label>
              <input id="user" name="user" type="text" placeholder="employee@example.com" />
            </div>
            <div class="flex">
              <button type="submit">Create</button>
              <span id="status" class="muted"></span>
            </div>
          </form>
          <div id="result" style="margin-top:0.75rem"></div>
        </div>

        <h2>Opened</h2>
        <table>
          <thead>
            <tr>
              <th>User</th>
              <th>AnnouncementId</th>
              <th>Time</th>
              <th>Device</th>
              <th>IP</th>
              <th>Target</th>
            </tr>
          </thead>
          <tbody>
            {% for e in opened %}
            <tr>
              <td>{{ e.get('user','') }}</td>
              <td>{{ e.get('announcementId','') }}</td>
              <td><code>{{ e.get('timestamp','') }}</code></td>
              <td><code title="{{ e.get('device','') }}">{{ e.get('device','')[:60] }}</code></td>
              <td>{{ e.get('ip','') }}</td>
              <td>{% if e.get('target') %}<a href="{{ e.get('target') }}" target="_blank" rel="noopener">link</a>{% endif %}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>

        <h2>Acknowledged</h2>
        <table>
          <thead>
            <tr>
              <th>User</th>
              <th>AnnouncementId</th>
              <th>Time</th>
              <th>Device</th>
              <th>IP</th>
              <th>Target</th>
            </tr>
          </thead>
          <tbody>
            {% for e in acknowledged %}
            <tr>
              <td>{{ e.get('user','') }}</td>
              <td>{{ e.get('announcementId','') }}</td>
              <td><code>{{ e.get('timestamp','') }}</code></td>
              <td><code title="{{ e.get('device','') }}">{{ e.get('device','')[:60] }}</code></td>
              <td>{{ e.get('ip','') }}</td>
              <td>{% if e.get('target') %}<a href="{{ e.get('target') }}" target="_blank" rel="noopener">link</a>{% endif %}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>

        <script>
          const form = document.getElementById('ann-form');
          const statusEl = document.getElementById('status');
          const resultEl = document.getElementById('result');

          form.addEventListener('submit', async (e) => {
            e.preventDefault();
            statusEl.textContent = 'Creating...';
            resultEl.innerHTML = '';
            const title = document.getElementById('title').value.trim();
            const details = document.getElementById('details').value.trim();
            const target = document.getElementById('target').value.trim();
            const user = document.getElementById('user').value.trim();
            try {
              const r = await fetch('/announcement', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ title, details, target })
              });
              const data = await r.json();
              if (!r.ok) throw new Error(data.error || 'Failed');
              let link = data.track;
              if (user) {
                const delim = link.includes('?') ? '&' : '?';
                link = `${link}${delim}user=${encodeURIComponent(user)}`;
              }
              statusEl.textContent = 'Created.';
              resultEl.innerHTML = `
                <div class="flex">
                  <strong>Share link:</strong>
                  <a href="${link}" target="_blank" rel="noopener">${link}</a>
                  <button type="button" id="copy">Copy</button>
                </div>`;
              const copyBtn = document.getElementById('copy');
              copyBtn?.addEventListener('click', async () => {
                try { await navigator.clipboard.writeText(link); copyBtn.textContent = 'Copied'; setTimeout(()=>copyBtn.textContent='Copy', 1500);} catch {}
              });
            } catch (err) {
              statusEl.textContent = err.message || 'Error';
            }
          });
        </script>
      </body>
    </html>
    """

    return render_template_string(
        html,
        opened=opened,
        acknowledged=acknowledged,
        sms_status=sms_status,
        sms_text_value=sms_text_value,
        sms_env_numbers=sms_env_numbers,
        ai_insights=ai_insights,
    )


@app.get("/")
def home():
    base = request.host_url.rstrip("/")
    example = {
        "title": "Security Policy Update",
        "details": "Please review and proceed to acknowledge.",
        "target": "https://example.com/security-policy",
    }
    return render_template_string(
        """
        <html><head><meta charset="utf-8" /><title>Ack Tracker</title>
        <style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem}code{background:#f6f6f6;padding:2px 4px;border-radius:4px}</style>
        </head><body>
        <h2>Acknowledgment Tracker</h2>
        <p>Create an announcement via:</p>
        <pre>curl -X POST '{{base}}/announcement' \
  -H 'Content-Type: application/json' \
  -d '{"title":"{title}","details":"{details}","target":"{target}"}'</pre>
        <p>Open the dashboard: <a href="/dashboard">/dashboard</a></p>
        </body></html>
        """.replace("{base}", base).replace("{title}", example["title"]).replace("{details}", example["details"]).replace("{target}", example["target"])  # noqa: E501
    )


if __name__ == "__main__":
    # Built-in dev server at http://localhost:5000
    app.run(host="127.0.0.1", port=5000, debug=True)
