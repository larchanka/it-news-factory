from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from config import AppConfig, load_config

logger = logging.getLogger(__name__)

# Word lists for number conversion
_ONES: list[str] = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]

_TENS: list[str] = [
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
]

_ORDINALS_SPECIAL: dict[str, str] = {
    "1st": "first",
    "2nd": "second",
    "3rd": "third",
    "4th": "fourth",
    "5th": "fifth",
    "6th": "sixth",
    "7th": "seventh",
    "8th": "eighth",
    "9th": "ninth",
    "10th": "tenth",
    "11th": "eleventh",
    "12th": "twelfth",
    "13th": "thirteenth",
    "14th": "fourteenth",
    "15th": "fifteenth",
    "16th": "sixteenth",
    "17th": "seventeenth",
    "18th": "eighteenth",
    "19th": "nineteenth",
    "20th": "twentieth",
    "21st": "twenty-first",
    "22nd": "twenty-second",
    "23rd": "twenty-third",
    "30th": "thirtieth",
    "31st": "thirty-first",
}

_ACRONYMS: dict[str, str] = {
    "AI": "A I",
    "API": "A P I",
    "LLM": "L L M",
    "GPU": "G P U",
    "CPU": "C P U",
    "RAM": "R A M",
    "UI": "U I",
    "UX": "U X",
    "OS": "O S",
    "CLI": "C L I",
    "PR": "P R",
    "SDK": "S D K",
    "URL": "U R L",
    "HTTP": "H T T P",
    "JSON": "J S O N",
    "SQL": "S Q L",
}

# Compiled regex patterns
_RE_MD_LINKS = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RE_MD_FORMATTING = re.compile(r"[*_`~#<>]")
_RE_EMOJI = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f1e0-\U0001f1ff"
    "\U00002702-\U000027b0"
    "\U000024c2-\U0001f251"
    "\U0001f900-\U0001f9ff"
    "\U0001fa70-\U0001faff"
    "\U00002600-\U000026ff"
    "]+",
    flags=re.UNICODE,
)
_RE_SHRUG = re.compile(r"¯\\?_?[^a-zA-Z0-9\s]+_?/¯")
_RE_CURRENCY = re.compile(r"([$€£])\s*(\d+(?:\.\d+)?)\s*([KkMmBbTt])?\b")
_RE_PERCENTAGE = re.compile(r"(-)?(\d+(?:\.\d+)?)\s*%")
_RE_ORDINAL = re.compile(r"\b(\d+)(st|nd|rd|th)\b", flags=re.IGNORECASE)
_RE_DECIMAL = re.compile(r"\b(\d+)\.(\d+)\b")
_RE_INTEGER = re.compile(r"\b\d+\b")


def _convert_two_digits(n: int) -> str:
    """Convert an integer from 0 to 99 into words."""
    if n < 20:
        return _ONES[n]
    tens_word = _TENS[n // 10]
    rem = n % 10
    if rem == 0:
        return tens_word
    return f"{tens_word}-{_ONES[rem]}"


def _convert_three_digits(n: int) -> str:
    """Convert an integer from 0 to 999 into words."""
    hundreds = n // 100
    rem = n % 100
    if hundreds == 0:
        return _convert_two_digits(rem)
    hundreds_word = f"{_ONES[hundreds]} hundred"
    if rem == 0:
        return hundreds_word
    return f"{hundreds_word} {_convert_two_digits(rem)}"


def number_to_words(n: int) -> str:
    """Convert an integer up to trillions into plain English words."""
    if n < 0:
        return f"negative {number_to_words(abs(n))}"
    if n < 100:
        return _convert_two_digits(n)
    if n < 1000:
        return _convert_three_digits(n)

    scales: list[tuple[int, str]] = [
        (1_000_000_000_000, "trillion"),
        (1_000_000_000, "billion"),
        (1_000_000, "million"),
        (1_000, "thousand"),
    ]

    for scale, name in scales:
        if n >= scale:
            head = n // scale
            tail = n % scale
            head_word = f"{number_to_words(head)} {name}"
            if tail == 0:
                return head_word
            return f"{head_word} {number_to_words(tail)}"

    return _convert_three_digits(n)


def _year_to_words(year: int) -> str:
    """Convert 4-digit year into conversational English words."""
    if 1000 <= year <= 2099:
        if year % 100 == 0:
            return number_to_words(year)
        century = year // 100
        decade = year % 100
        century_word = _convert_two_digits(century)
        if decade < 10:
            return f"{century_word} oh {_ONES[decade]}"
        return f"{century_word} {_convert_two_digits(decade)}"
    return number_to_words(year)


def _expand_currency_unit(suffix: str) -> tuple[int, str]:
    """Map currency multiplier suffixes to numeric multiplier and spoken unit."""
    mapping = {
        "K": (1_000, "thousand"),
        "M": (1_000_000, "million"),
        "B": (1_000_000_000, "billion"),
        "T": (1_000_000_000_000, "trillion"),
    }
    return mapping.get(suffix.upper(), (1, ""))


def count_words(text: str) -> int:
    """Count words in a text block, ignoring extraneous whitespace and punctuation."""
    tokens = [tok for tok in re.split(r"\s+", text.strip()) if tok]
    return len(tokens)


def _replace_currency(m: re.Match[str]) -> str:
    symbol, val_str, unit_char = m.group(1), m.group(2), m.group(3) or ""
    currency_name = {"$": "dollars", "€": "euros", "£": "pounds"}.get(symbol, "dollars")
    _, mult_word = _expand_currency_unit(unit_char)

    if "." in val_str:
        parts = val_str.split(".")
        int_part = int(parts[0])
        dec_words = " ".join(number_to_words(int(d)) for d in parts[1])
        dec_str = f"{number_to_words(int_part)} point {dec_words}"
        return (
            f"{dec_str} {mult_word} {currency_name}" if mult_word else f"{dec_str} {currency_name}"
        )

    num_int = int(val_str)
    if mult_word:
        return f"{number_to_words(num_int)} {mult_word} {currency_name}"
    return f"{number_to_words(num_int)} {currency_name}"


def _replace_percentage(m: re.Match[str]) -> str:
    sign, val = m.group(1), m.group(2)
    prefix = "negative " if sign == "-" else ""
    if "." in val:
        parts = val.split(".")
        dec_words = " ".join(number_to_words(int(d)) for d in parts[1])
        val_word = f"{number_to_words(int(parts[0]))} point {dec_words}"
    else:
        val_word = number_to_words(int(val))
    return f"{prefix}{val_word} percent"


_IRREGULAR_ORDINALS: dict[str, str] = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}


def _word_to_ordinal(word: str) -> str:
    """Convert a single cardinal word into its ordinal counterpart."""
    if word in _IRREGULAR_ORDINALS:
        return _IRREGULAR_ORDINALS[word]
    if word.endswith("y"):
        return f"{word[:-1]}ieth"
    return f"{word}th"


def _cardinal_to_ordinal(cardinal: str) -> str:
    """Convert a full cardinal number phrase into an ordinal phrase."""
    if "-" in cardinal:
        prefix, last_part = cardinal.rsplit("-", 1)
        return f"{prefix}-{_word_to_ordinal(last_part)}"
    if " " in cardinal:
        prefix, last_part = cardinal.rsplit(" ", 1)
        return f"{prefix} {_word_to_ordinal(last_part)}"
    return _word_to_ordinal(cardinal)


def _replace_ordinal(m: re.Match[str]) -> str:
    key = m.group(0).lower()
    if key in _ORDINALS_SPECIAL:
        return _ORDINALS_SPECIAL[key]
    num = int(m.group(1))
    return _cardinal_to_ordinal(number_to_words(num))


def _replace_decimal(m: re.Match[str]) -> str:
    int_part, dec_part = m.group(1), m.group(2)
    dec_words = " ".join(number_to_words(int(d)) for d in dec_part)
    return f"{number_to_words(int(int_part))} point {dec_words}"


def _replace_year_or_number(m: re.Match[str]) -> str:
    num_str = m.group(0)
    num = int(num_str)
    if len(num_str) == 4 and 1900 <= num <= 2099:
        return _year_to_words(num)
    return number_to_words(num)


def sanitize_for_tts(text: str) -> str:
    """Sanitize raw LLM or article text for high-quality TTS audio synthesis."""
    text = _RE_MD_LINKS.sub(r"\1", text)
    text = _RE_MD_FORMATTING.sub(" ", text)
    text = _RE_SHRUG.sub(" ", text)
    text = _RE_EMOJI.sub(" ", text)
    text = _RE_CURRENCY.sub(_replace_currency, text)
    text = _RE_PERCENTAGE.sub(_replace_percentage, text)
    text = _RE_ORDINAL.sub(_replace_ordinal, text)
    text = _RE_DECIMAL.sub(_replace_decimal, text)
    # Strip thousands separators (e.g. 50,000 -> 50000, 1,500 -> 1500)
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    text = _RE_INTEGER.sub(_replace_year_or_number, text)

    for acr, expansion in _ACRONYMS.items():
        text = re.sub(rf"\b{acr}\b", expansion, text)

    text = re.sub(r"[^\w\s.,!?:;'-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class ScriptPayload:
    """Structured representation of a synthesized video script and metadata."""

    hook: str
    script: str
    yt_title: str
    yt_desc: str
    body: str = ""
    punchline: str = ""

    @property
    def title(self) -> str:
        """Alias for yt_title."""
        return self.yt_title

    @property
    def full_script(self) -> str:
        """Alias for script."""
        return self.script

    @property
    def word_count(self) -> int:
        """Total word count of the spoken voiceover script."""
        return count_words(self.script)


def _extract_script_text(data: dict[str, Any], hook: str, body: str, punchline: str) -> str:
    """Extract voiceover script string from data, assembling segments if missing."""
    raw = str(data.get("script") or data.get("full_script") or "").strip()
    if raw:
        return raw
    parts = [p for p in [hook, body, punchline] if p]
    return " ".join(parts)


def _normalize_yt_title(raw_title: Any) -> str:
    """Ensure YouTube title has required hashtag."""
    title = str(raw_title or "Tech News Alert").strip()
    return title if "#Shorts" in title else f"{title} #Shorts"


def _normalize_yt_desc(raw_desc: Any, hook: str, default_source_url: str = "") -> str:
    """Ensure YouTube description has required hashtags and source link."""
    desc = str(raw_desc or "").strip()
    if not desc:
        desc = f"{hook} #Shorts #TechNews"
    else:
        if "#Shorts" not in desc:
            desc = f"{desc} #Shorts"
        if "#TechNews" not in desc:
            desc = f"{desc} #TechNews"

    if default_source_url and default_source_url not in desc:
        desc = f"{desc}\nSource: {default_source_url}"
    return desc


def parse_script_payload(
    raw_data: str | dict[str, Any], default_source_url: str = ""
) -> ScriptPayload:
    """Parse and normalize raw JSON or dictionary payload into a validated ScriptPayload."""
    if isinstance(raw_data, str):
        cleaned = raw_data.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        data = json.loads(cleaned)
    else:
        data = dict(raw_data)
    hook = str(data.get("hook", "")).strip()
    body = str(data.get("body", "")).strip()
    punchline = str(data.get("punchline", "")).strip()

    raw_script = _extract_script_text(data, hook, body, punchline)
    yt_title = _normalize_yt_title(data.get("yt_title") or data.get("title"))
    yt_desc = _normalize_yt_desc(
        data.get("yt_desc") or data.get("description"), hook, default_source_url
    )

    return ScriptPayload(
        hook=sanitize_for_tts(hook),
        script=sanitize_for_tts(raw_script),
        yt_title=yt_title,
        yt_desc=yt_desc,
        body=sanitize_for_tts(body) if body else "",
        punchline=sanitize_for_tts(punchline) if punchline else "",
    )


def validate_script_payload(
    payload: ScriptPayload, min_words: int = 85, max_words: int = 115
) -> tuple[bool, str]:
    """Validate that the generated script conforms to YouTube Shorts duration bounds."""
    count = payload.word_count
    if count < min_words:
        return False, f"Script is too short ({count} words, minimum is {min_words})"
    if count > max_words:
        return False, f"Script is too long ({count} words, maximum is {max_words})"
    return True, "ok"


def build_system_prompt() -> str:
    """Construct the system prompt enforcing cynical tone, Shorts structure, and word count."""
    return (
        "You are a cynical, tech-savvy software engineer and viral YouTube Shorts scriptwriter. "
        "Your mission is to turn tech news, engineering disasters, and startup announcements "
        "into sharp, sarcastic, high-retention 35-to-40 second video scripts for developers.\n\n"
        "Guidelines:\n"
        "1. Tone: Cynical, humorous, realistic, engineering-insider perspective. No corporate fluff.\n"
        "2. Structure:\n"
        "   - Hook (0-3s): One punchy, provocative opening sentence that stops viewers scrolling.\n"
        "   - Body / Meat (3-30s): Three to four sentences covering the actual technical facts, architecture, or failure.\n"
        "   - Punchline (30-40s): One sarcastic closing takeaway or warning for engineers.\n"
        "3. WORD COUNT CONSTRAINT: The total voiceover script MUST be strictly between 85 and 115 words.\n"
        "4. TTS Phonetic Rules:\n"
        "   - No markdown formatting (no asterisks, hashes, backticks, brackets).\n"
        "   - No emojis.\n"
        "   - Spell out all numbers, currencies, and percentages (e.g. 'forty-two million dollars', 'three point five percent').\n"
        "   - Spell out tech acronyms with spaces (e.g. 'A I', 'A P I', 'G P U', 'L L M').\n"
        "5. Output format: Return ONLY valid JSON with no markdown wrapping matching this schema:\n"
        "{\n"
        '  "yt_title": "Catchy YouTube title under 60 chars ending with #Shorts",\n'
        '  "hook": "Opening hook sentence",\n'
        '  "script": "Full continuous narration (85 to 115 words total)",\n'
        '  "yt_desc": "Two sentence description plus #Shorts #TechNews and source link"\n'
        "}"
    )


def build_user_prompt(title: str, article_text: str) -> str:
    """Format user prompt combining article headline and content."""
    headline_part = f"Headline: {title}\n" if title else ""
    return (
        f"{headline_part}"
        f"Article Content:\n{article_text.strip()}\n\n"
        "Write the YouTube Shorts script in JSON according to system instructions. "
        "Remember: the voiceover narration MUST contain between 85 and 115 words."
    )


def _extract_text_and_meta(content: Any, title: str | None) -> tuple[str, str, str]:
    """Extract article text, headline, and source URL from content or ScrapedContent."""
    if hasattr(content, "text"):
        article_text = str(getattr(content, "text", ""))
        article_title = str(title or getattr(content, "title", ""))
        source_url = str(getattr(content, "url", ""))
    else:
        article_text = str(content)
        article_title = str(title or "")
        source_url = ""
    return article_text, article_title, source_url


def _extract_response_content(response: Any) -> str:
    """Safely extract message content from Ollama ChatResponse, GenerateResponse, or dict."""
    if isinstance(response, dict):
        if "message" in response:
            msg = response["message"]
            return str(msg.get("content", msg) if isinstance(msg, dict) else msg)
        return str(response.get("response", response))

    msg = getattr(response, "message", None)
    if msg is not None:
        if isinstance(msg, dict):
            return str(msg.get("content", msg))
        return str(getattr(msg, "content", msg))

    return str(getattr(response, "response", response))


def _trim_script_to_bounds(payload: ScriptPayload, max_words: int = 115) -> ScriptPayload:
    """Trim a slightly over-budget script down to max_words cleanly at word boundaries."""
    words = payload.script.split()
    if len(words) <= max_words:
        return payload

    trimmed_script = " ".join(words[:max_words]).rstrip(".,;:-") + "."
    return ScriptPayload(
        hook=payload.hook,
        script=trimmed_script,
        yt_title=payload.yt_title,
        yt_desc=payload.yt_desc,
        body=payload.body,
        punchline=payload.punchline,
    )


@dataclass
class _AttemptResult:
    payload: ScriptPayload | None = None
    is_valid: bool = False
    feedback_message: str | None = None


def _evaluate_attempt(raw_content: str, source_url: str) -> _AttemptResult:
    """Evaluate a single raw response from Ollama, returning parsed payload and validation feedback."""
    try:
        payload = parse_script_payload(raw_content, default_source_url=source_url)
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        return _AttemptResult(
            feedback_message=(
                "Your output was not valid JSON. Return ONLY valid JSON matching the schema."
            )
        )

    is_valid, reason = validate_script_payload(payload)
    if is_valid:
        return _AttemptResult(payload=payload, is_valid=True)

    direction = "shorter" if payload.word_count > 115 else "longer"
    msg = (
        f"Your previous script had {payload.word_count} words ({reason}). "
        f"Rewrite the script {direction} so the narration is strictly between "
        "85 and 115 words while preserving the exact JSON schema."
    )
    return _AttemptResult(payload=payload, is_valid=False, feedback_message=msg)


def _record_attempt_feedback(
    messages: list[dict[str, str]],
    raw_content: str,
    feedback_message: str | None,
    attempt: int,
    max_retries: int,
) -> None:
    """Append feedback to conversational messages for the next attempt."""
    if attempt < max_retries - 1 and feedback_message:
        messages.append({"role": "assistant", "content": raw_content})
        messages.append({"role": "user", "content": feedback_message})


def _execute_attempt_step(
    raw_content: str,
    source_url: str,
    messages: list[dict[str, str]],
    attempt: int,
    max_retries: int,
) -> tuple[ScriptPayload | None, ScriptPayload | None]:
    """Process Ollama response for one attempt. Returns (valid_payload, latest_payload)."""
    result = _evaluate_attempt(raw_content, source_url)
    if result.is_valid and result.payload is not None:
        return result.payload, result.payload
    _record_attempt_feedback(messages, raw_content, result.feedback_message, attempt, max_retries)
    return None, result.payload


def _resolve_final_payload(last_payload: ScriptPayload | None) -> ScriptPayload:
    """Return trimmed payload if available, or raise error if generation failed."""
    if last_payload is not None:
        return _trim_script_to_bounds(last_payload)
    raise RuntimeError("Failed to generate a valid script from Ollama after retries.")


def _get_sync_client(client: Any, host: str) -> Any:
    if client is not None:
        return client
    import ollama

    return ollama.Client(host=host)


def _get_async_client(client: Any, host: str) -> Any:
    if client is not None:
        return client
    import ollama

    return ollama.AsyncClient(host=host)


def generate_script(
    article_text: Any,
    title: str | None = None,
    config: AppConfig | None = None,
    model: str | None = None,
    client: Any = None,
    max_retries: int = 3,
) -> ScriptPayload:
    """Generate a synthesized, phonetically sanitized Shorts script via Ollama."""
    cfg = config or load_config()
    target_model = model or cfg.ollama.model
    active_client = _get_sync_client(client, cfg.ollama.host)

    text, headline, source_url = _extract_text_and_meta(article_text, title)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": build_user_prompt(headline, text)},
    ]

    last_payload: ScriptPayload | None = None

    for attempt in range(max_retries):
        try:
            response = active_client.chat(
                model=target_model,
                messages=messages,
                format="json",
                options={"temperature": cfg.ollama.temperature},
            )
            raw_content = _extract_response_content(response)
        except Exception as exc:
            logger.warning("Ollama chat invocation failed on attempt %d: %s", attempt + 1, exc)
            if attempt == max_retries - 1:
                raise
            continue

        valid_payload, maybe_payload = _execute_attempt_step(
            raw_content, source_url, messages, attempt, max_retries
        )
        if valid_payload is not None:
            return valid_payload
        if maybe_payload is not None:
            last_payload = maybe_payload

    return _resolve_final_payload(last_payload)


async def async_generate_script(
    article_text: Any,
    title: str | None = None,
    config: AppConfig | None = None,
    model: str | None = None,
    client: Any = None,
    max_retries: int = 3,
) -> ScriptPayload:
    """Asynchronously generate a synthesized, phonetically sanitized Shorts script via Ollama."""
    cfg = config or load_config()
    target_model = model or cfg.ollama.model
    active_client = _get_async_client(client, cfg.ollama.host)

    text, headline, source_url = _extract_text_and_meta(article_text, title)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": build_user_prompt(headline, text)},
    ]

    last_payload: ScriptPayload | None = None

    for attempt in range(max_retries):
        try:
            response = await active_client.chat(
                model=target_model,
                messages=messages,
                format="json",
                options={"temperature": cfg.ollama.temperature},
            )
            raw_content = _extract_response_content(response)
        except Exception as exc:
            logger.warning("Ollama async chat failed on attempt %d: %s", attempt + 1, exc)
            if attempt == max_retries - 1:
                raise
            continue

        valid_payload, maybe_payload = _execute_attempt_step(
            raw_content, source_url, messages, attempt, max_retries
        )
        if valid_payload is not None:
            return valid_payload
        if maybe_payload is not None:
            last_payload = maybe_payload

    return _resolve_final_payload(last_payload)
