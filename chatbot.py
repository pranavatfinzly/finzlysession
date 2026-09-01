#!/usr/bin/env python3
"""
Command-line chatbot that talks to the Groq API  over raw HTTP.

No SDK, no framework. The request body is built by hand,
sent with `requests`, and the streamed response - plain `data: {json}` lines,
not typed SSE events - is parsed line-by-line so the wire format is fully
visible below.
"""

import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles
# often default to a legacy codepage (e.g. cp1252) that can't print every
# character a model might stream back (curly quotes, em dashes, etc.).

import truststore

truststore.inject_into_ssl()  # verify TLS certs against the OS trust store,
# not just the bundled certifi CA list - needed on networks (e.g. corporate
# TLS-inspecting proxies) whose root CA is trusted by Windows but isn't in
# certifi's public CA bundle.

import requests


def load_dotenv(path: str = ".env") -> None:
    """
    Minimal, dependency-free ".env" loader: reads KEY=VALUE lines and sets
    them in os.environ (without overriding a variable already set in the
    real shell environment). No python-dotenv package involved.
    """
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


load_dotenv()

# =============================================================================
# CONFIG - edit freely, effect is immediate on the next message you send.
# =============================================================================

# Named personas. Each value is a full "system" prompt sent with every
# request. Switch between them live with /persona <name> - see main().
PERSONAS = {
    "default": "You are a professional AI assistant. Provide clear, accurate, concise, and easy-to-understand answers. Use simple language, avoid unnecessary jargon, and explain complex concepts in straightforward terms. Stay focused on the user's request and provide practical answers without unnecessary detail.",
    "telegram": "You are a professional AI assistant. Provide clear, accurate, concise, and easy-to-understand answers. Use simple language, avoid unnecessary jargon, and explain complex concepts in straightforward terms. Stay focused on the user's request and provide practical answers without unnecessary detail. You must answer every question using only very short sentences, no more than 8 words each, like a telegram.",
}
DEFAULT_PERSONA = "default"

# The currently active persona name. build_messages() always looks this up
# fresh, so switching it takes effect on the very next request - no restart,
# no cache of an old system prompt.
active_persona = DEFAULT_PERSONA

MODEL = "openai/gpt-oss-120b"
DEFAULT_TEMPERATURE = 1.0
MAX_TOKENS = 4096

API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Retry behavior for HTTP 429 (rate limit) responses only.
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF_SECONDS = 30.0

# =============================================================================


def get_api_key() -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print(
            "Error: the GROQ_API_KEY environment variable is not set.\n"
            "Set it before running this script, e.g.:\n"
            "  export GROQ_API_KEY=gsk_...   (bash)\n"
            "  $env:GROQ_API_KEY='gsk_...'   (PowerShell)",
            file=sys.stderr,
        )
        sys.exit(1)
    return api_key


def build_messages(history: list) -> list:
    """
    Groq's chat completions format has no separate top-level "system" field -
    the system prompt is just another entry in the messages array, and it
    must come first. `history` itself stays plain user/assistant turns; this
    is the one place the system message gets folded in before a request.
    """
    return [{"role": "system", "content": PERSONAS[active_persona]}] + history


def parse_retry_after(response) -> float:
    """
    Groq (like most APIs) sends a Retry-After header on 429 responses with
    the number of seconds to wait. Prefer that over a guessed backoff when
    it's present and parses cleanly.
    """
    retry_after = response.headers.get("retry-after")
    if retry_after is None:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None


def post_with_retry(headers: dict, payload: dict):
    """
    POST to the API, retrying only on HTTP 429 (rate limit) with exponential
    backoff (doubling each attempt, capped at MAX_BACKOFF_SECONDS). Uses the
    server's Retry-After header when present, otherwise the computed
    backoff. Any other status code (200 or a real error) is returned
    immediately - only rate limits are worth waiting out here. Returns the
    final response either way; the caller is responsible for closing it.
    """
    backoff = INITIAL_BACKOFF_SECONDS

    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.post(API_URL, headers=headers, json=payload, stream=True)
        response.encoding = "utf-8"

        if response.status_code != 429 or attempt == MAX_RETRIES:
            return response

        wait_seconds = parse_retry_after(response) or backoff
        print(
            f"\n[rate limited - retry {attempt}/{MAX_RETRIES - 1} "
            f"in {wait_seconds:.1f}s]",
            file=sys.stderr,
        )
        response.close()
        time.sleep(wait_seconds)
        backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SECONDS)

    return response  # unreachable - loop always returns above


def stream_assistant_reply(
    api_key: str, history: list, temperature: float, debug: bool = False
) -> str:
    """
    Send the full conversation history to the chat completions endpoint with
    stream=True, manually parse the streamed lines as they arrive, print
    content deltas as they're received, and return the full assembled
    assistant text so it can be appended to history. When debug=True, each
    chunk is preceded by the time.time() at which it was received.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": temperature,
        "messages": build_messages(history),
        "stream": True,
    }

    # Groq's "text/event-stream" responses don't declare a charset, so
    # requests falls back to guessing the encoding from partial chunks -
    # which mangles multi-byte UTF-8 characters (curly quotes, em dashes).
    # post_with_retry() forces UTF-8 explicitly on every attempt.
    response = post_with_retry(headers, payload)

    if response.status_code != 200:
        print(f"\n[HTTP {response.status_code}] {response.text}", file=sys.stderr)
        response.close()
        return ""

    full_text = ""
    print("Assistant: ", end="", flush=True)

    # Groq streams plain "data: {json}" lines (no "event:" line, no typed
    # event names) separated by blank lines, and signals the end of the
    # stream with a literal "data: [DONE]" line instead of a message_stop
    # event. iter_lines() hands us one raw line at a time.
    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None or raw_line == "":
            continue  # blank line = line separator, nothing to do

        if not raw_line.startswith("data:"):
            continue

        data_str = raw_line[len("data:"):].strip()

        if data_str == "[DONE]":
            break

        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        choices = chunk.get("choices") or []
        if not choices:
            continue

        delta = choices[0].get("delta", {})
        text_piece = delta.get("content")
        if text_piece:
            if debug:
                print(f"\n[chunk received at {time.time()}] ", end="", flush=True)
            full_text += text_piece
            print(text_piece, end="", flush=True)

    print()  # newline after the reply finishes
    response.close()

    return full_text


def print_history(history: list) -> None:
    # build_messages() prepends the system message, matching the exact
    # "messages" array that goes out on the wire (see stream_assistant_reply).
    print("\n--- conversation history sent to the API ---")
    print(json.dumps(build_messages(history), indent=2))
    print("--- end history ---\n")


def print_help() -> None:
    print(
        "\nCommands:\n"
        "  /history       print the full message list as sent to the API\n"
        "  /temp <value>  set temperature for subsequent turns (0.0-1.0)\n"
        "  /reset         clear conversation history (persona/system prompt stays)\n"
        "  /persona       show the active persona and list available personas\n"
        "  /persona <name> switch the active persona (takes effect next message)\n"
        "  /debug on|off  print a timestamp before each streamed chunk's text\n"
        "  /help          show this message\n"
        "  /quit, /exit   leave the chat\n"
    )


def main() -> None:
    global active_persona

    api_key = get_api_key()
    history = []
    temperature = DEFAULT_TEMPERATURE
    debug = False

    print(f"Chatting with {MODEL} (persona: {active_persona}). Type /help for commands.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        if user_input in ("/quit", "/exit"):
            break

        if user_input == "/history":
            print_history(history)
            continue

        if user_input == "/reset":
            history.clear()
            print("Conversation history cleared (persona unchanged).")
            continue

        if user_input == "/help":
            print_help()
            continue

        if user_input == "/persona" or user_input.startswith("/persona "):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 1:
                print(f"\nActive persona: {active_persona}")
                print("Available personas: " + ", ".join(PERSONAS.keys()) + "\n")
            else:
                name = parts[1].strip()
                if name not in PERSONAS:
                    print(
                        f"Unknown persona '{name}'. Available personas: "
                        + ", ".join(PERSONAS.keys())
                    )
                else:
                    active_persona = name
                    print(f"Persona switched to '{name}' (history kept).")
            continue

        if user_input.startswith("/debug"):
            parts = user_input.split()
            if len(parts) != 2 or parts[1] not in ("on", "off"):
                print("Usage: /debug on|off")
                continue
            debug = parts[1] == "on"
            print(f"Debug mode {'enabled' if debug else 'disabled'}")
            continue

        if user_input.startswith("/temp"):
            parts = user_input.split()
            if len(parts) != 2:
                print("Usage: /temp <value between 0.0 and 1.0>")
                continue
            try:
                new_temp = float(parts[1])
            except ValueError:
                print("Temperature must be a number.")
                continue
            if not (0.0 <= new_temp <= 1.0):
                print("Temperature must be between 0.0 and 1.0.")
                continue
            temperature = new_temp
            print(f"Temperature set to {temperature}")
            continue

        # Regular chat turn: append the user message, call the API, append
        # the assistant reply once streaming finishes.
        history.append({"role": "user", "content": user_input})
        assistant_text = stream_assistant_reply(api_key, history, temperature, debug)
        if assistant_text:
            history.append({"role": "assistant", "content": assistant_text})
        else:
            # Request failed - drop the user turn so history stays consistent.
            history.pop()


if __name__ == "__main__":
    main()
