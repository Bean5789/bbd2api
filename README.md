# bbd2api

A lightweight Flask reverse proxy that bridges the **Claude Messages API** and **OpenAI Chat Completions API** to [backboard.io](https://app.backboard.io)'s assistant/thread API — with tool calling, thread pooling, and sub-5s response times.

## Features

- **Dual API compatibility** — `/v1/messages` (Claude) and `/v1/chat/completions` (OpenAI)
- **Tool calling** — prompt-injection approach; compact format (~800 chars vs ~68KB for full schemas)
- **Multi-turn tool continuity** — `tool_result` messages route back to the correct thread automatically
- **Thread pool** — pre-warms N threads at startup; zero cold-start on new conversations
- **Global assistant** — single assistant reused across all requests; eliminates per-request creation overhead
- **System prompt stability** — strips dynamic tags (`<system-reminder>` etc.) before hashing to prevent cache misses
- **Model routing** — auto-detects provider from model name (claude→anthropic, gpt→openai, gemini→google, etc.)

## Quick Start

```bash
git clone https://github.com/N1nEmAn/bbd2api.git
cd bbd2api
pip install -r requirements.txt
cp .env.example .env
# edit .env and set BBD_API_KEY
python server.py
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BBD_API_KEY` | *(required)* | Your backboard.io API key |
| `BBD_UPSTREAM` | `https://app.backboard.io/api` | Upstream API base URL |
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `10088` | Listen port |
| `THREAD_TTL` | `1800` | Thread cache TTL in seconds |
| `DEBUG` | `1` | Enable debug logging (`0` to disable) |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/messages` | Claude Messages API (streaming + sync) |
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions API |
| `GET` | `/v1/models` | List available models |
| `GET` | `/health` | Health check |
| `GET` | `/debug/state` | Show assistant/thread/pool state |
| `POST` | `/debug/clear` | Clear thread cache |

## Usage with Claude Code / OpenAI clients

```bash
# Claude-style
curl http://localhost:10088/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"Hello"}],"stream":true}'

# OpenAI-style
curl http://localhost:10088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello"}]}'
```

Or point any OpenAI-compatible client (LiteLLM, Continue, etc.) at `http://localhost:10088`.

## Architecture

```
Client (Claude/OpenAI API)
        │
        ▼
   bbd2api proxy (Flask, port 10088)
        │  • thread pool (pre-warmed)
        │  • tool prompt injection
        │  • tool_result continuity (tool_use_id → thread mapping)
        │  • SSE format translation
        ▼
  backboard.io API
  /assistants/{aid}/threads/{tid}/messages
```

### Tool Calling Flow

1. Client sends request with `tools` list
2. Proxy injects compact tool format instructions into the user message
3. Model responds with JSON: `{"tool":"name","args":{...}}`
4. Proxy translates to `tool_use` content blocks, stores `tool_use_id → thread_id`
5. Client executes tool, sends back `tool_result`
6. Proxy detects `tool_result`, looks up thread via `tool_use_id`, sends result to same thread
7. Model continues reasoning and produces final answer

## License

[CC BY-NC-SA 4.0](LICENSE) — Non-commercial use only. If you use or adapt this project, **you must credit the original author (N1nEmAn)** and distribute your changes under the same license.

Commercial use is **prohibited** without explicit written permission from the author.

## Disclaimer

See [DISCLAIMER.md](DISCLAIMER.md).
