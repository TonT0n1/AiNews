"""
Optional AI editing pass for the newsletter.

Given the stories collected from the feeds, asks a language model to write a
short summary of each, rate its importance, pick its section, and flag stories
that cover the same event. Uses OpenAI when OPENAI_API_KEY is set, otherwise
Claude when ANTHROPIC_API_KEY is set. If the call fails for any reason the
build carries on with the publishers' own text.
"""

import json
import os
import sys

# A light OpenAI model by default; set the OPENAI_MODEL variable to use another.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL") or "gpt-5.4-mini"
MODEL = "claude-opus-5-5"
EFFORT = "low"            # Claude only: low | medium | high
MAX_TEXT_CHARS = 1200     # publisher text sent per story

SYSTEM = """You are the editor of a twice-daily AI news page read by people who follow the AI \
industry. You receive the stories gathered from publishers' RSS feeds for this edition, as a JSON \
list. Each has an id, the source, the headline, and whatever text the publisher put in its feed \
(often just the opening lines of the article, sometimes nothing).

For every story, return:

- summary: two or three plain sentences telling the reader what happened and why it matters. \
Readers will take this as an accurate account of the article, and you can only see the feed text, \
not the article itself, so use only what the headline and text actually say. Do not add \
background, numbers, names or outcomes from your own knowledge, even if you are confident of \
them. When the text is thin, a single sentence that restates the headline clearly is the right \
answer. Write in a neutral news voice, without hype and without phrases like "the article says".

- importance: an integer from 1 to 5 for how much this matters to someone tracking AI. 5 is a \
story most readers would want to know about today (a major model release, significant \
regulation or court ruling, a very large deal, a serious safety incident). 3 is solid, ordinary \
news. 1 is minor, promotional, or only loosely about AI. Use the whole range; most editions have \
only a few 5s.

- topic: the section it belongs in, chosen from the allowed values by what the story is mainly \
about rather than by keywords in the headline.

- duplicate_of: when several stories report the same event (the same announcement, ruling, deal \
or release), pick the one with the fullest text as the main story and give its id here for each \
of the others. Use -1 for a story that stands alone, and for the main story itself. Stories that \
merely share a company or theme are not duplicates.

Return one entry for every id you were given."""


def _schema(topics):
    return {
        "type": "object",
        "properties": {
            "stories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "summary": {"type": "string"},
                        "importance": {"type": "integer"},
                        "topic": {"type": "string", "enum": topics},
                        "duplicate_of": {"type": "integer"},
                    },
                    "required": ["id", "summary", "importance", "topic", "duplicate_of"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["stories"],
        "additionalProperties": False,
    }


def _ask(client, payload, topics, with_fallback):
    extra = {}
    if with_fallback:
        # If a safety classifier declines the request (news about cyber or bio topics can
        # trip one), the API re-runs it on a fallback model inside the same call.
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    return client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": _schema(topics)},
        },
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        **extra,
    )


def _edit_claude(payload, topics):
    import anthropic
    client = anthropic.Anthropic()
    try:
        try:
            response = _ask(client, payload, topics, with_fallback=True)
        except anthropic.BadRequestError as exc:
            print(f"  AI: retrying without refusal fallback ({exc.message})", file=sys.stderr)
            response = _ask(client, payload, topics, with_fallback=False)
    except anthropic.AuthenticationError:
        return _skip("the Anthropic API key was rejected")
    except anthropic.RateLimitError:
        return _skip("rate limited")
    except anthropic.APIStatusError as exc:
        return _skip(f"API error {exc.status_code}")
    except anthropic.APIConnectionError:
        return _skip("could not reach the API")
    if response.stop_reason != "end_turn":
        return _skip(f"stopped early ({response.stop_reason})")
    text = next((b.text for b in response.content if b.type == "text"), "")
    usage = response.usage
    return text, response.model, usage.input_tokens, usage.output_tokens


def _edit_openai(payload, topics):
    import openai
    client = openai.OpenAI()
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "edition", "strict": True, "schema": _schema(topics)},
            },
        )
    except openai.AuthenticationError:
        return _skip("the OpenAI API key was rejected")
    except openai.NotFoundError:
        return _skip(f"OpenAI has no model called {OPENAI_MODEL!r}; set OPENAI_MODEL to a current one")
    except openai.RateLimitError:
        return _skip("rate limited, or the OpenAI account has no credit")
    except openai.APIStatusError as exc:
        return _skip(f"API error {exc.status_code}: {exc.message}")
    except openai.APIConnectionError:
        return _skip("could not reach the API")
    choice = response.choices[0]
    if choice.finish_reason != "stop" or getattr(choice.message, "refusal", None):
        return _skip(f"stopped early ({choice.finish_reason})")
    usage = response.usage
    return choice.message.content or "", response.model, usage.prompt_tokens, usage.completion_tokens


def _skip(reason):
    print(f"  AI: {reason}; publishing without AI editing", file=sys.stderr)
    return None


def edit(stories, topics):
    """Return {index: {"summary", "importance", "topic", "duplicate_of"}}, or {} on any failure."""
    payload = [
        {"id": i, "source": s["source"], "headline": s["title"],
         "text": s.get("text", "")[:MAX_TEXT_CHARS]}
        for i, s in enumerate(stories)
    ]
    ask = _edit_openai if os.environ.get("OPENAI_API_KEY") else _edit_claude
    result = ask(payload, topics)
    if not result:
        return {}
    text, model, tokens_in, tokens_out = result
    try:
        rows = json.loads(text)["stories"]
    except (ValueError, KeyError, TypeError):
        _skip("unreadable reply")
        return {}
    print(f"  AI: edited {len(rows)} stories with {model} "
          f"({tokens_in} tokens in, {tokens_out} out)", file=sys.stderr)
    return {row["id"]: row for row in rows if isinstance(row.get("id"), int)}
