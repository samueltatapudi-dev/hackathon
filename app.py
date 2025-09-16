from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List

from flask import Flask, request, jsonify, render_template_string, redirect, url_for


app = Flask(__name__)


# In-memory announcements store
announcements: List[Dict[str, Any]] = []
next_announcement_id: int = 1


EVENTS_FILE = "events.json"


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

    # Render an interstitial page with details and Proceed button
    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Announcement</title>
        <style>
          body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; }
          .card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; max-width: 800px; }
          .actions { margin-top: 1rem; }
          button { padding: 0.5rem 1rem; border: 0; border-radius: 6px; background: #2e6bff; color: white; cursor: pointer; }
          a.btn { text-decoration: none; color: white; }
          .meta { color: #666; font-size: 0.9rem; }
        </style>
      </head>
      <body>
        <div class="card">
          <h2>{{ title }}</h2>
          <p class="meta">Announcement ID: {{ announcement_id }}</p>
          <p>{{ details }}</p>
          <div class="actions">
            <form method="get" action="/proceed">
              <input type="hidden" name="id" value="{{ announcement_id }}" />
              <input type="hidden" name="target" value="{{ target }}" />
              <button type="submit">Proceed to Task</button>
            </form>
          </div>
        </div>
      </body>
    </html>
    """

    return render_template_string(
        html,
        announcement_id=announcement_id,
        title=(ann["title"] if ann else "Announcement"),
        details=(ann["details"] if ann else "Please proceed to complete the task."),
        target=target or (ann["target"] if ann else ""),
    )


@app.get("/proceed")
def proceed():
    ann_id = request.args.get("id", type=int)
    target = request.args.get("target", default="")
    ann = None
    if ann_id is not None:
        for a in announcements:
            if a["id"] == ann_id:
                ann = a
                break

    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Acknowledge</title>
        <style>
          body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; }
          form { display: grid; gap: 0.5rem; max-width: 500px; }
          input[type=text] { padding: 0.5rem; border: 1px solid #ccc; border-radius: 6px; }
          button { padding: 0.5rem 1rem; border: 0; border-radius: 6px; background: #2e6bff; color: white; cursor: pointer; }
          .meta { color: #666; font-size: 0.9rem; }
        </style>
      </head>
      <body>
        <h2>Acknowledgment</h2>
        <p class="meta">Announcement ID: {{ announcement_id }}</p>
        <form method="post" action="/acknowledge">
          <label for="user">Username</label>
          <input id="user" name="user" type="text" placeholder="employee@example.com" required />
          <input type="hidden" name="announcementId" value="{{ announcement_id }}" />
          <input type="hidden" name="target" value="{{ target }}" />
          <button type="submit">Acknowledge</button>
        </form>
      </body>
    </html>
    """

    return render_template_string(
        html, announcement_id=ann_id, target=target or (ann["target"] if ann else "")
    )


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


@app.get("/dashboard")
def dashboard():
    opened: List[Dict[str, Any]] = []
    acknowledged: List[Dict[str, Any]] = []

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
                if rec.get("event") == "opened":
                    opened.append(rec)
                elif rec.get("event") == "acknowledged":
                    acknowledged.append(rec)

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

    return render_template_string(html, opened=opened, acknowledged=acknowledged)


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
