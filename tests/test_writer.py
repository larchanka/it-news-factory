from __future__ import annotations

import pytest

from writer import (
    ScriptPayload,
    async_generate_script,
    build_system_prompt,
    build_user_prompt,
    count_words,
    generate_script,
    number_to_words,
    parse_script_payload,
    sanitize_for_tts,
    validate_script_payload,
)


class TestPhoneticSanitizer:
    def test_strip_markdown(self) -> None:
        raw = "This is **bold**, *italic*, `code`, and #heading with [a link](https://example.com) and ~~strike~~."
        result = sanitize_for_tts(raw)
        assert "*" not in result
        assert "`" not in result
        assert "#" not in result
        assert "[" not in result
        assert "]" not in result
        assert "(" not in result
        assert ")" not in result
        assert "~" not in result

    def test_strip_emojis_and_special_symbols(self) -> None:
        raw = "Robots are taking over! 🤖💥🚀 Welcome to the future... ¯\\_(ツ)_/¯"
        result = sanitize_for_tts(raw)
        assert "🤖" not in result
        assert "💥" not in result
        assert "🚀" not in result
        assert "Robots are taking over" in result
        assert "Welcome to the future" in result

    def test_number_to_words_integers(self) -> None:
        assert number_to_words(0) == "zero"
        assert number_to_words(5) == "five"
        assert number_to_words(13) == "thirteen"
        assert number_to_words(42) == "forty-two"
        assert number_to_words(100) == "one hundred"
        assert number_to_words(105) == "one hundred five"
        assert number_to_words(1000) == "one thousand"
        assert number_to_words(1000000) == "one million"
        assert number_to_words(1000000000) == "one billion"
        assert number_to_words(1000000000000) == "one trillion"
        assert number_to_words(-42) == "negative forty-two"

    def test_expand_currencies(self) -> None:
        raw = "The company raised $42M, invested €20M, borrowed £5B, lost $500K, and now has $10 left with $3.5B in debt, plus $3.25M in cash and $3.75M in reserves."
        result = sanitize_for_tts(raw)
        assert "forty-two million dollars" in result
        assert "twenty million euros" in result
        assert "five billion pounds" in result
        assert "five hundred thousand dollars" in result
        assert "ten dollars" in result
        assert "three point five billion dollars" in result
        assert "three point two five million dollars" in result
        assert "three point seven five million dollars" in result
        assert "$" not in result
        assert "€" not in result
        assert "£" not in result

    def test_expand_percentages_and_decimals(self) -> None:
        raw = "Latency spiked by 3.5%, revenue dropped by -5%, and CPU reached 50%."
        result = sanitize_for_tts(raw)
        assert "three point five percent" in result
        assert "negative five percent" in result
        assert "fifty percent" in result
        assert "%" not in result

    def test_expand_ordinals(self) -> None:
        raw = "This is their 1st failure, 2nd warning, 3rd strike, 4th attempt, 21st century, 42nd bug, 53rd release, and 101st user."
        result = sanitize_for_tts(raw)
        assert "first failure" in result
        assert "second warning" in result
        assert "third strike" in result
        assert "fourth attempt" in result
        assert "twenty-first century" in result
        assert "forty-second bug" in result
        assert "fifty-third release" in result
        assert "one hundred first user" in result

    def test_expand_comma_separated_numbers(self) -> None:
        raw = "The database handles 50,000 users and 1,500 requests per second."
        result = sanitize_for_tts(raw)
        assert "fifty thousand users" in result
        assert "one thousand five hundred requests" in result

    def test_expand_years(self) -> None:
        raw = "Founded in 2024, milestone in 2000, boom in 2005, and operating since 1999."
        result = sanitize_for_tts(raw)
        assert "twenty twenty-four" in result
        assert "two thousand" in result
        assert "twenty oh five" in result
        assert "nineteen ninety-nine" in result

    def test_expand_tech_acronyms(self) -> None:
        raw = "Our new AI uses an LLM connected to an API on the GPU."
        result = sanitize_for_tts(raw)
        assert "A I" in result
        assert "L L M" in result
        assert "A P I" in result
        assert "G P U" in result

    def test_count_words_standard(self) -> None:
        text = "This is a test sentence with eight words."
        assert count_words(text) == 8


class TestScriptPayload:
    def test_parse_schema_modules_md(self) -> None:
        raw = {
            "hook": "Your code is about to break.",
            "script": "Your code is about to break because another tech giant decided to rewrite their backend in Rust. "
            * 5,
            "yt_title": "Why Rust Broke Your App #Shorts",
            "yt_desc": "Tech news summary. #Shorts #TechNews https://example.com",
        }
        payload = parse_script_payload(raw)
        assert payload.hook == "Your code is about to break."
        assert "because another tech giant" in payload.script
        assert payload.yt_title.endswith("#Shorts")
        assert payload.title == payload.yt_title
        assert payload.full_script == payload.script

    def test_parse_schema_alternative_keys(self) -> None:
        raw_json = """{
            "title": "AI Bubble Warning #Shorts",
            "hook": "Here comes another billionaire prediction.",
            "body": "Every tech startup is burning millions on GPUs.",
            "punchline": "Have fun paying for API credits.",
            "full_script": "Here comes another billionaire prediction. Every tech startup is burning millions on GPUs. Have fun paying for API credits."
        }"""
        payload = parse_script_payload(raw_json)
        assert payload.yt_title == "AI Bubble Warning #Shorts"
        assert payload.hook == "Here comes another billionaire prediction."
        assert "burning millions" in payload.body
        assert payload.punchline == "Have fun paying for A P I credits."
        assert "Every tech startup" in payload.script

    def test_parse_assembles_script_if_full_script_missing(self) -> None:
        raw = {
            "title": "Silicon Valley Drama #Shorts",
            "hook": "Startup pivots again.",
            "body": "They threw away two years of work.",
            "punchline": "Investors applauded enthusiastically.",
        }
        payload = parse_script_payload(raw)
        assert "Startup pivots again" in payload.script
        assert "two years of work" in payload.script
        assert "Investors applauded enthusiastically" in payload.script

    def test_parse_strips_markdown_code_fences(self) -> None:
        fenced_json = """```json
        {
            "yt_title": "Fenced JSON Response #Shorts",
            "hook": "LLM wrapped in markdown fences.",
            "script": "Technology moves fast and breaking production on Friday is an ancient tradition. Technology moves fast and breaking production on Friday is an ancient tradition. Technology moves fast and breaking production on Friday is an ancient tradition. Technology moves fast and breaking production on Friday is an ancient tradition. Technology moves fast and breaking production on Friday is an ancient tradition. Technology moves fast and breaking production on Friday is an ancient tradition. Technology moves fast and breaking production on Friday is an ancient tradition. Technology moves fast and breaking production on Friday is an ancient",
            "yt_desc": "Fenced payload summary #Shorts #TechNews"
        }
        ```"""
        payload = parse_script_payload(fenced_json)
        assert payload.yt_title == "Fenced JSON Response #Shorts"
        assert payload.hook == "L L M wrapped in markdown fences."
        assert payload.word_count >= 85

    def test_validate_script_payload_bounds(self) -> None:
        # 100 words script
        valid_words = ["word"] * 100
        valid_text = " ".join(valid_words)
        payload = ScriptPayload(
            hook="word word word",
            script=valid_text,
            yt_title="Test Title #Shorts",
            yt_desc="Desc #Shorts",
        )
        is_valid, reason = validate_script_payload(payload)
        assert is_valid
        assert reason == "ok"
        assert payload.word_count == 100

        # Too short (< 85 words)
        short_words = ["word"] * 70
        short_payload = ScriptPayload(
            hook="word",
            script=" ".join(short_words),
            yt_title="Test #Shorts",
            yt_desc="Desc",
        )
        is_valid, reason = validate_script_payload(short_payload)
        assert not is_valid
        assert "too short" in reason.lower()

        # Too long (> 115 words)
        long_words = ["word"] * 130
        long_payload = ScriptPayload(
            hook="word",
            script=" ".join(long_words),
            yt_title="Test #Shorts",
            yt_desc="Desc",
        )
        is_valid, reason = validate_script_payload(long_payload)
        assert not is_valid
        assert "too long" in reason.lower()


class TestPromptBuilder:
    def test_build_system_prompt_contains_constraints(self) -> None:
        prompt = build_system_prompt()
        assert "cynical" in prompt.lower() or "sarcastic" in prompt.lower()
        assert "85" in prompt
        assert "115" in prompt
        assert "hook" in prompt.lower()
        assert "punchline" in prompt.lower()
        assert "json" in prompt.lower()

    def test_build_user_prompt(self) -> None:
        prompt = build_user_prompt(
            "New AI Framework Released", "A new framework claims 10x faster inference."
        )
        assert "New AI Framework Released" in prompt
        assert "10x faster inference" in prompt


def make_script_of_length(words: int) -> str:
    base = "Technology moves fast and breaking production on Friday is an ancient tradition. "
    tokens = (base * ((words // 10) + 2)).split()[:words]
    return " ".join(tokens)


class TestOllamaGenerator:
    def test_generate_script_success_first_attempt(self) -> None:
        valid_script = make_script_of_length(95)
        assert 85 <= count_words(valid_script) <= 115

        mock_response = {
            "message": {
                "content": f'{{"yt_title": "Monolith Strikes Back #Shorts", "hook": "Look at this breakthrough.", "script": "{valid_script}", "yt_desc": "Monoliths are back. #Shorts #TechNews"}}'
            }
        }

        class MockClient:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def chat(self, **kwargs):
                self.calls.append(kwargs)
                return type("ChatResponse", (), mock_response)()

        mock_client = MockClient()
        payload = generate_script(
            article_text="Monoliths are returning to favor across major tech companies.",
            title="Monolith Revival",
            client=mock_client,
        )

        assert payload.yt_title == "Monolith Strikes Back #Shorts"
        assert count_words(payload.script) >= 85
        assert len(mock_client.calls) == 1
        assert mock_client.calls[0]["format"] == "json"

    def test_generate_script_retries_on_word_count_and_succeeds(self) -> None:
        # First attempt: 130 words (too long)
        too_long = make_script_of_length(130)
        assert count_words(too_long) > 115

        # Second attempt: 95 words (valid)
        valid = make_script_of_length(95)
        assert 85 <= count_words(valid) <= 115

        responses = [
            type(
                "ChatResponse",
                (),
                {
                    "message": {
                        "content": f'{{"yt_title": "Long Story #Shorts", "hook": "Too long.", "script": "{too_long}", "yt_desc": "Desc #Shorts"}}'
                    }
                },
            )(),
            type(
                "ChatResponse",
                (),
                {
                    "message": {
                        "content": f'{{"yt_title": "Refactoring Disaster #Shorts", "hook": "Here is the real story.", "script": "{valid}", "yt_desc": "Desc #Shorts"}}'
                    }
                },
            )(),
        ]

        class MockClient:
            def __init__(self) -> None:
                self.call_count = 0
                self.recorded_messages: list = []

            def chat(self, **kwargs):
                self.recorded_messages.append(kwargs.get("messages", []))
                resp = responses[self.call_count]
                self.call_count += 1
                return resp

        client = MockClient()
        payload = generate_script("Some tech article", client=client, max_retries=3)

        assert client.call_count == 2
        assert payload.yt_title == "Refactoring Disaster #Shorts"
        assert 85 <= count_words(payload.script) <= 115
        # Verify feedback message was passed in second call
        second_call_messages = client.recorded_messages[1]
        assert any("too long" in str(m.get("content", "")).lower() for m in second_call_messages)

    def test_generate_script_retries_on_json_error(self) -> None:
        valid_script = make_script_of_length(95)
        assert 85 <= count_words(valid_script) <= 115

        responses = [
            type("ChatResponse", (), {"message": {"content": "Not valid json {"}})(),
            type(
                "ChatResponse",
                (),
                {
                    "message": {
                        "content": f'{{"yt_title": "Cloud Tax #Shorts", "hook": "Just when you thought it was safe.", "script": "{valid_script}", "yt_desc": "Desc #Shorts"}}'
                    }
                },
            )(),
        ]

        class MockClient:
            def __init__(self) -> None:
                self.call_count = 0

            def chat(self, **kwargs):
                resp = responses[self.call_count]
                self.call_count += 1
                return resp

        client = MockClient()
        payload = generate_script("Cloud pricing update", client=client, max_retries=2)
        assert client.call_count == 2
        assert payload.yt_title == "Cloud Tax #Shorts"

    def test_generate_script_with_scraped_content_object(self) -> None:
        from scraper import ScrapedContent

        valid_script = make_script_of_length(95)
        assert 85 <= count_words(valid_script) <= 115

        content = ScrapedContent(
            url="https://example.com/incident",
            title="Outage Postmortem",
            text="The team accidentally flushed Redis.",
            image_paths=["/tmp/img1.jpg"],
        )

        class MockClient:
            def chat(self, **kwargs):
                return type(
                    "ChatResponse",
                    (),
                    {
                        "message": {
                            "content": f'{{"yt_title": "Redis Outage #Shorts", "hook": "Here is what happened.", "script": "{valid_script}", "yt_desc": "Postmortem #Shorts"}}'
                        }
                    },
                )()

        payload = generate_script(content, client=MockClient())
        assert payload.yt_title == "Redis Outage #Shorts"
        assert "incident" in payload.yt_desc or "example.com" in payload.yt_desc

    @pytest.mark.asyncio
    async def test_async_generate_script(self) -> None:
        valid_script = make_script_of_length(95)
        assert 85 <= count_words(valid_script) <= 115

        class MockAsyncClient:
            async def chat(self, **kwargs):
                return type(
                    "ChatResponse",
                    (),
                    {
                        "message": {
                            "content": f'{{"yt_title": "New JS Runtime #Shorts", "hook": "Brace yourselves.", "script": "{valid_script}", "yt_desc": "Runtime war #Shorts"}}'
                        }
                    },
                )()

        payload = await async_generate_script("JS Runtime news", client=MockAsyncClient())
        assert payload.yt_title == "New JS Runtime #Shorts"
        assert count_words(payload.script) >= 85

    def test_generate_script_exhausted_retries_trims_over_budget(self) -> None:
        too_long = make_script_of_length(130)

        class MockClientAlwaysLong:
            def chat(self, **kwargs):
                return type(
                    "ChatResponse",
                    (),
                    {
                        "message": {
                            "content": f'{{"yt_title": "Always Long #Shorts", "hook": "Too long.", "script": "{too_long}", "yt_desc": "Desc #Shorts"}}'
                        }
                    },
                )()

        payload = generate_script("Long text", client=MockClientAlwaysLong(), max_retries=2)
        assert count_words(payload.script) <= 115

    def test_generate_script_raises_on_persistent_network_error(self) -> None:
        class MockFailingClient:
            def chat(self, **kwargs):
                raise ConnectionError("Ollama connection refused")

        with pytest.raises(ConnectionError):
            generate_script("Some text", client=MockFailingClient(), max_retries=2)

    @pytest.mark.asyncio
    async def test_async_generate_script_raises_on_persistent_network_error(self) -> None:
        class MockFailingAsyncClient:
            async def chat(self, **kwargs):
                raise ConnectionError("Ollama async connection refused")

        with pytest.raises(ConnectionError):
            await async_generate_script("Some text", client=MockFailingAsyncClient(), max_retries=2)
