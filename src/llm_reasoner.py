"""The LLM layer, with its authority deliberately fenced in.

The model does exactly two jobs: it proposes candidate rules (which the Phase 5
gate then backtests before they take effect), and it writes explanations or
breaks ties the solvers could not. It never unilaterally decides a match --
every verdict it returns is stamped `resolved_by='llm'`, carries its confidence,
and is thrown away entirely if that confidence is below the floor.

It is also given tools rather than asked to eyeball anything: arithmetic runs
through a real evaluator, and a candidate rule can be checked against history
*before* the model commits to proposing it.
"""
from __future__ import annotations

import ast
import json
import logging
import operator
import time
from dataclasses import dataclass, field
from datetime import date

from src.calendar_utils import working_days_between
from src.config import load
from src.deterministic import PredicateError, backtest, validate_predicate

log = logging.getLogger(__name__)

VERDICTS = ("match", "no_match", "insufficient_information")

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "matched_candidate_id": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string"},
        "residual_explanation": {"type": "string"},
        "proposed_rule": {
            "type": ["object", "null"],
            "properties": {
                "type": {"type": ["string", "null"],
                         "enum": ["fee_formula", "timing_window",
                                  "refund_pattern", "narration_pattern", None]},
                "instrument": {"type": "string"},
                "params": {"type": "object", "additionalProperties": True},
                "tolerance_paise": {"type": "integer", "minimum": 0},
                "generalisation_note": {"type": "string"},
            },
            "required": ["type", "instrument", "params", "tolerance_paise",
                         "generalisation_note"],
            "additionalProperties": False,
        },
    },
    "required": ["verdict", "matched_candidate_id", "confidence", "reasoning",
                 "residual_explanation", "proposed_rule"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are the reasoning layer of a payment reconciliation system for an Indian \
merchant. Three independent records of the same money must be made to agree: \
the merchant's order ledger, the payment aggregator's settlement report, and \
the bank statement.

Nobody has told you this merchant's fee structure or settlement timing. You are \
looking at cases the deterministic layer could not resolve, and your job is to \
work out what general rule would explain them.

You have exactly two responsibilities:

1. Propose a machine-checkable rule that explains the case AND would hold for \
   other cases like it. Use check_rule_against_history to test it against \
   already-resolved records BEFORE you propose it. If the check shows any \
   wrong_matches, your rule is wrong, revise it or propose nothing.
2. Say which candidate, if any, this record matches, and explain why.

Hard requirements:

- Never assert a number you have not obtained from the calculate tool. Fees are \
  integer paise; rounding matters at the paise.
- "insufficient_information" is a correct, expected and valued answer. Use it \
  whenever the evidence does not settle the question. A wrong match \
  silently corrupts a ledger; an unresolved exception costs a human two \
  minutes. Abstaining is roughly fifty times cheaper than guessing.
- Only propose a rule you believe generalises. One case is an anecdote. If the \
  case looks like a one-off (a refund, a manual correction), set proposed_rule \
  to null and say so in residual_explanation.
- Your confidence must reflect the evidence, not your fluency.
- Real settlement files carry a paise or two of rounding drift on a small \
  fraction of rows, so a rule with tolerance_paise 0 will be contradicted by \
  otherwise-correct records and rejected. Propose a small non-zero tolerance \
  (a handful of paise) unless you have a reason not to. This is about noise in \
  the data, not about the fee itself, do not widen the tolerance to make a \
  wrong rate fit, because a wide tolerance that overlaps another rule is \
  rejected too.

Some requests carry a "focus" field and a list of "resolved_examples" instead
of an unmatched record. Those are not asking you to match anything, the
records are already reconciled. They are asking: looking at these examples,
what rule of the focused type describes them? Answer with verdict
"no_match" (there is no match question) and put your answer in proposed_rule.
If the examples show no consistent pattern, propose nothing and say why.

Rule predicate forms you may propose, and nothing else:
  fee_formula      params {"rate": <0..1>, "gst": <0..1>}
                   or {"flat_paise": <int>, "gst": <0..1>}
                   meaning net = gross - fee - round(fee * gst)
  timing_window    params {"min_working_days": <int>, "max_working_days": <int>}
  refund_pattern   params {}, the net falls short of the fee-implied net
  narration_pattern params {"regex": "<one capture group>"}

When you have finished calling tools, your FINAL reply must be one JSON object
and nothing else, no prose around it, no markdown fence:

{"verdict": "match" | "no_match" | "insufficient_information",
 "matched_candidate_id": "<id>" | null,
 "confidence": <0.0-1.0>,
 "reasoning": "<why>",
 "residual_explanation": "<what accounts for the amount difference>",
 "proposed_rule": null | {"type": "fee_formula" | "timing_window" |
                                  "refund_pattern" | "narration_pattern",
                          "instrument": "<UPI|CARD_CREDIT|CARD_DEBIT|
                                          NETBANKING|ALL>",
                          "params": {...},
                          "tolerance_paise": <int>,
                          "generalisation_note": "<why this generalises>"}}
"""


def _strip_fence(text: str) -> str:
    """Unwrap ```json ... ``` and any prose either side of the object.

    A schema-constrained provider never needs this; one that cannot take a
    response schema alongside tool definitions. Gemini, for instance, often
    does. Cheaper than a retry, and the retry still backs it up.
    """
    t = text.strip()
    if "```" in t:
        chunks = t.split("```")
        for chunk in chunks[1:]:
            body = chunk[4:] if chunk.lower().startswith("json") else chunk
            if body.strip().startswith("{"):
                return body.strip()
    start, end = t.find("{"), t.rfind("}")
    return t[start:end + 1] if 0 <= start < end else t


# ------------------------------------------------------------ safe calculator
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos}
_FUNCS = {"round": round, "abs": abs, "int": int, "min": min, "max": max}


def safe_calculate(expression: str):
    """Arithmetic only. No names, no attributes, no calls but the whitelist --
    this string comes from a language model and is never eval()'d."""
    def walk(n):
        if isinstance(n, ast.Constant):
            if isinstance(n.value, (int, float)):
                return n.value
            raise ValueError(f"non-numeric constant {n.value!r}")
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](walk(n.left), walk(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](walk(n.operand))
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name) or n.func.id not in _FUNCS:
                raise ValueError("only round/abs/int/min/max may be called")
            return _FUNCS[n.func.id](*[walk(a) for a in n.args])
        raise ValueError(f"unsupported expression element {type(n).__name__}")

    try:
        return walk(ast.parse(expression.strip(), mode="eval").body)
    except ZeroDivisionError:
        raise ValueError("division by zero") from None
    except (SyntaxError, TypeError, RecursionError) as e:
        raise ValueError(f"cannot evaluate: {e}") from e


TOOLS = [
    {"name": "calculate",
     "description": "Evaluate an arithmetic expression over integer paise. "
                    "Supports + - * / // % ** and round/abs/int/min/max. "
                    "Use this for every number you intend to state.",
     "input_schema": {"type": "object",
                      "properties": {"expression": {"type": "string"}},
                      "required": ["expression"]}},
    {"name": "check_rule_against_history",
     "description": "Replay a candidate rule predicate against every "
                    "already-resolved record. Returns correct_matches, "
                    "wrong_matches, support and counterexamples. Any "
                    "wrong_matches means the rule contradicts a record we have "
                    "already resolved, so it will be rejected, check before "
                    "you propose.",
     "input_schema": {"type": "object",
                      "properties": {"predicate_json": {"type": "string"}},
                      "required": ["predicate_json"]}},
    {"name": "get_working_day_lag",
     "description": "Working days between two ISO dates on the Indian "
                    "calendar, skipping weekends and public holidays.",
     "input_schema": {"type": "object",
                      "properties": {"date1": {"type": "string"},
                                     "date2": {"type": "string"}},
                      "required": ["date1", "date2"]}},
]


@dataclass
class LLMVerdict:
    verdict: str = "insufficient_information"
    matched_candidate_id: str | None = None
    confidence: float = 0.0
    reasoning: str = ""
    residual_explanation: str = ""
    proposed_rule: dict | None = None
    # bookkeeping
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_seconds: float = 0.0
    error: str | None = None
    tool_calls: list = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.verdict == "match" and self.matched_candidate_id is not None


class LLMReasoner:
    """One instance per batch run; accumulates the call and token counters."""

    def __init__(self, conn, rules, client=None, model=None, cfg=None):
        self.conn, self.rules = conn, rules
        self.cfg = cfg or load()["llm"]
        self.model = model or self.cfg["model"]
        self._client = client
        self.calls = self.tokens_in = self.tokens_out = 0
        self.calls_avoided = 0
        self.disabled: str | None = None    # set on an unrecoverable failure
        self._consecutive_failures = 0

    @property
    def client(self):
        if self._client is None:
            from src.llm_client import make_client
            self._client = make_client(self.cfg.get("provider", "anthropic"))
        return self._client

    # ------------------------------------------------------------ the tools
    def run_tool(self, name, args) -> str:
        try:
            if name == "calculate":
                return json.dumps({"result": safe_calculate(args["expression"])})
            if name == "check_rule_against_history":
                pred = args["predicate_json"]
                return json.dumps(backtest(self.conn, pred))
            if name == "get_working_day_lag":
                return json.dumps({"working_days": working_days_between(
                    date.fromisoformat(str(args["date1"])[:10]),
                    date.fromisoformat(str(args["date2"])[:10]))})
        except (PredicateError, ValueError, KeyError) as e:
            # An error is information: hand it back so the model can correct
            # itself rather than failing the whole call.
            return json.dumps({"error": str(e)})
        return json.dumps({"error": f"unknown tool {name}"})

    # ----------------------------------------------------------- the prompt
    def build_prompt(self, case: dict) -> str:
        active = [{"rule_id": r.rule_id, "type": r.rule_type,
                   "instrument": r.scope, "predicate": r.predicate}
                  for r in self.rules.rules]
        return json.dumps({
            "unresolved_record": case["record"],
            "candidates": case.get("candidates", []),
            "precomputed_working_day_lags": case.get("lags", {}),
            "active_rule_library": active,
            "note_on_library": ("Rules already active explain nothing about "
                                "this case, or it would not have reached you. "
                                "Do not re-propose them."),
            "prior_proposals_for_similar_cases": case.get("prior_proposals", []),
            "reason_it_could_not_be_resolved": case.get("reason_code"),
        }, indent=2, default=str)

    # ------------------------------------------------------------- the call
    @staticmethod
    def _is_unrecoverable(e: Exception) -> bool:
        """Missing or rejected credentials will not fix themselves. Retrying
        them with backoff turns a keyless clean clone into a seven-minute wait
        for sixty identical failures."""
        if isinstance(e, TypeError) and "authentication" in str(e).lower():
            return True
        return type(e).__name__ in ("AuthenticationError", "PermissionDeniedError")

    def resolve(self, case: dict) -> LLMVerdict:
        """One case -> one verdict. Never raises: an API failure comes back as
        a verdict carrying `error`, and the caller records an exception."""
        t0 = time.time()
        v = LLMVerdict()
        if self.disabled:
            v.error = self.disabled
            return v
        messages = [{"role": "user", "content": self.build_prompt(case)}]

        for attempt in range(self.cfg.get("max_retries", 3)):
            try:
                v = self._converse(messages, v)
                v.latency_seconds = time.time() - t0
                self._consecutive_failures = 0
                return self._apply_floor(v)
            except _MalformedResponse as e:
                log.warning("malformed model response (attempt %d): %s",
                            attempt + 1, e)
                messages.append({
                    "role": "user",
                    "content": "Your previous reply did not parse as the "
                               "required JSON object. Reply with that object "
                               "and nothing else."})
            except Exception as e:                    # network, 429, 5xx...
                v.error = f"{type(e).__name__}: {e}"
                if self._is_unrecoverable(e):
                    self.disabled = v.error
                    log.error("LLM escalation disabled for this run: %s. The "
                              "deterministic layers continue; set "
                              "ANTHROPIC_API_KEY to enable rule induction.", e)
                    break
                log.warning("LLM call failed (attempt %d): %s", attempt + 1, e)
                time.sleep(min(2 ** attempt, 8))

        v.verdict = "insufficient_information"
        v.confidence = 0.0
        v.error = v.error or "response never parsed"
        v.latency_seconds = time.time() - t0

        # Every retry for this case was spent and none worked. If that keeps
        # happening the problem is not this case, it is the service (an
        # exhausted daily quota, a sustained outage), and grinding the rest
        # of the batch through the full backoff achieves nothing. A run that
        # burned 48 minutes to make 17 successful calls and 40 quota failures
        # is what motivated this.
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.cfg.get("give_up_after_failures", 3):
            self.disabled = f"{self._consecutive_failures} cases failed in a row: {v.error}"
            log.error("LLM escalation disabled for this run after %d consecutive "
                      "failures: %s", self._consecutive_failures, v.error)
        return v

    def _converse(self, messages, v: LLMVerdict) -> LLMVerdict:
        """Manual tool loop. Manual rather than the SDK tool runner because we
        need per-call token accounting, the avoided-call count is a headline
        metric and it has to be measured, not estimated."""
        for _ in range(8):                            # tool-loop safety valve
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                output_config={"format": {"type": "json_schema",
                                          "schema": RESPONSE_SCHEMA}},
                messages=messages,
            )
            self.calls += 1
            v.calls += 1
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self.tokens_in += usage.input_tokens or 0
                self.tokens_out += usage.output_tokens or 0
                v.tokens_in += usage.input_tokens or 0
                v.tokens_out += usage.output_tokens or 0

            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if not tool_uses:
                return self._parse(resp, v)

            results = []
            for tu in tool_uses:
                out = self.run_tool(tu.name, tu.input)
                v.tool_calls.append({"tool": tu.name, "input": tu.input})
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": out})
            messages.append({"role": "user", "content": results})

        raise _MalformedResponse("tool loop did not terminate")

    def _parse(self, resp, v: LLMVerdict) -> LLMVerdict:
        text = next((b.text for b in resp.content
                     if getattr(b, "type", "") == "text"), None)
        if not text:
            raise _MalformedResponse("no text block in response")
        try:
            data = json.loads(_strip_fence(text))
        except json.JSONDecodeError as e:
            raise _MalformedResponse(str(e)) from e
        if data.get("verdict") not in VERDICTS:
            raise _MalformedResponse(f"bad verdict {data.get('verdict')!r}")

        v.verdict = data["verdict"]
        v.matched_candidate_id = data.get("matched_candidate_id")
        v.confidence = float(data.get("confidence") or 0.0)
        v.reasoning = data.get("reasoning", "")
        v.residual_explanation = data.get("residual_explanation", "")
        v.proposed_rule = self._clean_rule(data.get("proposed_rule"))
        return v

    def _clean_rule(self, proposed) -> dict | None:
        """Turn the model's rule sketch into a predicate, or drop it. A rule
        the evaluator cannot parse is rejected here, at proposal time, so it
        can never reach the apply path."""
        if not proposed or not proposed.get("type"):
            return None
        pred = {"type": proposed["type"],
                "instrument": proposed.get("instrument", "ALL"),
                "tolerance_paise": int(proposed.get("tolerance_paise", 2))}
        params = proposed.get("params") or {}
        if pred["type"] == "fee_formula":
            pred["params"] = params
        elif pred["type"] == "timing_window":
            pred["min_working_days"] = params.get("min_working_days")
            pred["max_working_days"] = params.get("max_working_days")
        elif pred["type"] == "refund_pattern":
            pred["condition"] = "net < expected_net"
            pred["residual_explained_by"] = "partial_refund"
        elif pred["type"] == "narration_pattern":
            pred["regex"] = params.get("regex", "")
            pred["maps_to"] = "settlement_batch_id"
        try:
            return validate_predicate(pred)
        except PredicateError as e:
            log.info("discarding unparseable proposed rule: %s", e)
            return None

    def _apply_floor(self, v: LLMVerdict) -> LLMVerdict:
        """Below the confidence floor the verdict is discarded regardless of
        what the model said. The rule proposal survives, proposing is cheap
        and gated downstream; asserting a match is not."""
        if v.verdict == "match" and v.confidence < self.cfg["confidence_floor"]:
            log.info("confidence %.2f below floor; downgrading to exception",
                     v.confidence)
            v.residual_explanation = (
                f"model proposed a match at confidence {v.confidence:.2f}, "
                f"below the {self.cfg['confidence_floor']} floor: "
                + v.residual_explanation)
            v.verdict = "insufficient_information"
            v.matched_candidate_id = None
        return v


class _MalformedResponse(Exception):
    pass
