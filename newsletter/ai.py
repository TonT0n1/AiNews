"""
Optional AI editing pass for the newsletter.

Given the stories collected from the feeds, asks Claude to write a short
summary of each, rate its importance, pick its section, and flag stories that
cover the same event. Runs only when ANTHROPIC_API_KEY is set; if the call
fails for any reason the build carries on with the publishers' own text.
"""

import json
import sys

import anthropic

MODEL = "claude-opus-5-5"
EFFORT = "low"            # low | medium | high — higher is more careful and costs more
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


def edit(stories, topics):
    """Return {index: {"summary", "importance", "topic", "duplicate_of"}}, or {} on any failure."""
    payload = [
        {"id": i, "source": s["source"], "headline": s["title"],
         "text": s.get("text", "")[:MAX_TEXT_CHARS]}
        for i, s in enumerate(stories)
    ]
    client = anthropic.Anthropic()
    try:
        try:
            response = _ask(client, payload, topics, with_fallback=True)
        except anthropic.BadRequestError as exc:
            print(f"  AI: retrying without refusal fallback ({exc.message})", file=sys.stderr)
            response = _ask(client, payload, topics, with_fallback=False)
    except anthropic.AuthenticationError:
        print("  AI: the API key was rejected; publishing without AI editing", file=sys.stderr)
        return {}
    except anthropic.RateLimitError:
        print("  AI: rate limited; publishing without AI editing", file=sys.stderr)
        return {}
    except anthropic.APIStatusError as exc:
        print(f"  AI: API error {exc.status_code}; publishing without AI editing", file=sys.stderr)
        return {}
    except anthropic.APIConnectionError:
        print("  AI: could not reach the API; publishing without AI editing", file=sys.stderr)
        return {}

    if response.stop_reason != "end_turn":
        print(f"  AI: stopped early ({response.stop_reason}); publishing without AI editing",
              file=sys.stderr)
        return {}
    try:
        text = next(b.text for b in response.content if b.type == "text")
        rows = json.loads(text)["stories"]
    except (StopIteration, ValueError, KeyError, TypeError):
        print("  AI: unreadable reply; publishing without AI editing", file=sys.stderr)
        return {}

    usage = response.usage
    print(f"  AI: edited {len(rows)} stories with {response.model} "
          f"({usage.input_tokens} tokens in, {usage.output_tokens} out)", file=sys.stderr)
    return {row["id"]: row for row in rows if isinstance(row.get("id"), int)}
