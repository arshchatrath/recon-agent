"""The rule-driven matcher.

Nothing about this merchant's fees or settlement timing is hardcoded here. The
rule library is loaded from the `rules` table, which starts holding exactly one
rule (exact ID match). Everything else arrives by promotion in Phase 5.

Predicates are data, never code. A proposed `expr` string is matched against a
small set of known templates and then computed structurally in Python -- an
LLM-authored string is never eval()'d.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date

from src.calendar_utils import working_days_between
from src.money import apply_rate, within_tolerance

log = logging.getLogger(__name__)

INAPPLICABLE = "INAPPLICABLE"          # third verdict: this rule has no opinion
ALL = "ALL"
RULE_TYPES = {"exact_id", "fee_formula", "timing_window",
              "refund_pattern", "narration_pattern"}


class PredicateError(ValueError):
    """A predicate that the evaluator cannot parse. Rejected at proposal time."""


class RuleConflictError(Exception):
    def __init__(self, rule_ids):
        self.rule_ids = list(rule_ids)
        super().__init__(f"precedence cycle among rules {self.rule_ids}")


# ------------------------------------------------------------ fee templates
# Two shapes, because Indian MDR is quoted both ways: a percentage of gross,
# and a flat per-transaction fee. GST always applies to the fee, not the gross.
RATE_EXPR = ("net = gross - round(gross * {rate})"
             " - round(round(gross * {rate}) * {gst})")
FLAT_EXPR = "net = gross - {flat_paise} - round({flat_paise} * {gst})"


def fee_expected_net(gross: int, params: dict) -> int:
    """Apply a fee-formula predicate's params to a gross amount. All int paise."""
    gst = params.get("gst", 0)
    if "flat_paise" in params:
        fee = int(params["flat_paise"])
    else:
        fee = apply_rate(gross, params["rate"])
    return gross - fee - apply_rate(fee, gst)


# --------------------------------------------------------------- validation
def _num(v, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PredicateError(f"not a number: {v!r}")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise PredicateError(f"out of range: {v!r}")
    return v


def validate_predicate(pred) -> dict:
    """Parse-check a predicate. Raises PredicateError; never returns silently bad
    data. Called at proposal intake, so an unparseable rule can never go live."""
    if isinstance(pred, str):
        try:
            pred = json.loads(pred)
        except json.JSONDecodeError as e:
            raise PredicateError(f"not JSON: {e}") from e
    if not isinstance(pred, dict):
        raise PredicateError("predicate must be an object")

    t = pred.get("type")
    if t not in RULE_TYPES:
        raise PredicateError(f"unknown rule type {t!r}")
    _num(pred.get("tolerance_paise", 0), lo=0)

    if t == "fee_formula":
        params = pred.get("params")
        if not isinstance(params, dict):
            raise PredicateError("fee_formula needs params")
        _num(params.get("gst", 0), lo=0, hi=1)
        if "flat_paise" in params:
            _num(params["flat_paise"], lo=0)
            expected_expr = FLAT_EXPR
        elif "rate" in params:
            _num(params["rate"], lo=0, hi=1)
            expected_expr = RATE_EXPR
        else:
            raise PredicateError("fee_formula params need 'rate' or 'flat_paise'")
        expr = pred.get("expr")
        if expr is not None and expr != expected_expr:
            raise PredicateError("expr does not match a known fee template")

    elif t == "timing_window":
        lo = _num(pred.get("min_working_days"), lo=0)
        hi = _num(pred.get("max_working_days"), lo=0)
        if hi < lo:
            raise PredicateError("max_working_days < min_working_days")

    elif t == "refund_pattern":
        if pred.get("condition") != "net < expected_net":
            raise PredicateError("unsupported refund_pattern condition")

    elif t == "narration_pattern":
        try:
            re.compile(pred["regex"])
        except (KeyError, re.error) as e:
            raise PredicateError(f"bad narration regex: {e}") from e
        if pred.get("maps_to") != "settlement_batch_id":
            raise PredicateError("narration_pattern must map to settlement_batch_id")

    return pred


def canonical_fingerprint(pred: dict, precision: int = 6) -> str:
    """Stable key for 'is this the same rule?'. Rounds params so two proposals
    of 0.0350001 and 0.035 collapse into one."""
    def norm(v):
        if isinstance(v, float):
            return round(v, precision)
        if isinstance(v, dict):
            return {k: norm(v[k]) for k in sorted(v)}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v
    return json.dumps(norm(pred), sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------- evaluator
def _d(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def _as_date(v):
    return date.fromisoformat(str(v)[:10])


def evaluate(pred: dict, left, right, rules: "RuleSet | None" = None):
    """Verdict for one candidate pair: True, False, or INAPPLICABLE.

    `left` is an order or bank credit, `right` a settlement. Rows are anything
    subscriptable by column name (sqlite3.Row or dict).
    """
    t = pred["type"]
    tol = int(pred.get("tolerance_paise", 0))
    scope = pred.get("instrument", ALL)
    instr = _d(right, "instrument")
    if scope != ALL and instr != scope:
        return INAPPLICABLE

    if t == "exact_id":
        oid, claimed = _d(left, "order_id"), _d(right, "order_id_claimed")
        if oid is None or claimed is None:
            return INAPPLICABLE
        return oid == claimed

    if t == "fee_formula":
        # The question a fee rule answers is "does the aggregator's net match
        # what the MERCHANT'S order should have settled at", so the gross comes
        # from the left (the order) whenever we have one. Checking the
        # settlement row against itself only proves the aggregator can subtract
        # -- it is true of every internally-consistent row, including an orphan
        # bound to the wrong order, which is exactly how one got matched.
        order_gross = _d(left, "gross_amount_paise")
        gross = _d(right, "gross_amount_paise")
        net = _d(right, "net_amount_paise")
        if net is None or (gross is None and order_gross is None):
            return INAPPLICABLE
        if order_gross is not None and gross is not None and order_gross != gross:
            # A partial leg of a split payout. The fee rate is not what is in
            # question here, so the rule has no opinion rather than a negative
            # one -- being counted wrong for this would block its promotion.
            return INAPPLICABLE
        return within_tolerance(
            net, fee_expected_net(order_gross if order_gross is not None
                                  else gross, pred["params"]), tol)

    if t == "timing_window":
        od = _d(left, "order_datetime")
        sd = _d(right, "settled_datetime")
        if od is None or sd is None:
            return INAPPLICABLE
        lag = working_days_between(_as_date(od), _as_date(sd))
        return pred["min_working_days"] <= lag <= pred["max_working_days"]

    if t == "refund_pattern":
        # "the shortfall is a refund, not a mismatch" -- needs a fee rule to
        # know what the net should have been, so it is inapplicable until one
        # has been learned.
        #
        # It ALSO requires the merchant's own ledger to say the order was
        # partly refunded. Without that condition the rule reads "any net below
        # expectation is a refund", which would explain away every genuine
        # mismatch in the batch -- a false-positive engine wearing a rule's
        # clothing. The status column is ledger data, not ground truth.
        gross, net = _d(right, "gross_amount_paise"), _d(right, "net_amount_paise")
        if gross is None or net is None or rules is None:
            return INAPPLICABLE
        status = _d(left, "status")
        if status is None:
            return INAPPLICABLE
        if status != "refunded_partial":
            return False
        expected = rules.expected_net(gross, instr)
        if expected is None:
            return INAPPLICABLE
        return net < expected - tol

    if t == "narration_pattern":
        narration = _d(left, "narration")
        sbid = _d(right, "settlement_batch_id")
        if narration is None or sbid is None:
            return INAPPLICABLE
        m = re.search(pred["regex"], str(narration))
        return bool(m) and m.group(1) == sbid

    return INAPPLICABLE


# ----------------------------------------------------------------- rule set
class Rule:
    __slots__ = ("rule_id", "rule_type", "scope", "predicate", "priority")

    def __init__(self, rule_id, rule_type, scope, predicate_json, priority):
        self.rule_id = rule_id
        self.rule_type = rule_type
        self.scope = scope or ALL
        self.predicate = validate_predicate(predicate_json)
        self.priority = priority

    def __repr__(self):
        return f"<Rule {self.rule_id} {self.rule_type}/{self.scope} p{self.priority}>"


class RuleSet:
    """Active rules in evaluation order, plus the conflict checks."""

    def __init__(self, rules: list[Rule]):
        self.rules = topological_order(rules)
        self.overlaps = detect_overlaps(self.rules)
        for a, b in self.overlaps:
            log.warning("rules %s and %s could both fire on one record",
                        a.rule_id, b.rule_id)

    @classmethod
    def load(cls, conn) -> "RuleSet":
        """Load active rules; anything unparseable is skipped, not fatal."""
        good = []
        for r in conn.execute(
                "SELECT rule_id, rule_type, scope_instrument, predicate_json,"
                " priority FROM rules WHERE status='active'"):
            try:
                good.append(Rule(r["rule_id"], r["rule_type"],
                                 r["scope_instrument"], r["predicate_json"],
                                 r["priority"]))
            except PredicateError as e:
                log.error("skipping unparseable active rule %s: %s", r["rule_id"], e)
        return cls(good)

    def of_type(self, rule_type, instrument=None):
        return [r for r in self.rules if r.rule_type == rule_type
                and (instrument is None or r.scope in (ALL, instrument))]

    def first_match(self, left, right):
        """First rule in precedence order that returns True. -> (Rule, verdict)."""
        for r in self.rules:
            if evaluate(r.predicate, left, right, self) is True:
                return r
        return None

    def expected_net(self, gross: int, instrument: str) -> int | None:
        """What an active fee rule says this gross should settle at, or None if
        no fee rule has been learned for this instrument yet."""
        for r in self.of_type("fee_formula", instrument):
            return fee_expected_net(gross, r.predicate["params"])
        return None

    def expected_lag(self, instrument: str) -> tuple[int, int] | None:
        for r in self.of_type("timing_window", instrument):
            return (r.predicate["min_working_days"], r.predicate["max_working_days"])
        return None

    def __len__(self):
        return len(self.rules)


# ----------------------------------------------------- precedence machinery
def _specificity(r: Rule) -> int:
    return 0 if r.scope == ALL else 1


def precedence_edges(rules: list[Rule]) -> list[tuple[int, int]]:
    """a -> b means a is evaluated before b. Two independent sources of order:
    a lower priority number, and a more specific instrument scope."""
    edges = []
    for i, a in enumerate(rules):
        for j, b in enumerate(rules):
            if i == j:
                continue
            if a.priority < b.priority:
                edges.append((i, j))
            elif a.rule_type == b.rule_type and _specificity(a) > _specificity(b):
                edges.append((i, j))
    return edges


def topological_order(rules: list[Rule]) -> list[Rule]:
    """Kahn's algorithm. A cycle means the library contradicts itself about
    which rule wins -- that is a RuleConflictError, not something to guess at."""
    n = len(rules)
    edges = set(precedence_edges(rules))
    indeg = [0] * n
    adj = [[] for _ in range(n)]
    for a, b in edges:
        adj[a].append(b)
        indeg[b] += 1

    # deterministic tie-break so the same library always evaluates in the same
    # order: priority, then specificity, then rule_id
    ready = sorted((i for i in range(n) if indeg[i] == 0),
                   key=lambda i: (rules[i].priority, -_specificity(rules[i]),
                                  rules[i].rule_id))
    out = []
    while ready:
        i = ready.pop(0)
        out.append(i)
        for j in adj[i]:
            indeg[j] -= 1
            if indeg[j] == 0:
                ready.append(j)
        ready.sort(key=lambda k: (rules[k].priority, -_specificity(rules[k]),
                                  rules[k].rule_id))
    if len(out) != n:
        raise RuleConflictError(rules[i].rule_id for i in range(n) if i not in out)
    return [rules[i] for i in out]


def detect_overlaps(rules: list[Rule]) -> list[tuple[Rule, Rule]]:
    """Pairs of same-type, same-scope rules whose tolerance intervals overlap,
    i.e. both could fire on one record. Warned about, not fatal."""
    out = []
    for i, a in enumerate(rules):
        for b in rules[i + 1:]:
            if a.rule_type != b.rule_type or a.scope != b.scope:
                continue
            if a.rule_type == "fee_formula":
                # compare on a reference amount: do their expected nets sit
                # within the sum of their tolerances?
                ref = 1_000_000
                na = fee_expected_net(ref, a.predicate["params"])
                nb = fee_expected_net(ref, b.predicate["params"])
                span = (a.predicate.get("tolerance_paise", 0)
                        + b.predicate.get("tolerance_paise", 0))
                if abs(na - nb) <= span:
                    out.append((a, b))
            elif a.rule_type == "timing_window":
                if (a.predicate["min_working_days"] <= b.predicate["max_working_days"]
                        and b.predicate["min_working_days"]
                        <= a.predicate["max_working_days"]):
                    out.append((a, b))
    return out


# ----------------------------------------------------------------- backtest
def backtest(conn, predicate, exclude_batch=None) -> dict:
    """Replay a candidate predicate against every already-resolved match.

    A rule earns its place by agreeing with what we have already decided. So:

      correct  -- the predicate fires True on a pair we recorded as a match
      wrong    -- the predicate says False about a pair we recorded as a match,
                  i.e. it contradicts a resolved record
      skipped  -- INAPPLICABLE; the rule has no opinion, which costs it nothing

    `wrong` is the number that matters. A fee rate induced from one coincidental
    case (a refund that happens to look like a fee at another rate) will be
    False on every genuine settlement of that instrument, and the count blows
    up immediately. That is the gate catching a plausible-looking wrong answer.
    """
    predicate = validate_predicate(predicate)
    rules = RuleSet([])

    # Two independent sources of already-resolved evidence, unioned:
    #
    #   1. order/settlement pairs we have recorded as matches, and
    #   2. settlements the BANK leg confirmed -- the subset-sum solver proved
    #      they make up a real bulk credit -- joined to the order whose id they
    #      claim.
    #
    # (2) matters enormously: it needs no knowledge of fees, so it exists from
    # batch 1. Without it the system deadlocks -- no fee rule can gather
    # backtest support until some pair is matched, and no pair can be matched
    # until a fee rule is promoted. The bank leg is the way out, and it is
    # honest evidence rather than a bootstrap hack: arithmetic on the bank
    # statement, independent of anything the aggregator claims.
    sql = """
        SELECT left_id AS order_id, right_id AS settlement_txn_id, batch_id
          FROM matches
         WHERE left_type='order' AND right_type='settlement'
        UNION
        SELECT s.order_id_claimed, s.settlement_txn_id, s.batch_id
          FROM matches m
          JOIN settlements s ON s.settlement_txn_id = m.right_id
          JOIN orders o ON o.order_id = s.order_id_claimed
         WHERE m.left_type='bank_credit' AND m.right_type='settlement'
    """
    params = []
    if exclude_batch is not None:
        sql = f"SELECT * FROM ({sql}) WHERE batch_id <> ?"
        params.append(exclude_batch)

    # Orders paid out across several settlements. A split's later legs settle a
    # day or more after the first, so judging a base-settlement-rhythm rule on
    # them counts a correct rule wrong -- the same reason refunded orders are
    # skipped below. A fee rule is already INAPPLICABLE to a partial leg, so
    # this only affects timing.
    # Read this from the settlements data, not from the matches table. History
    # includes pairs confirmed by the BANK leg that were never matched
    # order-to-settlement, so a match-based filter silently misses exactly the
    # unmatched split legs that contradict the rule. This also covers an order
    # carrying a chargeback reversal, which is a second settlement row too.
    split_orders = {r[0] for r in conn.execute(
        "SELECT order_id_claimed FROM settlements"
        " GROUP BY order_id_claimed, batch_id HAVING COUNT(*) > 1")}

    correct = wrong = skipped = 0
    examples, deviations = [], []
    for m in conn.execute(sql, params).fetchall():
        o = conn.execute("SELECT * FROM orders WHERE order_id=?",
                         (m["order_id"],)).fetchone()
        s = conn.execute("SELECT * FROM settlements WHERE settlement_txn_id=?",
                         (m["settlement_txn_id"],)).fetchone()
        if o is None or s is None:
            continue
        if o["order_id"] in split_orders:
            skipped += 1
            continue
        if o["status"] != "captured":
            # The merchant's own ledger says this order was refunded, so a
            # short settlement is expected. Holding that against a fee rule
            # would be scoring it on a question it was never asked. This is
            # ledger data, not ground truth -- the status column is in
            # orders.csv, which the pipeline is entitled to read.
            skipped += 1
            continue
        v = evaluate(predicate, o, s, rules)
        if v is INAPPLICABLE:
            skipped += 1
        elif v:
            correct += 1
        else:
            wrong += 1
            # Record the SIZE of the disagreement, not just its existence. A
            # correct rule contradicted by rounding drift misses by a paise or
            # two; a wrong rate misses by thousands. The gate cannot tell those
            # apart without this number, and it must.
            deviation = None
            if predicate.get("type") == "fee_formula":
                try:
                    deviation = abs(int(s["net_amount_paise"])
                                    - fee_expected_net(int(o["gross_amount_paise"]),
                                                       predicate["params"]))
                except (KeyError, TypeError, ValueError):
                    deviation = None
            deviations.append(deviation)
            if len(examples) < 5:
                examples.append({"order_id": o["order_id"],
                                 "settlement_txn_id": s["settlement_txn_id"],
                                 "gross_amount_paise": s["gross_amount_paise"],
                                 "net_amount_paise": s["net_amount_paise"],
                                 "deviation_paise": deviation})

    support = correct + wrong
    known = [d for d in deviations if d is not None]
    return {"correct_matches": correct, "wrong_matches": wrong,
            "skipped": skipped, "support": support,
            "precision": (correct / support) if support else 0.0,
            "max_deviation_paise": max(known) if known else None,
            "counterexamples": examples}
