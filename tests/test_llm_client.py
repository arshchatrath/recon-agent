"""The provider adapter. Exercised against a fake google-genai so the suite
still needs no credentials of any kind."""
import json
import sys
import types as pytypes

import pytest

from src.llm_client import (Block, BlockList, GeminiClient, _clean_schema,
                            make_client)


# ------------------------------------------------------------- schema cleaning
def test_json_schema_keywords_gemini_rejects_are_stripped():
    cleaned = _clean_schema({
        "type": "object",
        "additionalProperties": False,          # Gemini rejects this
        "properties": {"a": {"type": "string", "minLength": 2},
                       "b": {"type": "array", "items": {"type": "integer"}}},
        "required": ["a"]})
    assert "additionalProperties" not in cleaned
    assert "minLength" not in cleaned["properties"]["a"]
    assert cleaned["properties"]["b"]["items"] == {"type": "integer"}
    assert cleaned["required"] == ["a"]


def test_a_nullable_union_type_becomes_a_scalar_plus_nullable():
    assert _clean_schema({"type": ["string", "null"]}) == {
        "type": "string", "nullable": True}


def test_nested_properties_are_cleaned_recursively():
    cleaned = _clean_schema({
        "type": "object",
        "properties": {"rule": {"type": "object", "additionalProperties": True,
                                "properties": {"rate": {"type": "number",
                                                        "maximum": 1}}}}})
    inner = cleaned["properties"]["rule"]
    assert "additionalProperties" not in inner
    assert "maximum" not in inner["properties"]["rate"]


# ------------------------------------------------------------- fake genai SDK
class FakePart:
    def __init__(self, text=None, function_call=None):
        self.text, self.function_call = text, function_call


class FakeFC:
    def __init__(self, name, args, id=None):
        self.name, self.args, self.id = name, args, id


class FakeContent:
    def __init__(self, parts, role="model"):
        self.parts, self.role = parts, role


class FakeUsage:
    prompt_token_count, candidates_token_count = 321, 123


class FakeResp:
    def __init__(self, parts):
        self.candidates = [pytypes.SimpleNamespace(content=FakeContent(parts))]
        self.usage_metadata = FakeUsage()


@pytest.fixture
def fake_genai(monkeypatch):
    """Installs a stand-in `google.genai` and records every request."""
    calls = []
    scripted = []

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            calls.append({"model": model, "contents": contents, "config": config})
            return FakeResp(scripted.pop(0) if scripted else
                            [FakePart(text='{"ok": true}')])

    class FakeClient:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.models = FakeModels()

    real_types = __import__("google.genai", fromlist=["types"]).types
    fake = pytypes.ModuleType("google.genai")
    fake.Client = FakeClient
    fake.types = real_types
    google = pytypes.ModuleType("google")
    google.genai = fake
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", fake)
    return pytypes.SimpleNamespace(calls=calls, scripted=scripted)


# ------------------------------------------------------------------ the client
def test_a_missing_key_raises_the_same_shape_the_pipeline_knows_to_disable_on(
        monkeypatch, fake_genai):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(TypeError) as e:
        GeminiClient()
    # llm_reasoner._is_unrecoverable keys off this wording
    assert "authentication" in str(e.value).lower()


def test_the_key_comes_from_the_environment(monkeypatch, fake_genai):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
    assert GeminiClient()._client.api_key == "test-key-123"


def test_text_responses_arrive_as_anthropic_shaped_blocks(monkeypatch, fake_genai):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake_genai.scripted.append([FakePart(text="hello")])
    r = GeminiClient().messages.create(
        model="gemini-2.5-flash", max_tokens=100,
        messages=[{"role": "user", "content": "hi"}])
    assert [b.type for b in r.content] == ["text"]
    assert r.content[0].text == "hello"
    assert r.usage.input_tokens == 321 and r.usage.output_tokens == 123


def test_a_function_call_becomes_a_tool_use_block(monkeypatch, fake_genai):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake_genai.scripted.append(
        [FakePart(function_call=FakeFC("calculate", {"expression": "1+1"}))])
    r = GeminiClient().messages.create(
        model="m", max_tokens=100, messages=[{"role": "user", "content": "go"}])
    b = r.content[0]
    assert b.type == "tool_use" and b.name == "calculate"
    assert b.input == {"expression": "1+1"} and b.id


def test_tools_are_translated_into_function_declarations(monkeypatch, fake_genai):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    GeminiClient().messages.create(
        model="m", max_tokens=100, system="be good",
        messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "calculate", "description": "adds",
                "input_schema": {"type": "object", "additionalProperties": False,
                                 "properties": {"expression": {"type": "string"}},
                                 "required": ["expression"]}}])
    cfg = fake_genai.calls[0]["config"]
    decl = cfg.tools[0].function_declarations[0]
    assert decl.name == "calculate"
    assert cfg.system_instruction == "be good"


def test_a_response_schema_is_only_sent_when_there_are_no_tools(
        monkeypatch, fake_genai):
    """Gemini rejects function calling and a response schema together."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    c = GeminiClient()
    oc = {"format": {"type": "json_schema", "schema": {"type": "object"}}}

    c.messages.create(model="m", max_tokens=10, output_config=oc,
                      messages=[{"role": "user", "content": "x"}])
    assert fake_genai.calls[-1]["config"].response_mime_type == "application/json"

    c.messages.create(model="m", max_tokens=10, output_config=oc,
                      tools=[{"name": "t", "description": "d",
                              "input_schema": {"type": "object",
                                               "properties": {}}}],
                      messages=[{"role": "user", "content": "x"}])
    assert fake_genai.calls[-1]["config"].response_mime_type is None


def test_a_tool_result_round_trips_back_as_a_function_response(
        monkeypatch, fake_genai):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    c = GeminiClient()
    fake_genai.scripted.append(
        [FakePart(function_call=FakeFC("calculate", {"expression": "1+1"}))])
    first = c.messages.create(model="m", max_tokens=10,
                              messages=[{"role": "user", "content": "go"}])
    tool_use_id = first.content[0].id

    c.messages.create(model="m", max_tokens=10, messages=[
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": first.content},
        {"role": "user", "content": [{"type": "tool_result",
                                      "tool_use_id": tool_use_id,
                                      "content": json.dumps({"result": 2})}]}])
    contents = fake_genai.calls[-1]["contents"]
    fr = contents[-1].parts[0].function_response
    assert fr.name == "calculate"          # id was resolved back to the name
    assert fr.response == {"result": 2}


def test_the_models_own_turn_is_replayed_verbatim(monkeypatch, fake_genai):
    """We hand back the provider's native Content rather than re-encoding our
    block objects, so nothing is lost in translation."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    c = GeminiClient()
    fake_genai.scripted.append([FakePart(text="thinking out loud")])
    first = c.messages.create(model="m", max_tokens=10,
                              messages=[{"role": "user", "content": "go"}])
    assert isinstance(first.content, BlockList) and first.content.raw is not None

    c.messages.create(model="m", max_tokens=10, messages=[
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": first.content}])
    assert fake_genai.calls[-1]["contents"][-1] is first.content.raw


def test_a_non_json_tool_result_is_still_deliverable(monkeypatch, fake_genai):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    c = GeminiClient()
    c._names["t1"] = "calculate"
    c.messages.create(model="m", max_tokens=10, messages=[
        {"role": "user", "content": [{"type": "tool_result",
                                      "tool_use_id": "t1",
                                      "content": "not json at all"}]}])
    fr = fake_genai.calls[-1]["contents"][-1].parts[0].function_response
    assert fr.response == {"result": "not json at all"}


# -------------------------------------------------------------- make_client
def test_make_client_selects_the_provider(monkeypatch, fake_genai):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert isinstance(make_client("gemini"), GeminiClient)


def test_an_unknown_provider_is_rejected_loudly():
    with pytest.raises(ValueError, match="unknown llm provider"):
        make_client("hal9000")
