# free-code

Free AI coding assistant for your terminal. A free alternative to Claude Code, Cursor, and GitHub Copilot.

Powered by [Free.ai](https://free.ai).

## Install

```bash
npm install -g freeai-code
```

Or with pip:

```bash
pip install free-code
```

Or the universal one-liner:

```bash
curl -fsSL https://free.ai/install | sh
```

## Quick Start

```bash
cd your-project/
free-code                          # Interactive coding session
free-code ask "How does auth work?" # One-shot question
free-code run "Add unit tests"     # Execute a task
```

## Requirements

- Python 3.9+ (the npm package will attempt to install Python dependencies automatically)
- Node.js 14+ (for npm installation method only)

## What is this?

This npm package is a thin wrapper around the `free-code` Python CLI. On `npm install`, it automatically installs the Python package via pip. The `free-code` command then delegates to the Python binary.

If you already have Python, you can skip npm and install directly:

```bash
pip install free-code
```

## Features

- AI-powered code reading, writing, and editing
- Shell command execution (with confirmation)
- Git integration
- Test runner auto-detection
- Streaming output with syntax highlighting
- Context-aware codebase understanding
- Multiple AI providers (Free.ai, OpenAI, Anthropic, Google, OpenRouter)

## License

MIT
