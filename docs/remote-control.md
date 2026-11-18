# Remote Control API

Watch and drive a running `free-code` session from the Free.ai app or from https://free.ai/code/sessions/.
This page is the contract for app developers (Android, iOS, web).

## How it works

1. The user runs `free-code rc` (or `/rc` inside a session). Remote Control is opt-in per session.
2. The CLI makes outbound HTTPS calls to `https://free.ai`. It registers a **session**, pushes transcript **events**, and polls for **commands**.
3. An app signed in to the **same account** lists the account's sessions, polls the events, and posts commands (prompt, approve, deny, interrupt, end).
4. The relay only forwards. It executes nothing. Tool calls that need approval (file writes, shell commands, uploads, MCP tools in safe mode) still need approval; the CLI raises an `approval_request` and waits for the first answer from the terminal or any app. Every remote prompt is also printed in the terminal.

Transport is short polling over HTTPS (about every 1.5 seconds), not websockets. Nothing is stored beyond a rolling buffer of the newest 300 events, deleted an hour after the session ends; a session whose terminal goes silent for 30 minutes is ended. Data passes through free.ai's servers only (no third party). The session can contain code and file contents the model read, so it is visible to the owning account only.

If the relay drops, the CLI keeps working locally, buffers up to 500 events and reconnects with backoff. `free-code rc off` (or `/rc off`) ends it.

## Authentication

App calls use the account's API key: `Authorization: Bearer sk-free-...` (the same key the Android and iOS apps already hold), or the signed-in web session cookie plus `X-CSRFToken` on POST (the web page uses this). A Bearer header takes precedence over a cookie. Keys need no CSRF token.

The CLI side additionally sends `X-RC-Secret: <secret>` (returned once at registration, only its hash is stored). Apps never see the secret; they do not need it.

A session that belongs to another account answers `404`, the same as a missing one. A missing or bad key answers `401 {"error": "Sign in required."}`.

All bodies are JSON (`Content-Type: application/json`), at most 256 KB.

## Endpoints

Base: `https://free.ai/api/v1/code/rc/sessions/`

### List sessions (app)

`GET /api/v1/code/rc/sessions/`

```json
{
  "sessions": [
    {
      "id": "74c99245-ba8e-4e26-8f0e-924ffe1e7d12",
      "host": "my-laptop",
      "project": "my-repo",
      "model": "qwen7b",
      "status": "live",
      "online": true,
      "created_at": "2026-11-17T21:00:00+00:00",
      "last_seen": "2026-11-17T21:05:12+00:00",
      "last_seq": 42
    }
  ],
  "poll_interval": 1.5
}
```

`status` is `live` or `ended`. `online` is true when the terminal was heard from in the last 20 seconds; show "terminal not responding" when a live session is not online. Newest first, at most 20, ended sessions disappear an hour after they end. Poll this list about every 5 seconds. Limit 120 calls per minute.

### Read the transcript (app)

`GET /api/v1/code/rc/sessions/<id>/events/?after=<seq>`

Returns events with `seq` greater than `after`, oldest first, at most 200 per call. Start with `after=0`, then pass the returned `last_seq` each time.

```json
{
  "events": [
    {"seq": 7, "type": "assistant", "data": {"text": "Done."}, "ts": "2026-11-17T21:05:12+00:00"}
  ],
  "last_seq": 7,
  "session": { "id": "...", "status": "live", "online": true, "host": "my-laptop", "project": "my-repo", "model": "qwen7b", "created_at": "...", "last_seen": "...", "last_seq": 7 },
  "poll_interval": 1.5
}
```

Poll about every 1.5 seconds while the screen is visible (back off to 5 seconds when idle or in the background). Limit 240 calls per minute. `seq` is a single counter per session shared with commands, so cursors may skip numbers.

### Send a command (app)

`POST /api/v1/code/rc/sessions/<id>/commands/` returns `201 {"ok": true, "seq": 12}`.

| Body | Meaning |
|------|---------|
| `{"type": "prompt", "text": "run the tests"}` | A prompt for the agent (up to 8000 characters). Shown in the terminal as `[from the app]`, then run. If the agent is busy it runs next. |
| `{"type": "approve", "approval_id": "a1b2c3d4e5f6"}` | Approve the pending `approval_request` with that id. Ignored if it was already answered. |
| `{"type": "deny", "approval_id": "a1b2c3d4e5f6"}` | Deny it. |
| `{"type": "interrupt"}` | Stop the current task after the step in flight (a running shell command is not killed). Also denies a pending approval. |
| `{"type": "end"}` | End the session. (Same effect as the end endpoint below.) |

Anything else is `400 {"error": "Not a valid command."}`. An ended session answers `410 {"error": "Session ended.", "status": "ended"}`. Limit 60 commands per minute per account. `approval_id` is 1 to 64 characters of `A-Z a-z 0-9 _ -`.

### End a session (app)

`POST /api/v1/code/rc/sessions/<id>/end/` returns `{"ok": true, "status": "ended"}`. The terminal is told (it exits Remote Control and keeps running locally).

## Event types (CLI to app)

Every event is `{"seq", "type", "data", "ts"}`. Treat unknown types and fields as ignorable. Render `text` and `preview` fields as plain text, never as HTML.

| type | data | Notes |
|------|------|-------|
| `prompt` | `{"text", "source"}` | A user prompt. `source` is `terminal` or `app`. |
| `assistant` | `{"text"}` | A model message (markdown). Up to 4000 characters, credentials redacted. |
| `tool_call` | `{"id", "tool", "preview"}` | A tool is about to run. `preview` is a short summary (path, command, query), not file contents. |
| `tool_result` | `{"id", "ok", "preview"}` | Its outcome, `preview` up to 800 characters. `ok` false means the tool reported an error. |
| `approval_request` | `{"id", "tool", "preview", "message"}` | Needs an answer. Show Approve and Deny; post a command with `approval_id` equal to `id`. |
| `approval_resolved` | `{"id", "approved", "by"}` | Answered. `by` is `app`, `terminal` or `interrupt`. Remove the buttons. |
| `status` | `{"state"}` | `idle`, `working`, `waiting_approval`, `ended`. |
| `notice` | `{"text"}` | Informational (for example "Interrupted from the app."). |
| `error` | `{"text"}` | A problem to show. |

A typical turn: `prompt`, `status working`, `tool_call`, `approval_request`, `approval_resolved`, `tool_result`, `assistant`, `status idle`. Approvals are raised before the corresponding `tool_call`.

## CLI side (for reference, not for apps)

| Call | Auth | Purpose |
|------|------|---------|
| `POST /api/v1/code/rc/sessions/` body `{"host", "project", "model"}` | Bearer | Register. `201 {"session_id", "secret", "poll_interval", "buffer", "url"}`. At most 5 live sessions per account (`409`), 20 registrations per hour (`429`). |
| `POST .../<id>/events/` body `{"events": [{"type", "data"}, ...]}` | Bearer + `X-RC-Secret` | Push up to 50 events. Unknown types are dropped; an event over 16 KB is truncated. Also the heartbeat. `410` once the session ended. |
| `GET .../<id>/commands/?after=<seq>` | Bearer + `X-RC-Secret` | `{"commands": [{"seq", "type", "data"}], "last_seq", "status", "poll_interval"}`. |
| `POST .../<id>/end/` | Bearer + `X-RC-Secret` | End from the terminal. |

## Errors

| Status | Meaning |
|--------|---------|
| 400 | Invalid JSON or invalid command |
| 401 | Missing or bad key |
| 403 | A browser session POST without a valid CSRF token |
| 404 | No such session for this account (or a wrong CLI secret) |
| 409 | Too many live sessions |
| 410 | Session ended |
| 413 | Body too large |
| 429 | Rate limited, slow down |

## Guidance for the app UI

- Home: a "Remote sessions" list from the list endpoint, live ones first.
- Session screen: transcript, a prompt box, Interrupt and End buttons, and Approve and Deny on any unresolved `approval_request`. Show the `preview` so the user can see what they are approving.
- Disable the prompt box when `status` is `ended`; show a "terminal not responding" banner when `online` is false.
- Send a local notification when an `approval_request` arrives while the app is backgrounded only if you poll in the background; there is no push channel yet.
