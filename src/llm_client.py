"""Provider adapter.

The reasoning and Q&A layers are written against one small surface:

    client.messages.create(model=, max_tokens=, system=, tools=, messages=)
      -> response.content  : list of blocks, each .type in {"text","tool_use"}
         response.usage    : .input_tokens / .output_tokens

That is the Anthropic shape, and the Anthropic client satisfies it directly.
This module lets a different provider satisfy it too, by translating in and
out. Keeping the translation here means `llm_reasoner.py` and `qa_agent.py`
never learn which provider they are talking to, and it is the same surface
the test stubs implement, so the tested path and the shipped path are one path.

Select with `llm.provider` in config.yaml: anthropic, gemini or openrouter.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

log = logging.getLogger(__name__)

__all__ = ["make_client", "Block", "Response", "BlockList", "OpenRouterClient"]


class Block:
    """One content block. Mirrors the Anthropic block shape closely enough that
    consumers can stay provider-agnostic."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self):
        return f"<Block {getattr(self, 'type', '?')}>"


class BlockList(list):
    """A list of blocks that remembers the provider-native turn it came from,
    so it can be replayed verbatim on the next request rather than
    round-tripped lossily through our own representation."""

    def __init__(self, blocks, raw=None):
        super().__init__(blocks)
        self.raw = raw


class Usage:
    def __init__(self, input_tokens=0, output_tokens=0):
        self.input_tokens = input_tokens or 0
        self.output_tokens = output_tokens or 0


class Response:
    def __init__(self, content, usage):
        self.content, self.usage = content, usage


# --------------------------------------------------------------------- Gemini
# Gemini's schema dialect is OpenAPI-flavoured and rejects several JSON Schema
# keywords outright, so proposals are filtered rather than passed through.
_SCHEMA_KEYS = {"type", "description", "properties", "required", "items",
                "enum", "nullable"}


def _clean_schema(node):
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k not in _SCHEMA_KEYS:
            continue
        if k == "properties":
            out[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _clean_schema(v)
        elif k == "type" and isinstance(v, list):
            # ["string","null"] -> "string" + nullable
            non_null = [t for t in v if t != "null"]
            out[k] = (non_null or ["string"])[0]
            if len(non_null) != len(v):
                out["nullable"] = True
        else:
            out[k] = v
    return out


class _GeminiMessages:
    def __init__(self, outer):
        self.outer = outer

    def create(self, *, model, max_tokens, messages, system=None, tools=None,
               output_config=None, **_):
        from google.genai import types

        contents, tool_names = self.outer._to_contents(messages)
        cfg = {"max_output_tokens": max_tokens}
        if system:
            cfg["system_instruction"] = system
        if tools:
            cfg["tools"] = [types.Tool(function_declarations=[
                types.FunctionDeclaration(
                    name=t["name"], description=t.get("description", ""),
                    parameters=_clean_schema(t["input_schema"]))
                for t in tools])]
        elif output_config:
            # Gemini refuses function calling and a response schema in the same
            # request, so the schema is only applied when there are no tools.
            # With tools present, the JSON contract is carried by the prompt and
            # enforced by the caller's existing malformed-response retry.
            fmt = (output_config or {}).get("format", {})
            if fmt.get("type") == "json_schema":
                cfg["response_mime_type"] = "application/json"
                cfg["response_json_schema"] = fmt["schema"]

        resp = self.outer._client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(**cfg))

        blocks, raw = [], None
        if resp.candidates:
            raw = resp.candidates[0].content
            for part in (raw.parts or []):
                if getattr(part, "text", None):
                    blocks.append(Block(type="text", text=part.text))
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    call_id = fc.id or f"call_{uuid.uuid4().hex[:12]}"
                    self.outer._names[call_id] = fc.name
                    blocks.append(Block(type="tool_use", id=call_id,
                                        name=fc.name, input=dict(fc.args or {})))
        um = getattr(resp, "usage_metadata", None)
        usage = Usage(getattr(um, "prompt_token_count", 0) if um else 0,
                      getattr(um, "candidates_token_count", 0) if um else 0)
        return Response(BlockList(blocks, raw), usage)


class GeminiClient:
    """Presents the Anthropic messages surface over google-genai."""

    def __init__(self, api_key=None):
        from google import genai
        key = api_key or os.environ.get("GEMINI_API_KEY") \
            or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise TypeError(
                "Could not resolve authentication method. Set GEMINI_API_KEY "
                "(or GOOGLE_API_KEY) for the gemini provider.")
        self._client = genai.Client(api_key=key)
        self._names: dict[str, str] = {}       # tool_use_id -> tool name
        self.messages = _GeminiMessages(self)

    def _to_contents(self, messages):
        """Anthropic-shaped messages -> Gemini contents."""
        from google.genai import types

        contents, names = [], self._names
        for m in messages:
            role, content = m["role"], m["content"]

            if isinstance(content, BlockList) and content.raw is not None:
                contents.append(content.raw)     # replay the model's own turn
                continue

            if isinstance(content, str):
                contents.append(types.Content(
                    role="user" if role == "user" else "model",
                    parts=[types.Part.from_text(text=content)]))
                continue

            parts = []
            for b in content:
                btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                if btype == "tool_result":
                    tid = b["tool_use_id"] if isinstance(b, dict) else b.tool_use_id
                    payload = b["content"] if isinstance(b, dict) else b.content
                    try:
                        payload = json.loads(payload) if isinstance(payload, str) \
                            else payload
                    except json.JSONDecodeError:
                        payload = {"result": payload}
                    if not isinstance(payload, dict):
                        payload = {"result": payload}
                    parts.append(types.Part.from_function_response(
                        name=names.get(tid, "tool"), response=payload))
                elif btype == "text":
                    text = b["text"] if isinstance(b, dict) else b.text
                    parts.append(types.Part.from_text(text=text))
                elif btype == "tool_use":
                    name = b["name"] if isinstance(b, dict) else b.name
                    args = b["input"] if isinstance(b, dict) else b.input
                    parts.append(types.Part.from_function_call(name=name,
                                                               args=args))
            if parts:
                contents.append(types.Content(
                    role="user" if role == "user" else "model", parts=parts))
        return contents, names


# ----------------------------------------------------------------- OpenRouter
# OpenRouter speaks the OpenAI chat-completions dialect. Plain httpx (already
# installed as an anthropic dependency) rather than the openai SDK: the whole
# surface used here is one POST.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class AuthenticationError(Exception):
    """Named so llm_reasoner._is_unrecoverable stops retrying a bad key."""


def _get(b, key):
    return b.get(key) if isinstance(b, dict) else getattr(b, key, None)


class _OpenRouterMessages:
    def __init__(self, outer):
        self.outer = outer

    def create(self, *, model, max_tokens, messages, system=None, tools=None,
               output_config=None, **_):
        # output_config is ignored: tools always ride along in the reasoner,
        # and many routed models reject a response schema next to them. The
        # JSON contract is in the prompt, backed by the malformed-reply retry.
        body = {"model": model, "max_tokens": max_tokens,
                "messages": _to_openai(messages, system)}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t["input_schema"]}} for t in tools]

        r = self.outer._http.post(OPENROUTER_URL, json=body)
        if r.status_code in (401, 403):
            raise AuthenticationError(f"OpenRouter rejected the key: {r.text[:200]}")
        r.raise_for_status()
        data = r.json()
        if "error" in data or not data.get("choices"):
            # OpenRouter reports upstream failures inside a 200
            raise RuntimeError(f"OpenRouter error: {data.get('error', data)}")

        msg = data["choices"][0]["message"]
        blocks = []
        if msg.get("content"):
            blocks.append(Block(type="text", text=msg["content"]))
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}      # the tool then reports the missing argument back
            blocks.append(Block(type="tool_use", id=tc["id"],
                                name=tc["function"]["name"], input=args))
        u = data.get("usage") or {}
        return Response(BlockList(blocks),
                        Usage(u.get("prompt_tokens"), u.get("completion_tokens")))


def _to_openai(messages, system):
    """Anthropic-shaped messages -> OpenAI chat messages."""
    out = [{"role": "system", "content": system}] if system else []
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        texts, calls = [], []
        for b in content:
            t = _get(b, "type")
            if t == "tool_result":
                payload = _get(b, "content")
                out.append({"role": "tool", "tool_call_id": _get(b, "tool_use_id"),
                            "content": payload if isinstance(payload, str)
                            else json.dumps(payload)})
            elif t == "text":
                texts.append(_get(b, "text"))
            elif t == "tool_use":
                calls.append({"id": _get(b, "id"), "type": "function",
                              "function": {"name": _get(b, "name"),
                                           "arguments": json.dumps(_get(b, "input"))}})
        if texts or calls:
            msg = {"role": role, "content": "\n".join(texts) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


class OpenRouterClient:
    """Presents the Anthropic messages surface over OpenRouter."""

    def __init__(self, api_key=None):
        import httpx
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise TypeError(
                "Could not resolve authentication method. Set OPENROUTER_API_KEY "
                "for the openrouter provider.")
        self._http = httpx.Client(timeout=120,
                                  headers={"Authorization": f"Bearer {key}"})
        self.messages = _OpenRouterMessages(self)


# ------------------------------------------------------------------ selection
def make_client(provider: str = "anthropic", api_key=None):
    # Load .env here rather than relying on a caller having done it: this is
    # the only place credentials are resolved, so it is the only place that
    # can guarantee they are present.
    from src.config import load_env
    load_env()
    provider = (provider or "anthropic").lower()
    if provider == "anthropic":
        import anthropic
        return anthropic.Anthropic(api_key=api_key) if api_key \
            else anthropic.Anthropic()
    if provider == "gemini":
        return GeminiClient(api_key)
    if provider == "openrouter":
        return OpenRouterClient(api_key)
    raise ValueError(f"unknown llm provider {provider!r}; "
                     "expected 'anthropic', 'gemini' or 'openrouter'")
