# LinkedIn Sales Tracker MCP

A personal MCP server that tracks a selling-by-chat pipeline on LinkedIn without manual exports.

- **Email intake:** polls Gmail (read-only) every 15 minutes for LinkedIn message notification
  emails and logs each new reply as a preview.
- **Screenshot intake:** a Chrome extension button. When you click it on an open LinkedIn
  conversation, it screenshots the visible tab and sends the image to a localhost endpoint. Claude
  (vision) turns the image into messages. Previews from emails are replaced with the full text.
- **Claude Desktop tools:** ask "who needs a follow-up?", read threads, and move people through stages.

It never scrapes page code, never sends requests to LinkedIn, never logs in for you and never runs
in the background on LinkedIn. The extension does nothing until you click it.

## Layout

| File | What it does |
|---|---|
| `server.py` | MCP server (stdio) and tool definitions; also starts the capture endpoint and Gmail poller |
| `capture_api.py` | FastAPI endpoint on `127.0.0.1:<capture_port>/capture` for screenshots |
| `vision.py` | Claude API call that turns a screenshot into message JSON |
| `gmail_sync.py` | Gmail OAuth, polling and parsing |
| `db.py` | SQLite schema, merging, deduplication, stage rules and queries |
| `config.py` | Loads `config.toml` |
| `extension/` | Manifest V3 Chrome extension (`manifest.json`, `background.js`, `popup.html`, `popup.js`) |
| `tests/` | Parsing and dedupe tests; sample emails and screenshots go in `tests/samples/` |

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.toml config.toml
```

Edit `config.toml`:

- `my_name`: your name as LinkedIn shows it in threads, so the vision step can tell your messages apart.
- `shared_secret`: any long random string, for example from `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `anthropic_api_key_path`: a file that holds only your Anthropic API key, for example
  `~/.config/linkedin-tracker/anthropic.key` (`chmod 600` it). If the file is missing, the SDK falls back
  to `ANTHROPIC_API_KEY` or an `ant auth login` profile.

### Gmail OAuth

1. Turn on email notifications for LinkedIn messages (LinkedIn → Settings → Notifications → Messages → Email).
2. In Google Cloud Console, create a project, enable the **Gmail API**, set up the OAuth consent
   screen (External, add yourself as a test user) and create an **OAuth client ID** of type
   **Desktop app**. Download the JSON file as `credentials.json` in this folder.
3. Run `python gmail_sync.py login`. A browser opens. Approve read-only access; `token.json` is saved.

The scope is `gmail.readonly`. Emails are only read, never changed. Processed emails are tracked by
Gmail message id, so nothing is logged twice.

### Confirm the email format (do this before trusting email intake)

The parser is built on **assumed** LinkedIn email formats: the From address, a display name like
"Sam Lee via LinkedIn", and subjects like "Sam just messaged you". Check them against your inbox:

```bash
python gmail_sync.py samples   # prints From, Subject, snippet and what the parser made of each
```

If anything is off, adjust `[gmail] query`, `subject_patterns` or `from_name_suffix` in
`config.toml`, or send the samples over so the parser can be fixed. Save real examples as JSON in
`tests/samples/emails/` (same shape as the synthetic ones there) and they become test cases.
Emails the parser can't read, such as digests covering several people, are reported as
`unparsed` by `sync_email` and are not guessed at.

### Chrome extension

1. Open `chrome://extensions`, turn on **Developer mode** and click **Load unpacked**. Pick the `extension/` folder.
2. Click the extension's icon, open **Settings**, paste your `shared_secret` and click Save.
3. Open a conversation on linkedin.com/messaging and click **Log this conversation**. You'll see
   something like "Logged 3 new messages with Sam, stage: replied".

For long threads, scroll up and click again. Overlapping messages are deduplicated. If two contacts
share a name, the popup lists their ids; enter the right id under Settings → Contact id and capture again.

### Claude Desktop

Add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), using absolute paths:

```json
{
  "mcpServers": {
    "linkedin-tracker": {
      "command": "/absolute/path/to/linkedin-sbc-nav/.venv/bin/python",
      "args": ["/absolute/path/to/linkedin-sbc-nav/server.py"]
    }
  }
}
```

Restart Claude Desktop. The capture endpoint and Gmail poller run while Claude Desktop is open. To
run the endpoint on its own, use `python capture_api.py`.

## Tools

| Tool | Purpose |
|---|---|
| `sync_email()` | Pull new notification emails now and report what changed |
| `list_contacts(stage?, needs_follow_up?)` | Name, company, stage, last in/out, next follow-up |
| `get_conversation(contact)` | The stored thread, so Claude can judge the stage |
| `update_contact(contact, stage?, role?, company?, opener_used?, next_follow_up?, notes?, profile_url?, reason?)` | Update fields; stage changes go to `stage_history` |
| `log_message(contact, direction, text, sent_at?)` | Manual fallback for anything missed |
| `follow_ups_due(today?)` | `next_follow_up` has passed, or your last message is older than `follow_up_days` with no reply |
| `opener_stats()` | Reply rate per `opener_used` |
| `pipeline_summary()` | Counts per stage plus the week's movement |

A contact can be named by id, name or profile URL. If a name matches more than one person, the tool
returns the candidates instead of guessing.

## Stages and rules

Stages, in order: `messaged`, `replied`, `chatting`, `call_booked`, `proposal_sent`, `won`, `lost`, `not_a_fit`.

On import:

- Only outbound messages: `messaged`.
- At least one inbound reply: `replied`.
- Three or more messages each way: `chatting`.
- Import rules only move a contact forward through those three stages. Later stages are set only by
  you or by Claude through `update_contact`, and automatic rules never override them.

## Deduplication

A new message is a duplicate of a stored one when it has the same direction, the same text after
normalising whitespace, quotes and trailing ellipses (or 95% similar for longer texts, to absorb
small vision misreads), and timestamps within 24 hours of each other (or a missing timestamp).
A truncated email preview is upgraded in place when a screenshot captures the full message, and
the email's exact timestamp is kept.

## Tests

```bash
pytest
```

The email tests use synthetic samples until real ones are added. To check the vision step on real
screenshots, save `name.png` with a matching `name.json`
(`{"contact_name": "...", "messages": [{"sender": "me", "text": "..."}]}`) in
`tests/samples/screenshots/` and run `RUN_VISION_TESTS=1 pytest -k live`. That test calls the Claude API.

The vision step uses `claude-opus-5-5` with structured outputs, and opts into server-side
fallbacks so a request a safety classifier declines is retried on another model. Change
`vision_model` in `config.toml` to use a different model.
