# Free.ai Coder

Free AI coding assistant for your terminal. A free, open-source alternative to Claude Code, Cursor, and GitHub Copilot.

**[Website](https://free.ai/coder/)** | **[PyPI](https://pypi.org/project/freeai-code/)** | **[npm](https://www.npmjs.com/package/freeai-code)** | **[Web IDE](https://free.ai/coder/)**

## Install

One line, nothing to set up first (no Python, no pip, no sudo). It uses [uv](https://docs.astral.sh/uv/) and installs it for you if missing.

```bash
# macOS / Linux
curl -fsSL https://free.ai/install.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://free.ai/install.ps1 | iex
```

Re-run the same line to upgrade. Pin a version with `FREE_CODE_VERSION=0.1.4` (PowerShell: `$env:FREE_CODE_VERSION = "0.1.4"`). The scripts are in [`install/`](install/): they use uv, else pipx, else a private venv in `~/.free-code`, and put `free-code` in `~/.local/bin`.

Already have Python or Node?

```bash
pip install freeai-code
npm install -g freeai-code
```

## First run

Run `free-code` in a project. The free anonymous tier works with no account.

To sign in, run `free-code login`. It opens your browser at free.ai; sign in or create an account there, press Approve, and the CLI receives a key automatically (stored in `~/.free-code/config.yaml`, mode 600). On a remote machine it prints the address and a short code instead, so you can approve from any device (`free-code login --no-browser`). When the anonymous daily limit is used up, the CLI offers the same sign-in on the spot. Prefer to paste a key or use your own provider? `free-code login --manual`.

On first run in a terminal it also asks once whether to send anonymous crash and tool-failure reports. The default is no. See [Feedback](#feedback-opt-in).

## Quick Start

```bash
# Interactive coding session in your project directory
cd your-project/
free-code

# Ask a one-shot question about your codebase
free-code ask "How does the auth system work?"

# Execute a task
free-code run "Add unit tests for the User model"

# Run a task in the Free.ai cloud (codex-style) and watch it
free-code cloud "Fix the failing CI on main" --repo https://github.com/you/project

# Initialize and scan codebase
free-code init
```

## Commands

| Command | Description |
|---------|-------------|
| `free-code` | Interactive coding session (alias: `free-code chat`). `-c` resumes the latest session, `--resume ID` a specific one |
| `free-code -p "task"` | Non-interactive: print the answer and exit (see [Scripts and CI](#scripts-and-ci)) |
| `free-code ask "question"` | One-shot question about the codebase |
| `free-code run "task"` | Execute a coding task |
| `free-code models` | Every model Free.ai serves, free tier first (`--free`, `--paid`, `--type image`, a search word) |
| `free-code tools` | The tools the agent has (local, platform, MCP) and what needs approval (`--refresh` fetches the server manifest) |
| `free-code mcp add/list/remove/tools/trust` | Use MCP servers such as sttai-mcp |
| `free-code rc` | Start a session you can watch and drive from the app (`rc status`, `rc off`) |
| `free-code sessions` | Saved sessions you can resume |
| `free-code login` / `logout` | Sign in through the browser / forget the key |
| `free-code update` | Upgrade through whatever installed it (`--check`, `--version X`, `--dry-run`) |
| `free-code feedback "text"` | Send feedback you type |
| `free-code config feedback on\|off\|status` | Opt-in automatic reports |
| `free-code cloud "task"` | Run a coding task in the Free.ai cloud (`--repo URL`, `--model`, `--no-watch`, `--download DIR`) |
| `free-code tasks` | List your recent cloud tasks |
| `free-code init` | Initialize config, scan codebase |
| `free-code config` | Show current configuration |
| `free-code config set key value` | Set a config value |

Inside a session: `/model` (picker), `/models`, `/tools`, `/sessions`, `/resume`, `/feedback`, `/update`, `/compact`, `/clear`, `/diff`, `/status`, `/test`, `/files`, `/repo`, `/help`.

## Features

- File read/write with diff preview and approval gates
- Shell command execution (with confirmation in safe mode)
- Git integration (status, diff, commit, log), test runner auto-detection (pytest, jest, go test, cargo test)
- `web_fetch` to read docs and issues
- Platform tools that call Free.ai with your key (see below): image, speech, music and video generation saved into your project, audio transcription, OCR of screenshots and PDFs, web search with citations, translation of strings and whole i18n files
- MCP client (stdio and HTTP): use sttai-mcp, Claude Desktop style servers and others
- Model picker over everything Free.ai serves, honest about free tier versus paid
- Sessions saved locally and resumable
- Non-interactive mode with JSON output and exit codes for scripts and CI
- Opt-in feedback, and self-update with a once-a-day version notice
- Tools shipped from a server manifest, so new tools and tool fixes arrive without a release (offline falls back to the built-in set)
- Streaming output, context window optimization, repo map, .gitignore-aware discovery
- Session sync with [web](https://free.ai/coder/) and cloud tasks on Free.ai's GPUs

## Platform tools

The model can call these. They use your Free.ai key (or the free anonymous tier) and the public API at api.free.ai.

| Tool | What it does | Approval in safe mode |
|------|--------------|-----------------------|
| `generate_image` | Text to image, saved to a path in your project | yes (writes a file) |
| `generate_speech` | Text to speech audio, saved to your project | yes |
| `generate_music` | Short music clip, saved to your project | yes |
| `generate_video` | Short video clip, saved to your project (slow) | yes |
| `transcribe_audio` | Speech to text for an audio or video file | yes (uploads the file) |
| `ocr_image` | Text out of an image or PDF | yes (uploads the file) |
| `web_search` | Cited web answer plus source links | no |
| `translate_text` | Translate a short string | no |
| `translate_file` | Translate a JSON or YAML locale file, protecting `{0}`, `{name}`, `%s` placeholders; a string whose placeholders change is left in the source language and reported | writes the new file |

Free tier versus paid: `free-code models` marks each model. "free tier" means no token price (Free.ai's self-hosted models, within the daily limits); "paid" means billed in tokens and it needs a key with balance. Self-hosted image, video and audio models list no per-token price and count against the daily free allowance.

## MCP servers

```bash
pip install sttai-mcp
free-code mcp add stt-ai sttai-mcp --env STT_API_KEY='${STT_API_KEY}'
free-code mcp tools stt-ai        # check it connects
free-code                          # its tools appear as mcp__stt_ai__<tool>
```

`${NAME}` in `--env` values is read from your own environment, so no secret is written to disk. Servers live in `~/.free-code/mcp.json` (same `mcpServers` shape as Claude Desktop; `--url https://...` adds an HTTP server). A project can ship `.free-code/mcp.json`, but opening a repository must not start programs, so it is ignored until you run `free-code mcp trust` and it re-prompts if the file changes. MCP tools ask for approval in safe mode.

## Remote Control

Watch and drive a session from the Free.ai app or https://free.ai/code/sessions/ (works on a phone).

```bash
free-code rc          # start a session with Remote Control on (or type /rc inside a running one)
free-code rc status   # this account's remote sessions
free-code rc off      # end it (also /rc off)
```

It is opt-in per session and needs `free-code login`. From the app you can see the live transcript and tool calls, send prompts, approve or deny tool calls, interrupt, and end the session. Approvals that safe mode requires stay required: the terminal and the app both get the request and the first answer wins, and every remote prompt is also printed in the terminal. The CLI only makes outbound HTTPS calls to free.ai; messages are relayed between your terminal and devices signed in to your own account, kept as a rolling buffer of the newest 300 events and deleted an hour after the session ends. If the connection drops the session keeps running locally and reconnects. Session text can include code the model read, and tokens and keys are redacted from previews, so treat it like the terminal itself. API for the apps: [docs/remote-control.md](docs/remote-control.md).

## Scripts and CI

```bash
free-code -p "summarize the failing tests" --yes        # plain answer on stdout
free-code run "bump the version" --json --yes           # one JSON object
free-code -p "review this diff: $(git diff)"             # prompt built by your shell
```

`--json` prints `{"ok", "response", "error", "tools": [{"tool","ok"}], "model", "session", "needs_approval"}`. Exit codes: 0 done, 1 failed (API or agent error), 3 an edit or command needed approval and none was given, 130 interrupted. Without a terminal nothing is ever approved implicitly: pass `--yes` to allow edits. `--yes` still refuses commands that match the dangerous list (`rm -rf`, `git reset --hard`, ...). `--max-steps`, `--model` and `--no-mcp` are available everywhere.

## Feedback (opt-in)

Off until you say yes. First run in a terminal asks once (Enter means no). Change it any time:

```bash
free-code config feedback on|off|status
```

`FREE_CODE_NO_TELEMETRY=1` or `DO_NOT_TRACK=1` force it off. `FREE_CODE_TELEMETRY_DRYRUN=1` prints every payload to stderr instead of sending it.

When on, exactly these can be sent to `https://free.ai/api/v1/code/telemetry/`, anonymously (no account, and the server stores no IP):

| Event | Contents |
|-------|----------|
| Crash | free-code version, OS name, Python version, install method, the exception class, free-code's own stack frames (file, line, function), and the exception message with quoted text, paths, numbers and long tokens replaced by placeholders |
| Tool failure | tool name, an error class (`not_found`, `timeout`, `rate_limited`...), model id, and the shape of the arguments: names and a type or size bucket such as `str:1-80`, never values. MCP tool names are not sent |
| Rating | thumbs up or down after a task (asked after the first task, then every fifth), plus a comment only if you type one |
| `/feedback` | the text you type, an email only if you give one. The last exchange (your request and the reply, which may contain code) is added only if you answer yes to "attach it" |

Never sent: your code, file contents, prompts, model replies, shell commands, file paths, argument values, API keys. `/feedback` and `free-code feedback "text"` work even with automatic reports off, because you are sending that message yourself. Failed sends are retried from a small local queue (max 50). Reports are read by the maintainers and used to fix tools and crashes.

## Updating

`free-code update` runs the right upgrade for how you installed it: `uv tool upgrade`, `pipx upgrade`, or `pip install --upgrade` (the installer's private venv, pip and npm). Once a day, without delaying startup, free-code checks whether a newer release exists and prints one line if so; it never installs by itself. Turn the check off with `FREE_CODE_NO_UPDATE_CHECK=1` or `free-code config set update_check false` (it is also off when `CI` is set).

## Tool manifest

The tool list comes from `https://free.ai/api/v1/code/manifest/`, cached for six hours and validated before use: HTTPS only, size-capped, strict schema, and tools are declarative calls to Free.ai API paths that can only save files inside your project. A manifest can add a tool, fix a tool's description or parameters, switch a broken tool off, and announce the newest release. It cannot add or replace local tools (shell, files, git), and an older manifest than the cached one is ignored. Offline, or on any error, the built-in set is used. Opt out with `FREE_CODE_NO_MANIFEST=1` or `free-code config set manifest false`. Only the version is sent in the request.

## Cloud Tasks

Run a coding task on Free.ai's infrastructure instead of your machine: like a codex agent from the terminal. Requires an API key: run `free-code login` (free tier works).

```bash
# Submit and watch (polls until done, prints the markdown report)
free-code cloud "Add rate limiting to the API" --repo https://github.com/you/project

# Pick the cloud model
free-code cloud "Refactor the auth module" --model deepseek-coder

# Fire and forget (you get an email when it finishes)
free-code cloud "Write integration tests" --no-watch

# Download the resulting code as a zip, unpacked into ./result
free-code cloud "Fix the flaky test suite" --download ./result

# List recent tasks
free-code tasks
```

## Configuration

Config is stored at `~/.free-code/config.yaml`.

```bash
# Set your Free.ai token (get one at https://free.ai/account/)
free-code config set token sk-free-xxx

# Use your own API key (BYOK: no markup, use your own provider)
free-code config set provider openai
free-code config set api_key sk-xxx

# Choose a model
free-code config set model qwen7b

# Enable safe mode (confirm before file writes)
free-code config set safe_mode true
```

### Supported Providers

| Provider | Default Model | Free Tier |
|----------|--------------|-----------|
| **free.ai** (default) | Qwen 7B | 50K tokens/day |
| **openai** | GPT-4o | BYOK: no markup |
| **anthropic** | Claude Sonnet 4 | BYOK: no markup |
| **google** | Gemini 2.5 Pro | BYOK: no markup |
| **openrouter** | 346+ models | BYOK: no markup |

## How It Works

1. Scans your project (respects .gitignore)
2. Builds a context map of your codebase
3. Sends your request + relevant code context to the AI
4. AI plans steps, reads/writes files, runs commands
5. You review and approve changes (safe mode)

All file operations happen locally on your machine. The AI sees only what it needs.

## Pricing

- **Anonymous**: 10K tokens/day (Qwen Coder only)
- **Free account**: 50K tokens/day (sign up at [free.ai](https://free.ai/signup/))
- **Paid plans**: From $5/month for 200K tokens ([pricing](https://free.ai/pricing/))
- **BYOK**: $0: bring your own API key, no markup

## Links

- [Free.ai](https://free.ai): 400+ free AI tools
- [Web IDE](https://free.ai/coder/): Use in your browser
- [API Docs](https://free.ai/api/): Build on top of Free.ai
- [Compare vs Claude Code](https://free.ai/compare/coder-vs-claude-code/)
- [Compare vs Cursor](https://free.ai/compare/coder-vs-cursor/)

## vs Competitors

| Feature | Free.ai Coder | Claude Code | OpenAI Codex | Cursor | GitHub Copilot |
|---------|--------------|-------------|--------------|--------|----------------|
| Free tier | 50K tokens/day | None ($20/mo) | None ($200/mo) | None ($20/mo) | None ($10/mo) |
| Self-hosted models | Yes (Qwen, DeepSeek) | No | No | No | No |
| BYOK (bring your own key) | Yes: $0 markup | No | No | Partial | No |
| Open source CLI | Yes (MIT) | No | No | No | No |
| Web IDE | Yes (free.ai/coder/) | No | Yes | Yes | No |
| Terminal access | Yes | Yes | Yes | Yes | No |
| 346+ models | Yes | Claude only | GPT only | Multiple | GPT only |
| Session sync (CLI ↔ web) | Yes | No | No | No | No |

## BYOK (Bring Your Own Key)

Use your own API keys from any provider: zero markup, zero fees. Free.ai just proxies the request.

```bash
# OpenAI
free-code config set provider openai
free-code config set api_key sk-proj-xxx

# Anthropic
free-code config set provider anthropic
free-code config set api_key sk-ant-xxx

# Google
free-code config set provider google
free-code config set api_key AIzaSyxxx

# OpenRouter (access 346+ models)
free-code config set provider openrouter
free-code config set api_key sk-or-xxx
```

When using BYOK, requests go directly to the provider. No markup. No logging. Your key, your usage, your bill.

You can also set keys via environment variables:

```bash
export FREE_CODE_PROVIDER=openai
export FREE_CODE_API_KEY=sk-proj-xxx
free-code
```

## Free.ai Ecosystem

- **[Free.ai](https://free.ai)**: 400+ free AI tools on the web
- **[Python SDK](https://github.com/muddyholdings/free-sdk)**: `pip install free-dot-ai`: use Free.ai from your code
- **[CLI Coder](https://github.com/muddyholdings/free-code)**: `pip install freeai-code`: AI coding in your terminal
- **[iOS App](https://free.ai/ios/)**: Native iPhone/iPad app
- **[Android App](https://free.ai/android/)**: Native Android app
- **[API Docs](https://free.ai/api/)**: Build on top of Free.ai
- **[Web IDE](https://free.ai/coder/)**: Code in your browser

## License

MIT
