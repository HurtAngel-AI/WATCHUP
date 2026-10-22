# Web OS — RAM edition

Turkish-language Flask Web OS starter. It is deliberately **RAM-only**: no PostgreSQL, SQLite, or persistent disk is used. The UI is a compact dark watch/phone interface with ProxWeb, Gemini AI, ChatApp, Banka, Ayarlar, and a separate Admin Panel page.

## Included

- Watch-style responsive launcher and app views; Music and App Store are removed.
- Requested login flow: password box starts at four characters; typing `4564` expands it to nine characters so `456456456` can enter as admin. Admin passcode is checked on the server. New devices can create a 4+ character password; only a password hash is stored in RAM.
- ProxWeb web search, result descriptions, normal browser link opening, domain block rules, search and opened-site activity logs. This is not an open proxy or a network-restriction bypass tunnel.
- Gemini chat wired to the server-side Gemini REST endpoint. It reads `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or `GOOGLE_AI_API_KEY` from the service environment; the key is never placed in HTML/JavaScript. Conversation history is held in RAM and added to JSON backups.
- ChatApp direct messages between users registered in this Web OS, with RAM message history and a basic polling-based UI.
- Separate `/admin` page: live-ish device presence (10-second refresh; online if heartbeat is no older than 75 seconds), administrator-only device labels, device blocking, site block list, trial duration/price display settings, announcements, per-device activity filter, ChatApp/Gemini message inspection, JSON export/import.
- JSON export/import for the current format and adapter support for the earlier `webos-backup` v1 shape plus common `users`/`devices`, `events`/`logs`, and `messages` shapes.
- Existing bank service connection kept via `BANK_SERVER_URL` and these expected remote paths: `/api/link`, `/api/balance`, `/api/subscribe`. The external bank service was not available for integration testing; its request fields/endpoints may need alignment with that service.
- `/health` reports whether the process is responding and basic RAM-store counts.

## Render setup — no database required

For an existing Render Python Web Service, upload these files to the repository connected to that service and deploy.

**Build Command**

```bash
pip install -r requirements.txt
```

**Start Command**

```bash
gunicorn --workers 1 --threads 4 --timeout 45 app:app
```

Keep it at **one instance and one Gunicorn worker**. RAM data is local to one Python process; multiple workers or multiple service instances would have different independent data stores.

In Render → Web Service → Environment, set:

| Variable | Value |
|---|---|
| `SESSION_SECRET` | A long random secret, for example 64+ random hexadecimal characters. Keep it private. |
| `ADMIN_PASSCODE` | The desired admin code. `456456456` is the built-in fallback to preserve the requested behavior; change it before making a public service because anyone who knows the code can become an admin. |
| `COOKIE_SECURE` | `true` on Render HTTPS. |
| `GEMINI_API_KEY` | Keep/use the existing Gemini key here if that is the variable name already set. If your existing key uses `GOOGLE_API_KEY` or `GOOGLE_AI_API_KEY`, the app also checks those names. Never put the key in the ZIP or in HTML. |
| `GEMINI_MODEL` | `gemini-3.8-flash` by default. Change this if that model is unavailable to your key/project. |
| `BANK_SERVER_URL` | Optional; defaults to `https://sanal-banka.onrender.com`. |

No `DATABASE_URL` is required. `COOKIE_SECURE=false` is only for plain-HTTP local testing. If you set `SESSION_SECRET` later, keep it stable between deployments; changing it invalidates browser sessions.

The Render service's Python version can use `.python-version` from this ZIP. Set `/health` as the service health-check path if you want an HTTP health check.

## Data, backup and restore

- User accounts, hashed passwords, device presence, app logs, site block rules, administrator labels, announcements, trial settings, ChatApp messages and Gemini history exist in RAM only.
- Restart, redeploy, instance replacement, or a process crash clears the RAM store. The next login starts a new store.
- An administrator can download `webos-ram-backup.json` manually and later import it.
- **Birleştir** adds/imports records while leaving unrelated current records in place. It is the preferred choice for an older JSON file.
- **Tam geri yükle** replaces the RAM data and is accepted only if the backup includes the currently logged-in admin device as an admin.
- Older backups lacking some fields are imported best-effort. Review the result after importing; a legacy export may not contain all data needed to sign into every account.
- Backup files include device identifiers, password hashes, audit records, and message content. Keep them private. Passwords themselves are never exported as plaintext by this version.

## Privacy and current limits

The login screen explains that application activity is recorded. ProxWeb search text and opened URL/host are logged; successful Gemini prompts and answers, and ChatApp messages, are retained in RAM and are visible to the administrator through the Admin Panel. Admin-only device labels do not rename a user's public profile. Real device hardware IDs are not collected; the ID is generated into browser local storage and can change if that storage is cleared.

ProxWeb uses a search provider and opens normal web links in the user's browser. It does not fetch/rewrite arbitrary pages through the server, and it does not evade network filters. Search providers may rate-limit or change their HTML; if they do, the app displays an error instead of fabricated results.

The bank connection is preserved but **not live-verified** from this build environment. No real banking card or PIN should be used in this test UI; use only the separate virtual bank service's test account/code.

## Validation performed for this ZIP

- Python syntax compilation passed (`python -m py_compile app.py`).
- Both HTML files' JavaScript blocks passed Node syntax checking.
- Full Flask request/response integration tests could not run in the build container because Flask dependencies were not installed there and package download access was unavailable. Render will install the pinned requirements during the build. After deployment, check `/health`, try normal login/admin login, ProxWeb search, Gemini, ChatApp with two devices, JSON export/import, and site/device rules.
