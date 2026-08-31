# CLI Chatbot (Groq, raw HTTP)

A command-line chatbot that talks to the Groq API (OpenAI-compatible chat
completions format) using nothing but `requests` — no `openai`/`anthropic`
SDK, no LangChain or other framework. The request body is built by hand and
the streamed response is parsed line-by-line, so the wire format is fully
visible in the code.

## Requirements

- Python 3.10+
- A [Groq API key](https://console.groq.com/keys) (free tier available)

## Setup

1. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

2. Create a `.env` file in this directory with your key (no quotes needed):

   ```
   GROQ_API_KEY=gsk_your_key_here
   ```

   `.env` is already gitignored — it will never be committed.

   Alternatively, skip the file and set the environment variable directly
   for the current shell session:

   ```powershell
   $env:GROQ_API_KEY = "gsk_your_key_here"      # PowerShell
   ```

   ```bash
   export GROQ_API_KEY="gsk_your_key_here"      # bash
   ```

## Running it

```powershell
python chatbot.py
```

Type messages at the `You:` prompt. Responses stream token-by-token as
they arrive from the API.

## Commands

| Command | Effect |
|---|---|
| `/history` | Print the exact JSON message array that would be sent to the API next (system prompt + full conversation so far) |
| `/temp <value>` | Set temperature (0.0–1.0) for subsequent turns, without restarting |
| `/reset` | Clear conversation history back to just the persona — the system prompt is untouched |
| `/debug on` / `/debug off` | Print `time.time()` before each streamed chunk's text, to inspect arrival timing |
| `/help` | List commands |
| `/quit` / `/exit` | Leave the chat (Ctrl+C / Ctrl+D also work) |

## Testing it end-to-end

A quick smoke test to exercise every feature in one pass:

1. Send a normal message — confirm it prints word-by-word, not all at once.
2. `/temp 0.2`, ask the same question a few times, then `/temp 1.0` and ask
   it again — lower temperature should give more repetitive phrasing,
   higher should vary more.
3. `/debug on`, send a message — each chunk should print on its own line
   prefixed with a timestamp. `/debug off` returns to normal streaming.
4. `/history` — confirm the system message (persona) is first, followed by
   the actual conversation.
5. `/reset`, then `/history` again — should show only the system message.
6. `/temp abc` and `/temp 5` — should print validation errors, not crash.

## Editing the persona

The entire system prompt is the `SYSTEM_PROMPT` constant near the top of
`chatbot.py`. Change the text and re-run — no other code needs to change.
It's folded into the `messages` array as a `{"role": "system", ...}` entry
by `build_messages()`, since Groq's chat completions format has no separate
top-level `system` field (unlike Anthropic's Messages API, which this
project was originally built against before migrating to Groq).

## Configuration

All at the top of `chatbot.py`:

| Constant | Meaning |
|---|---|
| `SYSTEM_PROMPT` | The persona / system prompt |
| `MODEL` | Groq model ID (e.g. `openai/gpt-oss-120b`) |
| `DEFAULT_TEMPERATURE` | Starting temperature, overridable in-session with `/temp` |
| `MAX_TOKENS` | Max tokens per response |
| `API_URL` | Groq chat completions endpoint |
| `MAX_RETRIES`, `INITIAL_BACKOFF_SECONDS`, `BACKOFF_MULTIPLIER`, `MAX_BACKOFF_SECONDS` | Retry/backoff behavior on HTTP 429 (rate limit) responses only |

### Picking a model

Groq deprecates models over time — if `MODEL` starts returning
`model_not_found`, list what your key currently has access to:

```powershell
python -c "import requests,os; from chatbot import load_dotenv; load_dotenv(); r=requests.get('https://api.groq.com/openai/v1/models', headers={'Authorization': f\"Bearer {os.environ['GROQ_API_KEY']}\"}); print([m['id'] for m in r.json()['data']])"
```

## How it works (architecture notes)

- **Conversation history** is a plain Python list of `{role, content}`
  dicts held in memory for the session (`history` in `main()`). It's
  cleared by `/reset` and lost when the process exits — there's no
  persistence across restarts.
- **Streaming parser** (`stream_assistant_reply`): Groq streams plain
  `data: {json}` lines (not typed SSE `event:`/`data:` pairs like
  Anthropic's Messages API), terminated by a literal `data: [DONE]` line.
  The code reads `response.iter_lines()` one line at a time, skips
  anything that isn't a `data:` line, and pulls text out of
  `choices[0].delta.content` on each chunk.
- **Retry with backoff** (`post_with_retry`): only HTTP 429 responses are
  retried. It honors the API's `Retry-After` header when present,
  otherwise backs off exponentially (doubling, capped at
  `MAX_BACKOFF_SECONDS`), up to `MAX_RETRIES` attempts, printing each retry
  to stderr. Any other status code (200 or a real error) returns
  immediately with no retry.
- **`.env` loading** (`load_dotenv`): a ~15-line hand-rolled parser, not
  `python-dotenv` — reads `KEY=VALUE` lines and sets `os.environ` without
  overriding a variable already set in the real shell environment.

## Environment quirks this project ran into

These aren't part of the core chatbot logic, but were needed to get it
running reliably on Windows / corporate networks — worth knowing about if
something breaks after an OS or network change:

- **TLS certificate verification** (`truststore.inject_into_ssl()`): on
  networks with a TLS-inspecting corporate proxy, Windows trusts the
  proxy's root certificate but Python's bundled `certifi` CA list doesn't.
  `truststore` makes Python's `ssl` module use the OS trust store instead,
  matching what tools like `curl` already do on Windows.
- **Console encoding** (`sys.stdout.reconfigure(encoding="utf-8")`):
  Windows consoles often default to a legacy codepage (e.g. `cp1252`) that
  can't print every character a model might stream back (curly quotes, em
  dashes). Without this, the process crashes mid-response on those
  characters.
- **Response charset guessing** (`response.encoding = "utf-8"`): Groq's
  `text/event-stream` responses don't declare a charset, so `requests`
  guesses one from partial chunks — which can mangle multi-byte UTF-8
  characters. Forcing `"utf-8"` explicitly fixes this.

If you deploy this somewhere without that proxy/encoding situation (e.g. a
plain Linux CI box), these three lines are harmless no-ops — safe to leave
in.

## Known limitations

- No tool/function calling.
- No mid-conversation system messages (only the one persona at the start).
- History is in-memory only — nothing is saved to disk.
- Rate-limit retry only handles 429; other transient errors (5xx, network
  drops) are not retried.
