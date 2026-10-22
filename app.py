import copy
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from flask import Flask, abort, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "true" if os.environ.get("RENDER") else "false").lower() == "true"

# Preserve the established login behavior; override this value in Render for a public deployment.
ADMIN_PASSCODE = os.environ.get("ADMIN_PASSCODE", "456456456").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_AI_API_KEY") or "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
BANK_SERVER_URL = os.environ.get("BANK_SERVER_URL", "https://sanal-banka.onrender.com").rstrip("/")
SEARCH_TIMEOUT = 10
MAX_QUERY_LENGTH = 160
MAX_CHAT_LENGTH = 2000
MAX_EVENTS = 20000
MAX_MESSAGES = 10000
MAX_BACKUP_BYTES = 5 * 1024 * 1024
ONLINE_SECONDS = 75

LOCK = threading.RLock()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def unix_now():
    return time.time()


def new_store():
    return {
        "users": {},                 # keyed by device_id; in RAM only
        "events": [],                # transparent application activity log
        "messages": [],              # ChatApp DMs in RAM
        "gemini_messages": [],       # Gemini history in RAM
        "settings": {"trial_seconds": 600, "trial_price": 0, "currency": "TRY"},
        "admin_labels": {},          # admin-only device names, independent of accounts
        "blocked_devices": [],
        "blocked_sites": [],
        "announcements": [],
        "next_id": 1,
        "started_at": utc_now(),
    }


STORE = new_store()
LOGIN_FAILURES = {}  # transient, per-device, in-memory soft throttle


def record_event(event_type, details=None, device_id=None):
    details = details if isinstance(details, dict) else {}
    # Never log passwords, API keys, session cookies, PINs, or raw auth tokens.
    safe = {}
    for key, value in details.items():
        k = str(key)[:60]
        if k.lower() in {"password", "passcode", "token", "cookie", "api_key", "secret", "pin", "authorization"}:
            continue
        if isinstance(value, (dict, list)):
            try:
                value = json.loads(json.dumps(value, ensure_ascii=False, default=str))
            except Exception:
                value = str(value)
        elif not isinstance(value, (str, int, float, bool)) and value is not None:
            value = str(value)
        safe[k] = value
    event = {
        "id": STORE["next_id"],
        "device_id": device_id or session.get("device_id"),
        "event_type": str(event_type)[:60],
        "details": safe,
        "created_at": utc_now(),
    }
    STORE["next_id"] += 1
    STORE["events"].append(event)
    if len(STORE["events"]) > MAX_EVENTS:
        del STORE["events"][:len(STORE["events"]) - MAX_EVENTS]


def current_user():
    device_id = session.get("device_id")
    if not device_id:
        return None
    user = STORE["users"].get(device_id)
    if not user or session.get("user_id") != user.get("id"):
        return None
    return user


def trial_left(user):
    if not user or user.get("is_admin"):
        return None
    started = user.get("trial_started_ts")
    if started is None:
        return int(STORE["settings"].get("trial_seconds", 600))
    return max(0, int(STORE["settings"].get("trial_seconds", 600) - (unix_now() - started)))


def user_payload(user):
    return {
        "device_id": user["device_id"],
        "username": user.get("username", "Kullanıcı"),
        "tag": user.get("tag", "#0000"),
        "is_admin": bool(user.get("is_admin")),
        "plan": "VIP" if user.get("is_admin") else "TRIAL",
        "trial_left_seconds": trial_left(user),
        "trial_expired": (not user.get("is_admin") and trial_left(user) == 0),
    }


def require_user(check_trial=False):
    user = current_user()
    if not user:
        return None, (jsonify({"ok": False, "error": "Önce giriş yapmalısın."}), 401)
    if user.get("blocked") or user["device_id"] in STORE["blocked_devices"]:
        session.clear()
        return None, (jsonify({"ok": False, "error": "Bu cihaz yönetici tarafından engellendi."}), 403)
    if check_trial and not user.get("is_admin") and trial_left(user) == 0:
        return None, (jsonify({"ok": False, "error": "Deneme süresi sona erdi. Yönetici ayarlarından süre güncellenebilir."}), 402)
    return user, None


def require_admin():
    user, error = require_user(check_trial=False)
    if error:
        return None, error
    if not user.get("is_admin"):
        return None, (jsonify({"ok": False, "error": "Bu sayfa yalnızca yöneticilere açık."}), 403)
    return user, None


def domain_is_blocked(host):
    host = (host or "").lower().rstrip(".")
    for raw in STORE["blocked_sites"]:
        domain = str(raw).lower().replace("https://", "").replace("http://", "").split("/")[0].split(":")[0].strip().lstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            return True
    return False


def online_state(user):
    last = float(user.get("last_seen_ts", 0) or 0)
    return last > 0 and unix_now() - last <= ONLINE_SECONDS


def safe_result_url(raw):
    try:
        parsed = urlparse(str(raw or ""))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        return parsed
    except Exception:
        return None


def collect_backup():
    return {
        "format": "webos-ram-backup",
        "version": 2,
        "exported_at": utc_now(),
        "users": [copy.deepcopy(u) for u in STORE["users"].values()],
        "events": copy.deepcopy(STORE["events"]),
        "messages": copy.deepcopy(STORE["messages"]),
        "gemini_messages": copy.deepcopy(STORE["gemini_messages"]),
        "settings": copy.deepcopy(STORE["settings"]),
        "admin_labels": copy.deepcopy(STORE["admin_labels"]),
        "blocked_devices": copy.deepcopy(STORE["blocked_devices"]),
        "blocked_sites": copy.deepcopy(STORE["blocked_sites"]),
        "announcements": copy.deepcopy(STORE["announcements"]),
        "started_at": STORE["started_at"],
    }


def listify(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        # Both mapping values and {device_id: record} legacy maps are supported.
        result = []
        for key, item in value.items():
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("device_id", key)
                result.append(item)
        return result
    return []


def normalize_import(payload):
    """Import adapter for current v2, previous v1, and common legacy JSON shapes."""
    if not isinstance(payload, dict):
        raise ValueError("JSON kök verisi bir nesne olmalı.")
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if isinstance(payload.get("RAM_DB"), dict):
        merged = dict(payload["RAM_DB"])
        for k, v in payload.items():
            if k != "RAM_DB" and k not in merged:
                merged[k] = v
        payload = merged

    users_raw = payload.get("users", payload.get("devices", payload.get("accounts", [])))
    events_raw = payload.get("events", payload.get("logs", payload.get("cookie", [])))
    messages_raw = payload.get("messages", payload.get("chat_messages", []))
    gemini_raw = payload.get("gemini_messages", payload.get("gemini_history", []))
    if isinstance(payload.get("format"), str) and payload["format"] not in {"webos-backup", "webos-ram-backup", "webos-json", "webos"}:
        raise ValueError("Tanınmayan JSON yedek biçimi.")

    users = []
    for i, raw in enumerate(listify(users_raw)):
        if not isinstance(raw, dict):
            continue
        device_id = str(raw.get("device_id") or raw.get("device_hash") or raw.get("deviceId") or raw.get("id_device") or "").strip()
        if not device_id and isinstance(raw.get("id"), str) and len(raw["id"]) > 8:
            device_id = raw["id"].strip()
        if not device_id:
            continue
        username = str(raw.get("username") or raw.get("name") or "Kullanıcı")[:40]
        password_hash = raw.get("password_hash") if isinstance(raw.get("password_hash"), str) else None
        legacy_password = raw.get("password")
        if not password_hash and isinstance(legacy_password, str) and legacy_password:
            password_hash = generate_password_hash(legacy_password[:128])
        users.append({
            "id": raw.get("id") if isinstance(raw.get("id"), int) else None,
            "device_id": device_id[:128],
            "username": username,
            "tag": str(raw.get("tag") or ("#" + str(secrets.randbelow(9000) + 1000)))[:12],
            "password_hash": password_hash,
            "is_admin": bool(raw.get("is_admin", raw.get("admin", False))),
            "created_at": str(raw.get("created_at") or raw.get("createdAt") or utc_now())[:40],
            "trial_started_ts": raw.get("trial_started_ts") if isinstance(raw.get("trial_started_ts"), (int, float)) else None,
            "blocked": bool(raw.get("blocked", False)),
            "last_seen_ts": float(raw.get("last_seen_ts", 0) or 0),
            "last_seen_at": str(raw.get("last_seen_at") or "")[:40],
        })

    events = []
    for raw in listify(events_raw):
        if not isinstance(raw, dict):
            continue
        details = raw.get("details", raw.get("details_json", raw.get("data", {})))
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except Exception:
                details = {"legacy_details": details[:500]}
        if not isinstance(details, dict):
            details = {"value": str(details)[:500]}
        events.append({
            "id": raw.get("id") if isinstance(raw.get("id"), int) else None,
            "device_id": str(raw.get("device_id") or raw.get("device_hash") or "")[:128] or None,
            "event_type": str(raw.get("event_type") or raw.get("type") or raw.get("action") or "imported_event")[:60],
            "details": details,
            "created_at": str(raw.get("created_at") or raw.get("timestamp") or raw.get("time") or utc_now())[:40],
        })
    messages = []
    for raw in listify(messages_raw):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or raw.get("message") or raw.get("content") or "")[:MAX_CHAT_LENGTH]
        sender = str(raw.get("sender_device_id") or raw.get("from_device_id") or raw.get("sender") or "")[:128]
        recipient = str(raw.get("recipient_device_id") or raw.get("to_device_id") or raw.get("recipient") or "")[:128]
        if text:
            messages.append({"id": raw.get("id") or secrets.token_hex(5), "sender_device_id": sender, "recipient_device_id": recipient, "text": text, "created_at": str(raw.get("created_at") or raw.get("timestamp") or utc_now())[:40]})
    gemini_messages = []
    for raw in listify(gemini_raw):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or raw.get("content") or "")[:5000]
        device_id = str(raw.get("device_id") or "")[:128]
        role = str(raw.get("role") or "user")
        if text and device_id and role in {"user", "model", "assistant"}:
            gemini_messages.append({"id": raw.get("id") or secrets.token_hex(5), "device_id": device_id, "role": "model" if role == "assistant" else role, "text": text, "created_at": str(raw.get("created_at") or utc_now())[:40]})
    settings_raw = payload.get("settings", {})
    settings = {}
    if isinstance(settings_raw, dict):
        try:
            settings["trial_seconds"] = max(60, min(31_536_000, int(settings_raw.get("trial_seconds", settings_raw.get("trial_duration", 600)))))
        except Exception:
            settings["trial_seconds"] = 600
        try:
            settings["trial_price"] = max(0, float(settings_raw.get("trial_price", settings_raw.get("price", 0))))
        except Exception:
            settings["trial_price"] = 0
        settings["currency"] = str(settings_raw.get("currency", "TRY"))[:8]
    else:
        settings = {"trial_seconds": 600, "trial_price": 0, "currency": "TRY"}
    labels = payload.get("admin_labels", payload.get("device_names", {}))
    if not isinstance(labels, dict):
        labels = {}
    blocked_devices = payload.get("blocked_devices", [])
    if not isinstance(blocked_devices, list):
        blocked_devices = []
    blocked_sites = payload.get("blocked_sites", payload.get("site_blocklist", []))
    if not isinstance(blocked_sites, list):
        blocked_sites = []
    announcements = payload.get("announcements", payload.get("broadcasts", []))
    if not isinstance(announcements, list):
        announcements = []
    return {
        "users": users,
        "events": events[-MAX_EVENTS:],
        "messages": messages[-MAX_MESSAGES:],
        "gemini_messages": gemini_messages[-MAX_MESSAGES:],
        "settings": settings,
        "admin_labels": {str(k)[:128]: str(v)[:40] for k, v in labels.items()},
        "blocked_devices": [str(v)[:128] for v in blocked_devices if v],
        "blocked_sites": [str(v)[:253].lower() for v in blocked_sites if v],
        "announcements": announcements[-200:],
    }


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/admin")
def admin_page():
    _, error = require_admin()
    if error:
        return render_template("admin.html", denied=True), 403
    return render_template("admin.html", denied=False)


@app.get("/health")
def health():
    return jsonify({"ok": True, "storage": "RAM-only", "users": len(STORE["users"]), "started_at": STORE["started_at"]})


@app.post("/api/auth/init")
def auth_init():
    payload = request.get_json(silent=True) or {}
    device_id = str(payload.get("device_hash", payload.get("device_id", ""))).strip()
    password = str(payload.get("password", ""))
    if not device_id or len(device_id) > 128:
        return jsonify({"ok": False, "error": "Cihaz kimliği geçersiz."}), 400
    if len(password) > 128:
        return jsonify({"ok": False, "error": "Şifre çok uzun."}), 400

    # Small per-device cooldown; it is temporary and also lives only in RAM.
    failures, until = LOGIN_FAILURES.get(device_id, (0, 0))
    if until > unix_now():
        return jsonify({"ok": False, "error": "Çok fazla deneme yapıldı. Biraz bekle."}), 429

    with LOCK:
        user = STORE["users"].get(device_id)
        is_admin_code = bool(ADMIN_PASSCODE) and secrets.compare_digest(password, ADMIN_PASSCODE)
        if is_admin_code:
            if user is None:
                user = {
                    "id": STORE["next_id"], "device_id": device_id, "username": "Yönetici", "tag": "#ADMIN",
                    "password_hash": None, "is_admin": True, "created_at": utc_now(), "trial_started_ts": None,
                    "blocked": False, "last_seen_ts": unix_now(), "last_seen_at": utc_now(),
                }
                STORE["next_id"] += 1
                STORE["users"][device_id] = user
            else:
                user["is_admin"] = True
                user["username"] = "Yönetici"
                user["blocked"] = False
            session.clear()
            session["user_id"] = user["id"]
            session["device_id"] = device_id
            user["last_seen_ts"] = unix_now()
            user["last_seen_at"] = utc_now()
            LOGIN_FAILURES.pop(device_id, None)
            record_event("admin_login", {"result": "success"}, device_id)
            record_event("device_connected", {"is_admin": True}, device_id)
            return jsonify({"ok": True, "data": user_payload(user)})

        if device_id in STORE["blocked_devices"] or (user and user.get("blocked")):
            record_event("blocked_device_login_attempt", {"result": "denied"}, device_id)
            return jsonify({"ok": False, "error": "Bu cihaz yönetici tarafından engellendi."}), 403
        if user is None:
            if not password:
                return jsonify({"ok": False, "need_password_setup": True})
            if len(password) < 4:
                return jsonify({"ok": False, "error": "Şifre en az 4 karakter olmalı."}), 400
            user = {
                "id": STORE["next_id"], "device_id": device_id, "username": "Kullanıcı", "tag": "#" + str(secrets.randbelow(9000) + 1000),
                "password_hash": generate_password_hash(password), "is_admin": False, "created_at": utc_now(),
                "trial_started_ts": unix_now(), "blocked": False, "last_seen_ts": unix_now(), "last_seen_at": utc_now(),
            }
            STORE["next_id"] += 1
            STORE["users"][device_id] = user
            record_event("account_created", {"tag": user["tag"]}, device_id)
        else:
            if not password or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
                failures += 1
                LOGIN_FAILURES[device_id] = (failures, unix_now() + (30 if failures >= 5 else 0))
                record_event("login_failed", {"attempt_count": failures}, device_id)
                if failures >= 5:
                    LOGIN_FAILURES[device_id] = (0, unix_now() + 30)
                return jsonify({"ok": False, "error": "Şifre yanlış."}), 401
        session.clear()
        session["user_id"] = user["id"]
        session["device_id"] = device_id
        user["last_seen_ts"] = unix_now()
        user["last_seen_at"] = utc_now()
        LOGIN_FAILURES.pop(device_id, None)
        record_event("login", {"result": "success", "is_admin": bool(user.get("is_admin"))}, device_id)
        return jsonify({"ok": True, "data": user_payload(user)})


@app.post("/api/auth/logout")
def auth_logout():
    user = current_user()
    if user:
        record_event("logout", {"result": "success"}, user["device_id"])
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def auth_me():
    user, error = require_user(check_trial=False)
    if error:
        return error
    user["last_seen_ts"] = unix_now()
    user["last_seen_at"] = utc_now()
    return jsonify({"ok": True, "data": user_payload(user)})


@app.post("/api/presence")
def presence():
    user, error = require_user(check_trial=False)
    if error:
        return error
    user["last_seen_ts"] = unix_now()
    user["last_seen_at"] = utc_now()
    return jsonify({"ok": True})


@app.post("/api/profile")
def profile_update():
    user, error = require_user(check_trial=False)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()[:24]
    if username:
        user["username"] = username
        record_event("profile_updated", {"username": username})
    return jsonify({"ok": True, "data": user_payload(user)})


@app.get("/api/announcements")
def announcements():
    return jsonify({"ok": True, "announcements": STORE["announcements"][-20:]})


@app.get("/api/search")
def search():
    user, error = require_user(check_trial=True)
    if error:
        return error
    query = str(request.args.get("q", "")).strip()[:MAX_QUERY_LENGTH]
    if not query:
        return jsonify({"ok": True, "query": "", "results": []})
    try:
        response = requests.get(
            "https://html.duckduckgo.com/html/", params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; WebOS-ProxWeb/2.0; +https://duckduckgo.com/)"},
            timeout=SEARCH_TIMEOUT,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        for item in soup.select(".result"):
            link = item.select_one(".result__a")
            snippet = item.select_one(".result__snippet")
            if not link or not link.get("href"):
                continue
            href = link.get("href")
            parsed = safe_result_url(href)
            if parsed and parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
                wrapped = parse_qs(parsed.query).get("uddg", [])
                if wrapped:
                    href = wrapped[0]
                    parsed = safe_result_url(href)
            if not parsed or domain_is_blocked(parsed.hostname):
                continue
            results.append({
                "title": link.get_text(" ", strip=True)[:240],
                "url": href,
                "host": parsed.hostname or "",
                "snippet": snippet.get_text(" ", strip=True)[:600] if snippet else "",
            })
            if len(results) >= 10:
                break
        record_event("proxweb_search", {"query": query, "result_count": len(results)})
        return jsonify({"ok": True, "query": query, "results": results})
    except requests.RequestException as exc:
        record_event("proxweb_search_error", {"query": query, "error": type(exc).__name__})
        return jsonify({"ok": False, "error": "Arama sağlayıcısına şu anda ulaşılamıyor. Biraz sonra tekrar dene."}), 502


@app.get("/go")
def go():
    user, error = require_user(check_trial=True)
    if error:
        return redirect("/?notice=login")
    target = str(request.args.get("url", "")).strip()
    parsed = safe_result_url(target)
    if not parsed:
        abort(400)
    if domain_is_blocked(parsed.hostname):
        record_event("proxweb_blocked_site_open_attempt", {"host": parsed.hostname})
        abort(403, description="Bu site yönetici tarafından engellenmiş.")
    record_event("proxweb_open_link", {"host": parsed.hostname or "", "url": target[:1500]})
    return redirect(target, code=302)


@app.post("/api/gemini/chat")
def gemini_chat():
    user, error = require_user(check_trial=True)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("message", "")).strip()[:5000]
    if not prompt:
        return jsonify({"ok": False, "error": "Bir mesaj yaz."}), 400
    if not GEMINI_API_KEY:
        record_event("gemini_unavailable", {"reason": "missing_server_api_key"})
        return jsonify({"ok": False, "error": "Gemini API anahtarı sunucuda bulunamadı. Render Environment bölümünde GEMINI_API_KEY değişkenini kontrol et."}), 503
    device_id = user["device_id"]
    with LOCK:
        history = [m for m in STORE["gemini_messages"] if m.get("device_id") == device_id][-12:]
    contents = []
    for item in history:
        if item.get("role") in ("user", "model"):
            contents.append({"role": item["role"], "parts": [{"text": item.get("text", "")[:5000]}]})
    contents.append({"role": "user", "parts": [{"text": prompt}]})
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={"contents": contents, "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2048}},
            timeout=35,
        )
        if not resp.ok:
            record_event("gemini_api_error", {"status_code": resp.status_code})
            if resp.status_code in (400, 401, 403, 404):
                return jsonify({"ok": False, "error": "Gemini isteği reddedildi. API anahtarını ve GEMINI_MODEL ayarını kontrol et."}), 502
            return jsonify({"ok": False, "error": "Gemini şu anda yanıt veremiyor. Biraz sonra tekrar dene."}), 502
        data = resp.json()
        answer = "".join(part.get("text", "") for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []) if isinstance(part, dict)).strip()
        if not answer:
            feedback = data.get("promptFeedback", {}).get("blockReason")
            return jsonify({"ok": False, "error": "Gemini boş yanıt verdi." + (f" ({feedback})" if feedback else "")}), 502
        now = utc_now()
        with LOCK:
            STORE["gemini_messages"].append({"id": secrets.token_hex(6), "device_id": device_id, "role": "user", "text": prompt, "created_at": now})
            STORE["gemini_messages"].append({"id": secrets.token_hex(6), "device_id": device_id, "role": "model", "text": answer[:10000], "created_at": utc_now()})
            STORE["gemini_messages"] = STORE["gemini_messages"][-MAX_MESSAGES:]
            record_event("gemini_message", {"prompt": prompt[:1500], "response_chars": len(answer)}, device_id)
        return jsonify({"ok": True, "answer": answer})
    except requests.RequestException as exc:
        record_event("gemini_network_error", {"error": type(exc).__name__}, device_id)
        return jsonify({"ok": False, "error": "Gemini bağlantısı kurulamadı."}), 502


@app.get("/api/gemini/history")
def gemini_history():
    user, error = require_user(check_trial=False)
    if error:
        return error
    messages = [m for m in STORE["gemini_messages"] if m.get("device_id") == user["device_id"]][-100:]
    return jsonify({"ok": True, "messages": messages})


@app.post("/api/gemini/clear")
def gemini_clear():
    user, error = require_user(check_trial=False)
    if error:
        return error
    device_id = user["device_id"]
    STORE["gemini_messages"] = [m for m in STORE["gemini_messages"] if m.get("device_id") != device_id]
    record_event("gemini_history_cleared", {})
    return jsonify({"ok": True})


@app.get("/api/chat/users")
def chat_users():
    user, error = require_user(check_trial=True)
    if error:
        return error
    users = []
    for item in STORE["users"].values():
        if item["device_id"] == user["device_id"] or item.get("blocked") or item["device_id"] in STORE["blocked_devices"]:
            continue
        users.append({"device_id": item["device_id"], "username": item.get("username", "Kullanıcı"), "tag": item.get("tag", "#0000"), "online": online_state(item)})
    users.sort(key=lambda u: (not u["online"], u["username"].lower()))
    return jsonify({"ok": True, "users": users})


@app.get("/api/chat/messages")
def chat_messages():
    user, error = require_user(check_trial=True)
    if error:
        return error
    peer = str(request.args.get("with", ""))[:128]
    device_id = user["device_id"]
    msgs = [m for m in STORE["messages"] if (m.get("sender_device_id") == device_id and m.get("recipient_device_id") == peer) or (m.get("sender_device_id") == peer and m.get("recipient_device_id") == device_id)]
    return jsonify({"ok": True, "messages": msgs[-300:]})


@app.post("/api/chat/messages")
def chat_send():
    user, error = require_user(check_trial=True)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    recipient_id = str(payload.get("to_device_id", ""))[:128]
    text = str(payload.get("text", "")).strip()[:MAX_CHAT_LENGTH]
    recipient = STORE["users"].get(recipient_id)
    if not recipient or recipient.get("blocked") or recipient_id in STORE["blocked_devices"]:
        return jsonify({"ok": False, "error": "Alıcı bulunamadı veya erişimi engelli."}), 404
    if not text:
        return jsonify({"ok": False, "error": "Boş mesaj gönderilemez."}), 400
    msg = {"id": secrets.token_hex(8), "sender_device_id": user["device_id"], "recipient_device_id": recipient_id, "text": text, "created_at": utc_now()}
    with LOCK:
        STORE["messages"].append(msg)
        STORE["messages"] = STORE["messages"][-MAX_MESSAGES:]
        record_event("chat_message_sent", {"to_device_id": recipient_id, "text": text, "message_id": msg["id"]})
    return jsonify({"ok": True, "message": msg})


@app.get("/api/bank/status")
def bank_status():
    user, error = require_user(check_trial=False)
    if error:
        return error
    return jsonify({"ok": True, "server_url": BANK_SERVER_URL, "message": "Banka bağlantısı yapılandırılmıştır; uzak uç noktanın canlı uyumluluğu bu ortamda doğrulanmadı."})


@app.post("/api/bank/link")
def bank_link():
    user, error = require_user(check_trial=False)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    # Never accept or persist full card numbers / PINs in the Web OS event log.
    forwarded = {k: v for k, v in payload.items() if str(k).lower() not in {"pin", "password", "passcode", "secret", "api_key"}}
    forwarded["device_id"] = user["device_id"]
    try:
        resp = requests.post(BANK_SERVER_URL + "/api/link", json=forwarded, timeout=12)
        data = resp.json() if resp.content else {}
        record_event("bank_link_attempt", {"status_code": resp.status_code})
        return jsonify({"ok": resp.ok, "data": data, "error": None if resp.ok else "Banka servisi işlemi reddetti."}), (200 if resp.ok else 502)
    except (requests.RequestException, ValueError) as exc:
        record_event("bank_link_error", {"error": type(exc).__name__})
        return jsonify({"ok": False, "error": "Banka servisine şu anda ulaşılamıyor."}), 502


@app.get("/api/bank/balance")
def bank_balance():
    user, error = require_user(check_trial=False)
    if error:
        return error
    try:
        resp = requests.get(BANK_SERVER_URL + "/api/balance", params={"device_id": user["device_id"]}, timeout=12)
        data = resp.json() if resp.content else {}
        record_event("bank_balance_check", {"status_code": resp.status_code})
        return jsonify({"ok": resp.ok, "data": data, "error": None if resp.ok else "Banka servisi yanıt vermedi."}), (200 if resp.ok else 502)
    except (requests.RequestException, ValueError) as exc:
        record_event("bank_balance_error", {"error": type(exc).__name__})
        return jsonify({"ok": False, "error": "Banka servisine şu anda ulaşılamıyor."}), 502


@app.post("/api/bank/subscribe")
def bank_subscribe():
    user, error = require_user(check_trial=False)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    forwarded = {k: v for k, v in payload.items() if str(k).lower() not in {"pin", "password", "passcode", "secret", "api_key"}}
    forwarded["device_id"] = user["device_id"]
    try:
        resp = requests.post(BANK_SERVER_URL + "/api/subscribe", json=forwarded, timeout=12)
        data = resp.json() if resp.content else {}
        record_event("bank_subscribe_attempt", {"status_code": resp.status_code})
        return jsonify({"ok": resp.ok, "data": data, "error": None if resp.ok else "Banka servisi aboneliği reddetti."}), (200 if resp.ok else 502)
    except (requests.RequestException, ValueError) as exc:
        record_event("bank_subscribe_error", {"error": type(exc).__name__})
        return jsonify({"ok": False, "error": "Banka servisine şu anda ulaşılamıyor."}), 502


@app.get("/api/admin/overview")
def admin_overview():
    admin, error = require_admin()
    if error:
        return error
    devices = []
    for user in STORE["users"].values():
        devices.append({
            "device_id": user["device_id"], "username": user.get("username", "Kullanıcı"), "tag": user.get("tag", "#0000"),
            "is_admin": bool(user.get("is_admin")), "blocked": bool(user.get("blocked") or user["device_id"] in STORE["blocked_devices"]),
            "created_at": user.get("created_at"), "last_seen_at": user.get("last_seen_at"), "online": online_state(user),
            "trial_left_seconds": trial_left(user), "admin_label": STORE["admin_labels"].get(user["device_id"], ""),
        })
    events = sorted(STORE["events"], key=lambda e: e.get("created_at", ""), reverse=True)[:100]
    return jsonify({"ok": True, "devices": devices, "events": events, "settings": STORE["settings"], "blocked_sites": STORE["blocked_sites"], "announcements": STORE["announcements"][-20:], "counts": {"users": len(STORE["users"]), "online": sum(1 for u in STORE["users"].values() if online_state(u)), "events": len(STORE["events"]), "messages": len(STORE["messages"])}})


@app.post("/api/admin/label")
def admin_label():
    _, error = require_admin()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    device_id = str(payload.get("device_id", ""))[:128]
    label = str(payload.get("label", "")).strip()[:40]
    if device_id not in STORE["users"]:
        return jsonify({"ok": False, "error": "Cihaz bulunamadı."}), 404
    if label:
        STORE["admin_labels"][device_id] = label
    else:
        STORE["admin_labels"].pop(device_id, None)
    record_event("admin_device_label_changed", {"target_device_id": device_id, "label": label})
    return jsonify({"ok": True})


@app.post("/api/admin/device-block")
def admin_device_block():
    admin, error = require_admin()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    device_id = str(payload.get("device_id", ""))[:128]
    blocked = bool(payload.get("blocked"))
    if device_id not in STORE["users"]:
        return jsonify({"ok": False, "error": "Cihaz bulunamadı."}), 404
    if device_id == admin["device_id"]:
        return jsonify({"ok": False, "error": "Kendi yönetici cihazını buradan engelleyemezsin."}), 400
    STORE["users"][device_id]["blocked"] = blocked
    if blocked and device_id not in STORE["blocked_devices"]:
        STORE["blocked_devices"].append(device_id)
    if not blocked and device_id in STORE["blocked_devices"]:
        STORE["blocked_devices"].remove(device_id)
    record_event("admin_device_block_changed", {"target_device_id": device_id, "blocked": blocked})
    return jsonify({"ok": True})


@app.post("/api/admin/site-block")
def admin_site_block():
    _, error = require_admin()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    domain = str(payload.get("domain", "")).strip().lower()
    action = str(payload.get("action", "add"))
    domain = domain.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0].strip().lstrip(".")
    if not domain or len(domain) > 253 or "." not in domain or any(c.isspace() for c in domain):
        return jsonify({"ok": False, "error": "Geçerli bir alan adı gir; örnek: example.com"}), 400
    if action == "remove":
        STORE["blocked_sites"] = [s for s in STORE["blocked_sites"] if s != domain]
    elif domain not in STORE["blocked_sites"]:
        STORE["blocked_sites"].append(domain)
    record_event("admin_site_block_changed", {"domain": domain, "action": action})
    return jsonify({"ok": True, "blocked_sites": STORE["blocked_sites"]})


@app.post("/api/admin/settings")
def admin_settings():
    _, error = require_admin()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        trial_seconds = max(60, min(31_536_000, int(payload.get("trial_seconds", STORE["settings"]["trial_seconds"]))))
        trial_price = max(0, min(1_000_000, float(payload.get("trial_price", STORE["settings"]["trial_price"]))))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Süre veya fiyat geçersiz."}), 400
    STORE["settings"]["trial_seconds"] = trial_seconds
    STORE["settings"]["trial_price"] = trial_price
    STORE["settings"]["currency"] = "TRY"
    record_event("admin_settings_updated", {"trial_seconds": trial_seconds, "trial_price": trial_price})
    return jsonify({"ok": True, "settings": STORE["settings"]})


@app.post("/api/admin/announcement")
def admin_announcement():
    _, error = require_admin()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()[:500]
    if not text:
        return jsonify({"ok": False, "error": "Duyuru metni boş olamaz."}), 400
    ann = {"id": secrets.token_hex(6), "text": text, "created_at": utc_now(), "created_by": session.get("device_id")}
    STORE["announcements"].append(ann)
    STORE["announcements"] = STORE["announcements"][-200:]
    record_event("admin_announcement_published", {"announcement_id": ann["id"], "text": text})
    return jsonify({"ok": True, "announcement": ann})


@app.post("/api/admin/announcement/delete")
def admin_announcement_delete():
    _, error = require_admin()
    if error:
        return error
    ann_id = str((request.get_json(silent=True) or {}).get("id", ""))[:40]
    STORE["announcements"] = [a for a in STORE["announcements"] if a.get("id") != ann_id]
    record_event("admin_announcement_deleted", {"announcement_id": ann_id})
    return jsonify({"ok": True})


@app.get("/api/admin/events")
def admin_events():
    _, error = require_admin()
    if error:
        return error
    device_id = str(request.args.get("device_id", ""))[:128]
    try:
        limit = max(1, min(1000, int(request.args.get("limit", "300"))))
    except ValueError:
        limit = 300
    events = STORE["events"]
    if device_id:
        events = [e for e in events if e.get("device_id") == device_id or e.get("details", {}).get("target_device_id") == device_id]
    return jsonify({"ok": True, "events": sorted(events, key=lambda e: e.get("created_at", ""), reverse=True)[:limit]})


@app.get("/api/admin/messages")
def admin_messages():
    _, error = require_admin()
    if error:
        return error
    return jsonify({"ok": True, "messages": STORE["messages"][-500:], "gemini_messages": STORE["gemini_messages"][-500:]})


@app.get("/api/admin/export")
def admin_export():
    _, error = require_admin()
    if error:
        return error
    record_event("admin_json_export", {"users": len(STORE["users"]), "events": len(STORE["events"])})
    data = collect_backup()
    return app.response_class(json.dumps(data, ensure_ascii=False, indent=2), mimetype="application/json", headers={"Content-Disposition": "attachment; filename=webos-ram-backup.json"})


@app.post("/api/admin/import")
def admin_import():
    admin, error = require_admin()
    if error:
        return error
    if request.content_length and request.content_length > MAX_BACKUP_BYTES:
        return jsonify({"ok": False, "error": "JSON dosyası 5 MB sınırını aşıyor."}), 413
    payload = request.get_json(silent=True)
    try:
        incoming = normalize_import(payload)
        mode = str((payload or {}).get("mode", "merge")) if isinstance(payload, dict) else "merge"
        if mode not in {"merge", "replace"}:
            mode = "merge"
        imported_users = 0
        if mode == "replace":
            if not any(u["device_id"] == admin["device_id"] and u["is_admin"] for u in incoming["users"]):
                raise ValueError("Tam geri yükleme, giriş yaptığın yönetici cihazını yönetici olarak içermeli. Eski JSON için 'Birleştir' modunu kullan.")
            fresh = new_store()
            for item in incoming["users"]:
                if not item.get("id"):
                    item["id"] = fresh["next_id"]
                    fresh["next_id"] += 1
                else:
                    fresh["next_id"] = max(fresh["next_id"], item["id"] + 1)
                fresh["users"][item["device_id"]] = item
            for key in ("events", "messages", "gemini_messages", "settings", "admin_labels", "blocked_devices", "blocked_sites", "announcements"):
                fresh[key] = incoming[key]
            fresh["next_id"] = max([u.get("id", 0) for u in fresh["users"].values()] + [0]) + 1
            STORE.clear()
            STORE.update(fresh)
            restored = STORE["users"].get(admin["device_id"])
            session["user_id"] = restored["id"]
            session["device_id"] = restored["device_id"]
            imported_users = len(incoming["users"])
        else:
            for item in incoming["users"]:
                existing = STORE["users"].get(item["device_id"])
                if existing:
                    # Preserve live login hash if the import lacks a usable hash.
                    merged = dict(existing)
                    merged.update({k: v for k, v in item.items() if v is not None})
                    if not item.get("password_hash"):
                        merged["password_hash"] = existing.get("password_hash")
                    STORE["users"][item["device_id"]] = merged
                else:
                    if not item.get("id"):
                        item["id"] = STORE["next_id"]
                        STORE["next_id"] += 1
                    STORE["users"][item["device_id"]] = item
                imported_users += 1
            STORE["events"].extend(incoming["events"])
            STORE["events"] = STORE["events"][-MAX_EVENTS:]
            for key in ("messages", "gemini_messages"):
                existing_ids = {item.get("id") for item in STORE[key]}
                STORE[key].extend(item for item in incoming[key] if item.get("id") not in existing_ids)
                STORE[key] = STORE[key][-MAX_MESSAGES:]
            STORE["settings"].update(incoming["settings"])
            STORE["admin_labels"].update(incoming["admin_labels"])
            STORE["blocked_devices"] = list(dict.fromkeys(STORE["blocked_devices"] + incoming["blocked_devices"]))
            STORE["blocked_sites"] = list(dict.fromkeys(STORE["blocked_sites"] + incoming["blocked_sites"]))
            existing_ann = {a.get("id") for a in STORE["announcements"] if isinstance(a, dict)}
            STORE["announcements"].extend(a for a in incoming["announcements"] if isinstance(a, dict) and a.get("id") not in existing_ann)
            STORE["announcements"] = STORE["announcements"][-200:]
        record_event("admin_json_import", {"mode": mode, "users_imported": imported_users, "events_imported": len(incoming["events"])})
        return jsonify({"ok": True, "mode": mode, "users_imported": imported_users, "events_imported": len(incoming["events"]), "messages_imported": len(incoming["messages"])})
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify({"ok": False, "error": str(exc)[:400]}), 400


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"ok": False, "error": "Gönderilen dosya çok büyük."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False, threaded=True)
