# Web OS — Stable Project Spec

This file exists so the project's goal is not dependent on remembering an old chat. Update it whenever a confirmed requirement changes.

## Project choices that should stay fixed

- Runtime: Python + Flask on Render.
- Storage: RAM-only for now; data loss on restart is acceptable. No PostgreSQL or persistent disk.
- Data backup: manual JSON download and JSON import. Import must support merge/integration and full replacement modes. Older JSONs should be migrated best-effort.
- UI: keep a polished, dark OLED/watch-like design, responsive down to small screens.
- Login flow: input initially limits to 4 characters; typing `4564` allows 9 characters; `456456456` is the currently requested admin code. Server verifies it. Admin access has no trial timer.
- Remove WebMusic and App Store; do not re-add them unless explicitly asked.

## Apps

### ProxWeb — highest priority
- Search the web from the server, display result title/snippet/link, and log searches/clicked links.
- Apply an administrator-managed block list to result domains and normal link opening.
- Do not make it an unrestricted open proxy or a network-rule bypass. Links open normally in the browser.

### Gemini AI — highest priority
- Real server-side Gemini API call, using an API key that lives in Render Environment only.
- Support `GEMINI_API_KEY`, `GOOGLE_API_KEY`, and `GOOGLE_AI_API_KEY` environment variable names.
- No client-side exposure of the key.
- Chat history is held in RAM, is logged, and is included in backups.

### Separate Admin Panel — highest priority
- Separate `/admin` page, not just a settings popover.
- Admin access does not expire with the normal user's trial.
- Show currently connected/recent devices, account state, recent activity and events by device.
- Admin may assign custom device labels visible only within their own panel, separate from the user's public username.
- Block/unblock devices and manage blocked domains.
- Publish/delete announcements; manage the displayed trial duration and price.
- View app activity logs, ChatApp messages and Gemini conversation records.
- Export all current RAM data to JSON and import older/new JSON using merge or replace.

### ChatApp
- Real DMs between users known to the service, not local-only echo.
- Message history lives in RAM and is included in JSON backup.

### Bank app — intentionally incomplete pending separate service confirmation
- Keep the current separate service URL/configuration and `/api/link`, `/api/balance`, `/api/subscribe` calls exposed through the Web OS.
- This integration has not been tested against the remote bank service; align field names/routes once its source/API is available.
- Test account only; don't request real card PIN/payment details.

### Settings
- Edit the user's profile display name and show device ID / trial state.

## RAM and JSON data

Backup format: `webos-ram-backup`, version 2. It includes users and password hashes, events/Cookie log, ChatApp messages, Gemini messages, settings, administrator-only device labels, blocked devices, blocked sites, announcements and export timestamp. It excludes Flask session secrets, API keys and plaintext passwords.

JSON merge should preserve unrelated in-memory records. Full replace should preserve the active administrator identity by requiring its device to exist in the incoming backup as admin.

## Current known limits to keep visible

- RAM clears when the serving process restarts. Keep one service instance and one Gunicorn worker; more workers do not share RAM.
- Browser-generated device ID is a convenience ID, not a hardware ID.
- Website search provider HTML may change/rate-limit.
- Bank API behavior cannot be confirmed until the separate bank server is accessible.
- Users are informed that app logs, Gemini content and ChatApp messages are visible to the admin.
