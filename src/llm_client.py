"""Provider adapter.

The reasoning and Q&A layers are written against one small surface:

    client.messages.create(model=, max_tokens=, system=, tools=, messages=)
      -> response.content  : list of blocks, each .type in {"text","tool_use"}
         response.usage    : .input_tokens / .output_tokens

That is the Anthropic shape, and the Anthropic client satisfies it directly.
This module lets a different provider satisfy it too, by translating in and
out. Keeping the translation here means `llm_reasoner.py` and `qa_agent.py`
never learn which provider they are talking to -- and it is the same surface
the test stubs implement, so the tested path and the shipped path are one path.

Select with `llm.provider` in config.yaml.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

log = logging.getLogger(__name__)

__all__ = ["make_client", "Block", "Response", "BlockList"]


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
    raise ValueError(f"unknown llm provider {provider!r}; "
                     "expected 'anthropic' or 'gemini'")
