"""Proposal intake, the promotion gate, and retirement.

This is the part that makes the system self-improving rather than merely
LLM-assisted. A proposed rule is a hypothesis; it becomes part of the
deterministic layer only after it has been proposed independently several
times, carried enough confidence, and -- the gate that actually matters --
been replayed against every record already resolved without contradicting one
of them.

Following the Hypotheses-to-Theories framework (Zhu et al., arXiv:2310.07064):
induce candidate rules from examples, filter by occurrence count and
association with correct answers, then apply the surviving library. The
backtest is our correctness filter, and it is deliberately unforgiving --
one promoted bad rule silently corrupts every batch after it, while a
rejection costs nothing but an open exception.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from src.config import load
from src.deterministic import (ALL, PredicateError, Rule, RuleConflictError,
                               RuleSet, backtest, canonical_fingerprint,
                               detect_overlaps, topological_order,
                               validate_predicate)

log = logging.getLogger(__name__)

# Instrument-scoped rules outrank library-wide ones; keeping the two bands
# apart is what stops the precedence DAG developing cycles on its own.
PRIORITY_SPECIFIC, PRIORITY_GENERAL = 50, 80


def audit(conn, event, *, rule_id=None, proposal_id=None, batch_id=None, detail):
    conn.execute(
        "INSERT INTO rule_audit (event, rule_id, proposal_id, batch_id,"
        " detail_json) VALUES (?,?,?,?,?)",
        (event, rule_id, proposal_id, batch_id, json.dumps(detail, default=str)))


# ------------------------------------------------------------------- intake
def intake(conn, batch_id, predicate, confidence, case_id=None, cfg=None) -> dict:
    """Record one proposal. Identical proposals collapse onto one fingerprint
    and increment its occurrence count -- that count is the first gate."""
    try:
        pred = validate_predicate(predicate)
    except PredicateError as e:
        audit(conn, "rejected", batch_id=batch_id,
              detail={"gate": "schema", "error": str(e), "predicate": predicate})
        return {"status": "rejected", "reason": f"unparseable predicate: {e}"}

    # Fingerprint on the CLAIM, not on the noise allowance. "CARD_DEBIT charges
    # 0.9% plus 18% GST" is one hypothesis; tolerance_paise is a nuisance
    # parameter about how much rounding drift the data carries, not part of
    # what is being claimed. Including it splits one recurring proposal into
    # tol=0/1/2/3 variants that each stay below MIN_OCCURRENCES forever, so the
    # model converges on the right answer and the gate never sees it converge.
    # Observed live: four correct fee formulas, all stuck at n=1..3.
    fp = canonical_fingerprint({k: v for k, v in pred.items()
                                if k != "tolerance_paise"})
    scope = pred.get("instrument", ALL)

    # already live? then this is not news
    for r in conn.execute("SELECT rule_id, predicate_json FROM rules"
                          " WHERE status='active'"):
        if canonical_fingerprint(json.loads(r["predicate_json"])) == fp:
            audit(conn, "rejected", rule_id=r["rule_id"], batch_id=batch_id,
                  detail={"gate": "duplicate", "of_rule": r["rule_id"]})
            return {"status": "rejected",
                    "reason": f"duplicates active rule {r['rule_id']}"}

    row = conn.execute("SELECT * FROM rule_proposals WHERE fingerprint=?",
                       (fp,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO rule_proposals (batch_id, rule_type, scope_instrument,"
            " predicate_json, fingerprint, proposed_by_case_id, llm_confidence)"
            " VALUES (?,?,?,?,?,?,?)",
            (batch_id, pred["type"], scope, json.dumps(pred), fp, case_id,
             float(confidence)))
        pid, occurrences, avg = cur.lastrowid, 1, float(confidence)
    else:
        pid = row["proposal_id"]
        occurrences = row["occurrence_count"] + 1
        prior = row["llm_confidence"] or 0.0
        avg = (prior * row["occurrence_count"] + float(confidence)) / occurrences
        # Keep the most generous tolerance anyone proposed, capped. The claim
        # is identical across these proposals; the widest allowance is the one
        # that survives rounding drift. The cap stops a single sloppy proposal
        # widening a rule until it swallows its neighbours -- and the overlap
        # gate still rejects it if it does.
        cap = (cfg or load()["rule_engine"]).get("max_tolerance_paise", 10)
        stored = validate_predicate(row["predicate_json"])
        pred = dict(pred, tolerance_paise=min(
            max(int(pred.get("tolerance_paise", 0)),
                int(stored.get("tolerance_paise", 0))), cap))
        conn.execute("UPDATE rule_proposals SET occurrence_count=?,"
                     " llm_confidence=?, batch_id=?, predicate_json=?"
                     " WHERE proposal_id=?",
                     (occurrences, avg, batch_id, json.dumps(pred), pid))

    audit(conn, "proposed", proposal_id=pid, batch_id=batch_id,
          detail={"predicate": pred, "occurrence_count": occurrences,
                  "avg_confidence": round(avg, 3), "case_id": case_id})
    return {"status": "pending", "proposal_id": pid,
            "occurrence_count": occurrences, "avg_confidence": avg}


# --------------------------------------------------------------------- gate
def check_gate(conn, proposal, cfg=None) -> tuple[bool, dict]:
    """All four gates, evaluated in full so the audit trail records every
    reason a rule failed, not merely the first."""
    cfg = cfg or load()["rule_engine"]
    pred = json.loads(proposal["predicate_json"])
    report = {"predicate": pred, "gates": {}}

    occ = proposal["occurrence_count"]
    report["gates"]["occurrence"] = {
        "value": occ, "required": cfg["min_occurrences"],
        "passed": occ >= cfg["min_occurrences"]}

    conf = proposal["llm_confidence"] or 0.0
    report["gates"]["confidence"] = {
        "value": round(conf, 3), "required": cfg["confidence_floor"],
        "passed": conf >= cfg["confidence_floor"]}

    bt = backtest(conn, pred)
    report["backtest"] = bt
    zero_tol = cfg.get("zero_tolerance_money_paise", 0)
    noise_band = cfg.get("noise_band_paise", 5)
    money_at_risk = sum(abs(c["net_amount_paise"]) for c in bt["counterexamples"])

    # Is this rule WRONG, or is it RIGHT about noisy data? Both look like a
    # contradicted record, and counting them the same rejected four correct
    # fee formulas induced by a live model -- each missed by a paise or two of
    # rounding drift on a single row. The magnitude separates them cleanly: a
    # wrong rate misses by thousands of paise, drift by single digits. So a
    # contradiction is only fatal when it is bigger than the noise band.
    max_dev = bt.get("max_deviation_paise")
    within_noise = (bt["wrong_matches"] > 0 and max_dev is not None
                    and max_dev <= noise_band)
    fatal = (bt["wrong_matches"] > 0 and money_at_risk > zero_tol
             and not within_noise)
    effective_precision = (1.0 if within_noise else bt["precision"])

    report["gates"]["backtest"] = {
        "support": bt["support"], "required_support": cfg["min_backtest_support"],
        "precision": round(bt["precision"], 4),
        "required_precision": cfg["backtest_precision_floor"],
        "wrong_matches": bt["wrong_matches"],
        "max_deviation_paise": max_dev,
        "noise_band_paise": noise_band,
        "contradictions_within_noise": within_noise,
        "counterexample_money_paise": money_at_risk,
        "passed": (bt["support"] >= cfg["min_backtest_support"]
                   and effective_precision >= cfg["backtest_precision_floor"]
                   and not fatal)}

    conflict = check_conflicts(conn, proposal)
    report["gates"]["conflict"] = conflict

    passed = all(g["passed"] for g in report["gates"].values())
    report["passed"] = passed
    report["failed_gates"] = [k for k, g in report["gates"].items()
                              if not g["passed"]]
    return passed, report


def check_conflicts(conn, proposal) -> dict:
    """Would adding this rule make the library unorderable, or duplicate the
    reach of one already in it?"""
    pred = json.loads(proposal["predicate_json"])
    scope = proposal["scope_instrument"]
    priority = PRIORITY_GENERAL if scope == ALL else PRIORITY_SPECIFIC
    existing = [Rule(r["rule_id"], r["rule_type"], r["scope_instrument"],
                     r["predicate_json"], r["priority"])
                for r in conn.execute(
                    "SELECT rule_id, rule_type, scope_instrument, predicate_json,"
                    " priority FROM rules WHERE status='active'")]
    candidate = Rule(-1, proposal["rule_type"], scope, json.dumps(pred), priority)

    try:
        topological_order(existing + [candidate])
    except RuleConflictError as e:
        return {"passed": False, "reason": "precedence cycle",
                "rule_ids": e.rule_ids}

    overlaps = [(a.rule_id, b.rule_id)
                for a, b in detect_overlaps(existing + [candidate])
                if -1 in (a.rule_id, b.rule_id)]
    if overlaps:
        return {"passed": False, "reason": "tolerance interval fully overlaps "
                                           "an active rule of the same scope",
                "overlaps_with": [x for pair in overlaps for x in pair if x != -1]}
    return {"passed": True}


# ---------------------------------------------------------------- promotion
def promote(conn, proposal, report) -> int:
    scope = proposal["scope_instrument"]
    cur = conn.execute(
        "INSERT INTO rules (rule_type, scope_instrument, predicate_json,"
        " priority, status, promoted_at, promoted_from_proposal_id)"
        " VALUES (?,?,?,?, 'active', ?, ?)",
        (proposal["rule_type"], scope, proposal["predicate_json"],
         PRIORITY_GENERAL if scope == ALL else PRIORITY_SPECIFIC,
         datetime.now().isoformat(timespec="seconds"), proposal["proposal_id"]))
    rule_id = cur.lastrowid
    conn.execute("UPDATE rule_proposals SET status='promoted' WHERE proposal_id=?",
                 (proposal["proposal_id"],))
    audit(conn, "promoted", rule_id=rule_id, proposal_id=proposal["proposal_id"],
          batch_id=proposal["batch_id"], detail=report)
    log.info("promoted rule %s: %s %s", rule_id, proposal["rule_type"], scope)
    return rule_id


def reject(conn, proposal, report):
    reason = ", ".join(report["failed_gates"])
    conn.execute("UPDATE rule_proposals SET status='rejected', rejection_reason=?"
                 " WHERE proposal_id=?", (reason, proposal["proposal_id"]))
    audit(conn, "rejected", proposal_id=proposal["proposal_id"],
          batch_id=proposal["batch_id"], detail=report)
    log.info("rejected proposal %s on gate(s) %s", proposal["proposal_id"], reason)


def review_pending(conn, batch_id=None, cfg=None) -> dict:
    """Run the gate over every pending proposal. Rejections are as much the
    point as promotions -- they are the evidence the gate is load-bearing."""
    cfg = cfg or load()["rule_engine"]
    promoted, rejected, still_pending = [], [], []
    for p in conn.execute("SELECT * FROM rule_proposals WHERE status='pending'"
                          " ORDER BY occurrence_count DESC").fetchall():
        passed, report = check_gate(conn, p, cfg)
        if passed:
            promoted.append(promote(conn, p, report))
        elif set(report["failed_gates"]) <= {"occurrence", "confidence"}:
            # not enough evidence *yet* -- leave it pending for a later batch
            still_pending.append(p["proposal_id"])
        else:
            reject(conn, p, report)
            rejected.append(p["proposal_id"])
    conn.commit()
    return {"promoted": promoted, "rejected": rejected,
            "pending": still_pending}


# --------------------------------------------------------------- retirement
def retire_stale_rules(conn, cfg=None) -> list:
    """A rule that stops being right gets pulled, and every match it produced
    is re-opened as an exception. This is the rollback path: promotion is not
    a one-way door."""
    cfg = cfg or load()["rule_engine"]
    retired = []
    for r in conn.execute(
            "SELECT * FROM rules WHERE status='active' AND times_applied >= ?",
            (cfg["retirement_min_applications"],)).fetchall():
        accuracy = r["times_correct"] / r["times_applied"]
        if accuracy >= cfg["retirement_floor"]:
            continue

        conn.execute("UPDATE rules SET status='retired' WHERE rule_id=?",
                     (r["rule_id"],))
        reopened = 0
        for m in conn.execute("SELECT * FROM matches WHERE rule_id=?",
                              (r["rule_id"],)).fetchall():
            conn.execute(
                "INSERT INTO exceptions (batch_id, record_type, record_id,"
                " reason_code, reason_text, money_at_risk_paise, status)"
                " VALUES (?,?,?,?,?,?, 'open')",
                (m["batch_id"], m["right_type"], m["right_id"],
                 "RULE_RETIRED",
                 f"rule {r['rule_id']} was retired at "
                 f"{accuracy:.2%} accuracy; this match is withdrawn",
                 _money_of(conn, m["right_type"], m["right_id"])))
            conn.execute("DELETE FROM matches WHERE match_id=?", (m["match_id"],))
            reopened += 1

        audit(conn, "retired", rule_id=r["rule_id"],
              detail={"accuracy": round(accuracy, 4),
                      "floor": cfg["retirement_floor"],
                      "times_applied": r["times_applied"],
                      "times_correct": r["times_correct"],
                      "matches_reopened": reopened})
        retired.append(r["rule_id"])
        log.warning("retired rule %s at %.2f%% accuracy; reopened %d matches",
                    r["rule_id"], accuracy * 100, reopened)
    conn.commit()
    return retired


def _money_of(conn, record_type, record_id) -> int:
    table, key, col = {
        "settlement": ("settlements", "settlement_txn_id", "net_amount_paise"),
        "order": ("orders", "order_id", "gross_amount_paise"),
        "bank_credit": ("bank_credits", "utr", "credit_amount_paise"),
    }[record_type]
    row = conn.execute(f"SELECT {col} v FROM {table} WHERE {key}=?",
                       (record_id,)).fetchone()
    return abs(row["v"]) if row else 0


# ------------------------------------------------------------------ plumbing
def process_proposals(conn, batch_id, proposals, cfg=None) -> dict:
    """Called at the end of a batch with whatever the LLM leg proposed."""
    for p in proposals:
        intake(conn, batch_id, p["predicate"], p.get("confidence", 0.0),
               p.get("case_id"))
    conn.commit()
    result = review_pending(conn, batch_id, cfg)
    result["retired"] = retire_stale_rules(conn, cfg)
    return result


def plain_english(predicate: dict) -> str:
    """Render a learned rule the way a controller would say it. Used by the
    dashboard and the Q&A agent."""
    t = predicate.get("type")
    scope = predicate.get("instrument", ALL)
    who = "every instrument" if scope == ALL else scope
    if t == "fee_formula":
        p = predicate.get("params", {})
        if not p.get("flat_paise") and not p.get("rate"):
            return (f"{who}: settles at par -- no MDR is deducted at all, "
                    f"so there is no GST either")
        fee = (f"a flat {p['flat_paise']} paise" if "flat_paise" in p
               else f"{p.get('rate', 0) * 100:g}% of gross")
        gst = p.get("gst", 0)
        return (f"{who}: the aggregator deducts {fee}, plus "
                f"{gst * 100:g}% GST on that fee (never on the gross)")
    if t == "timing_window":
        lo, hi = predicate["min_working_days"], predicate["max_working_days"]
        when = f"{lo} working day" + ("" if lo == 1 else "s")
        if hi != lo:
            when = f"{lo}-{hi} working days"
        return f"{who}: settles {when} after the order"
    if t == "refund_pattern":
        return f"{who}: a net below the fee-implied net indicates a partial refund"
    if t == "narration_pattern":
        return f"bank narrations matching {predicate['regex']} carry the batch id"
    if t == "exact_id":
        return "a settlement claiming an order id matches that order"
    return json.dumps(predicate)
