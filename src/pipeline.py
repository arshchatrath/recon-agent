"""One batch, end to end.

Three legs, and a record is only reconciled when all of them tie out:

  order  <-> settlement    identity, by id or by assignment
  amount <-> fee rule      is the deduction explained by something we learned?
  settlement <-> bank      which rows make up this bulk credit? (subset sum)

The middle leg is why batch 1 is mostly exceptions. The ids line up fine; what
we cannot yet do is say *why* 4,72,000 paise arrived as 4,63,296. Nobody told
us the fee structure, and until the rule engine induces it every one of those
is an honest open question rather than a match.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from datetime import date, timedelta

from src import blocking
from src.assignment import solve_component
from src.calendar_utils import working_days_between
from src.config import load
from src.db import get_conn, ingest_batch, init_db, reset_db
from src.deterministic import INAPPLICABLE, RuleSet, evaluate
from src.llm_reasoner import LLMReasoner
from src.rule_engine import process_proposals
from src.subset_sum import disambiguate, solve

log = logging.getLogger("pipeline")

# A payout hits the bank on, or right beside, the day it settled. Widening
# this does not find more answers -- it floods the subset-sum pool with
# settlements from neighbouring payout batches, and coincidental sums start
# colliding with the real one. 1 day, not 3.
BANK_POOL_WINDOW_DAYS = 1


def _rows(conn, table, batch_id):
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM {table} WHERE batch_id = ?", (batch_id,))]


def _day(v) -> date:
    return date.fromisoformat(str(v)[:10])


class BatchRun:
    def __init__(self, conn, batch_id: str, reasoner=None, use_llm=True):
        self.conn, self.batch_id = conn, batch_id
        self.rules = RuleSet.load(conn)
        self.counts = Counter()
        self.matched_settlements: set[str] = set()
        self.matched_orders: set[str] = set()
        self.use_llm = use_llm
        self.reasoner = reasoner or (LLMReasoner(conn, self.rules)
                                     if use_llm else None)
        self.proposals: list[dict] = []
        self.rule_changes = {"promoted": [], "rejected": [], "pending": [],
                             "retired": []}

    # -------------------------------------------------------------- writes
    def match(self, left_type, left_id, right_type, right_id, kind, by,
              rule_id=None, confidence=None, cost=None, explanation=""):
        self.conn.execute(
            "INSERT INTO matches (batch_id,left_type,left_id,right_type,right_id,"
            "match_kind,resolved_by,rule_id,confidence,cost,explanation)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.batch_id, left_type, left_id, right_type, right_id, kind, by,
             rule_id, confidence, cost, explanation))
        self.counts[kind] += 1
        if left_type == "order":
            self.matched_orders.add(left_id)
        if right_type == "settlement":
            self.matched_settlements.add(right_id)

    def exception(self, record_type, record_id, reason_code, reason_text,
                  money_at_risk, candidates=None):
        self.conn.execute(
            "INSERT INTO exceptions (batch_id,record_type,record_id,reason_code,"
            "reason_text,money_at_risk_paise,candidates_json,status)"
            " VALUES (?,?,?,?,?,?,?,'open')",
            (self.batch_id, record_type, record_id, reason_code, reason_text,
             int(abs(money_at_risk)), json.dumps(candidates or [])))
        self.counts["exceptions"] += 1

    # --------------------------------------------------------------- legs
    def explain_amount(self, order, settlement):
        """Does any active rule account for gross -> net? -> (rule_id | None, why)."""
        for r in self.rules.rules:
            if r.rule_type not in ("fee_formula", "refund_pattern"):
                continue
            v = evaluate(r.predicate, order, settlement, self.rules)
            if v is True:
                return r.rule_id, f"rule {r.rule_id} ({r.rule_type}) explains the net"
            if v is INAPPLICABLE:
                continue
        return None, "no active rule explains the deduction"

    def explain_leg(self, settlement):
        """Is this settlement row internally consistent with a learned fee
        schedule -- its own net against its own gross? Used for split legs,
        where there is no full order to compare against."""
        for r in self.rules.of_type("fee_formula", settlement["instrument"]):
            if evaluate(r.predicate, {}, settlement, self.rules) is True:
                return r.rule_id, f"leg consistent with fee rule {r.rule_id}"
        return None, "no fee rule explains this leg"

    def explain_timing(self, order, settlement):
        window = self.rules.expected_lag(settlement["instrument"])
        lag = working_days_between(_day(order["order_datetime"]),
                                   _day(settlement["settled_datetime"]))
        if window is None:
            return None, lag
        return window[0] <= lag <= window[1], lag

    def run_identity_leg(self, orders, settlements):
        pairs, left_o, left_s = blocking.hash_join(orders, settlements)
        for o, s in pairs:
            rule_id, why = self.explain_amount(o, s)
            timing_ok, lag = self.explain_timing(o, s)
            if rule_id is None:
                # The ids agree, but the money does not yet. Claiming a match
                # here would be claiming we understand a deduction we do not.
                self.exception(
                    "settlement", s["settlement_txn_id"], "FEE_UNEXPLAINED",
                    f"{o['order_id']} settled {s['net_amount_paise']}p against "
                    f"{s['gross_amount_paise']}p gross after {lag} working days; "
                    + why,
                    s["gross_amount_paise"] - s["net_amount_paise"],
                    [{"order_id": o["order_id"], "lag_working_days": lag}])
            elif timing_ok is False:
                self.exception(
                    "settlement", s["settlement_txn_id"], "TIMING_UNEXPLAINED",
                    f"lag of {lag} working days is outside the learned window "
                    f"{self.rules.expected_lag(s['instrument'])}",
                    s["net_amount_paise"],
                    [{"order_id": o["order_id"], "lag_working_days": lag}])
            else:
                self.match("order", o["order_id"], "settlement",
                           s["settlement_txn_id"], "exact", "deterministic",
                           rule_id=rule_id, confidence=0.99,
                           explanation=f"id match; {why}; lag {lag} working days")
        return left_o, left_s

    def run_split_leg(self, orders, settlements):
        """One order paid out across several settlements.

        A fee rule speaks to a full settlement, so a leg covering 40% of an
        order is INAPPLICABLE to it and the assignment solver correctly
        declines -- which left every split payout as an exception and was the
        single largest cause of missed recall.

        The constraint that identifies a split is exact and needs no new rule:
        the legs' GROSS amounts must sum to the order's gross. That is a subset
        sum, and the solver is already here. Requiring an exact sum over gross
        (not net, which carries fees and drift) is what keeps this from binding
        an orphan that merely happens to be smaller than some order.

        -> (leftover_orders, leftover_settlements)
        """
        by_claim: dict[str, list] = {}
        for s in settlements:
            by_claim.setdefault(s["order_id_claimed"], []).append(s)

        used, matched_orders = set(), set()
        for o in orders:
            legs = by_claim.get(o["order_id"], [])
            if len(legs) < 2:
                continue
            result = solve(int(o["gross_amount_paise"]),
                           [(s["settlement_txn_id"], s["gross_amount_paise"])
                            for s in legs], delta=0)
            if not result.found or result.ambiguous:
                continue                     # ambiguous means escalate, not guess
            chosen = set(result.solutions[0])
            if len(chosen) < 2:
                continue
            members = [s for s in legs if s["settlement_txn_id"] in chosen]
            # Every leg must still be internally consistent with a learned fee
            # schedule. Checked leg-against-itself, not leg-against-order: a
            # fee rule is INAPPLICABLE to a partial leg by design, so asking
            # explain_amount(order, leg) here would always say no and the whole
            # split path would never fire.
            if not all(self.explain_leg(s)[0] for s in members):
                continue
            for s in members:
                self.match("order", o["order_id"], "settlement",
                           s["settlement_txn_id"], "assignment", "subset_sum",
                           confidence=0.95,
                           explanation=f"split payout: {len(members)} legs whose "
                                       f"gross sums exactly to "
                                       f"{o['gross_amount_paise']}p")
                used.add(s["settlement_txn_id"])
            matched_orders.add(o["order_id"])

        return ([o for o in orders if o["order_id"] not in matched_orders],
                [s for s in settlements if s["settlement_txn_id"] not in used])

    def run_assignment_leg(self, orders, settlements):
        pairs = blocking.candidate_pairs(orders, settlements)
        extra = ([("order", o, "order_id") for o in orders]
                 + [("settlement", s, "settlement_txn_id") for s in settlements])
        comps = blocking.components(pairs, extra)
        dist = blocking.size_distribution(comps)
        log.info("batch %s: %d components, size distribution %s",
                 self.batch_id, len(comps), dist)

        for comp_orders, comp_settlements in comps:
            matched, uo, us, method = solve_component(
                comp_orders, comp_settlements, self.rules)
            for o, s, cost in matched:
                self.match("order", o["order_id"], "settlement",
                           s["settlement_txn_id"], "assignment", method,
                           confidence=0.9, cost=cost,
                           explanation=f"optimal {method} assignment within a "
                                       f"component of {len(comp_orders)} orders "
                                       f"and {len(comp_settlements)} settlements")
            for o in uo:
                self.exception(
                    "order", o["order_id"], "NO_SETTLEMENT",
                    "no settlement this order could be bound to more cheaply "
                    "than leaving it open",
                    o["gross_amount_paise"],
                    [{"considered": s["settlement_txn_id"]}
                     for s in comp_settlements])
            for s in us:
                self.exception(
                    "settlement", s["settlement_txn_id"], "ORPHAN_SETTLEMENT",
                    f"settlement claims order {s['order_id_claimed']!r}, which is "
                    "not in the ledger, and no order explains it",
                    s["net_amount_paise"],
                    [{"considered": o["order_id"]} for o in comp_orders])
        return dist

    def run_bank_leg(self, settlements, credits):
        """Work out which settlements make up each bulk bank credit.

        Cheap structural join first, expensive search second -- the same order
        blocking.py uses. A payout batch is a real grouping the aggregator
        gives us (settlement_batch_id is in settlements.csv); what nobody tells
        us is which UTR it arrived under, because the narration deliberately
        does not carry it. So the first question is simply "does one payout
        batch sum exactly to this credit?", answered by a hash join.

        Subset-sum then earns its place on the residual: credits no batch sums
        to, because a row is missing, split, or reversed. Running the search on
        every credit instead floods a 30-row pool with settlements from four
        neighbouring payout batches, and coincidental sums collide with the
        real one -- which is how this leg produced 15 confident false positives
        before the pool was narrowed and this join put in front of it.
        """
        groups: dict[str, list] = {}
        for s in settlements:
            groups.setdefault(s["settlement_batch_id"], []).append(s)
        by_sum: dict[int, list[str]] = {}
        for gid, members in groups.items():
            by_sum.setdefault(sum(m["net_amount_paise"] for m in members),
                              []).append(gid)

        for c in credits:
            cd = _day(c["credit_datetime"])
            amount = int(c["credit_amount_paise"])
            hits = [g for g in by_sum.get(amount, [])
                    if abs((_day(groups[g][0]["settled_datetime"]) - cd).days)
                    <= BANK_POOL_WINDOW_DAYS]

            if len(hits) == 1:
                for s in groups[hits[0]]:
                    self.match("bank_credit", c["utr"], "settlement",
                               s["settlement_txn_id"], "exact", "deterministic",
                               confidence=0.99,
                               explanation=f"payout batch {hits[0]} "
                                           f"({len(groups[hits[0]])} settlements) "
                                           f"sums exactly to this credit")
                continue
            if len(hits) > 1:
                self.exception(
                    "bank_credit", c["utr"], "AMBIGUOUS_SUBSET",
                    f"{len(hits)} payout batches each sum exactly to this "
                    f"credit; refusing to pick one", amount,
                    [{"settlement_batch_id": g} for g in hits])
                continue

            self.run_subset_sum_fallback(c, settlements, cd, amount)

    def run_subset_sum_fallback(self, c, settlements, cd, amount):
        """No payout batch matches this credit, so something is split, missing
        or reversed. Search for the constituent subset."""
        pool = [s for s in settlements
                if abs((_day(s["settled_datetime"]) - cd).days)
                <= BANK_POOL_WINDOW_DAYS]
        by_id = {s["settlement_txn_id"]: s for s in pool}
        result = solve(amount, [(s["settlement_txn_id"], s["net_amount_paise"])
                                for s in pool])
        chosen, conf, why = disambiguate(result, by_id)
        if chosen is None:
            self.exception(
                "bank_credit", c["utr"], why,
                f"no payout batch sums to this credit; "
                f"{len(result.solutions)}{'+' if result.truncated else ''} "
                f"subsets of the {len(pool)} nearby settlements hit it",
                amount, [sorted(s) for s in result.solutions])
            return
        for sid in chosen:
            self.match("bank_credit", c["utr"], "settlement", sid,
                       "subset_sum", "subset_sum", confidence=conf,
                       explanation=f"{why}; {len(chosen)} settlements sum to "
                                   f"{amount}p")

    def update_rule_stats(self):
        """Score each rule that fired this batch, for the retirement gate.

        There is no ground truth here -- reading it would be cheating -- so the
        signal has to be a contradiction the data itself exposes: a settlement
        claimed by two different orders, or an order bound to more settlements
        than a split payout allows. An over-broad rule fires on pairs it should
        not and produces exactly those collisions.

        What we deliberately do NOT count as an error is a match the bank leg
        happened not to confirm. Subset-sum does not resolve every credit, and
        scoring a rule down for another leg's silence retires correct rules --
        which is precisely what an earlier version of this method did, tanking
        the match rate in batch 3. Absence of confirmation is not evidence of
        error.
        """
        max_bindings = load()["assignment"]["max_bindings_per_order"]
        contested = {r["right_id"] for r in self.conn.execute(
            "SELECT right_id FROM matches WHERE batch_id=? AND"
            " right_type='settlement' AND left_type='order'"
            " GROUP BY right_id HAVING COUNT(DISTINCT left_id) > 1",
            (self.batch_id,))}
        overbound = {r["left_id"] for r in self.conn.execute(
            "SELECT left_id FROM matches WHERE batch_id=? AND"
            " left_type='order' AND right_type='settlement'"
            " GROUP BY left_id HAVING COUNT(DISTINCT right_id) > ?",
            (self.batch_id, max_bindings))}

        for m in self.conn.execute(
                "SELECT rule_id, left_id, right_id FROM matches WHERE batch_id=?"
                " AND rule_id IS NOT NULL", (self.batch_id,)).fetchall():
            ok = (m["right_id"] not in contested
                  and m["left_id"] not in overbound)
            self.conn.execute(
                "UPDATE rules SET times_applied = times_applied + 1,"
                " times_correct = times_correct + ? WHERE rule_id = ?",
                (1 if ok else 0, m["rule_id"]))
        self.conn.commit()

    # ------------------------------------------------------ LLM escalation
    ESCALATABLE = {"FEE_UNEXPLAINED", "TIMING_UNEXPLAINED", "AMBIGUOUS_SUBSET",
                   "NO_SUBSET_FOUND"}

    def run_llm_leg(self):
        """Only exceptions that survived Phase 3, and only the kinds where a
        general rule could plausibly exist. Everything skipped is counted --
        the number of calls NOT made is the metric this whole design is for."""
        if not self.use_llm or self.reasoner is None:
            return
        for exc in self.conn.execute(
                "SELECT * FROM exceptions WHERE batch_id=? AND status='open'"
                " ORDER BY money_at_risk_paise DESC", (self.batch_id,)).fetchall():
            if exc["reason_code"] not in self.ESCALATABLE:
                self.reasoner.calls_avoided += 1
                continue

            v = self.reasoner.resolve(self.build_case(exc))
            self.counts["llm_cases"] += 1

            if v.error:
                self.conn.execute(
                    "UPDATE exceptions SET reason_code='LLM_UNAVAILABLE',"
                    " reason_text=? WHERE exception_id=?",
                    (f"escalation failed: {v.error}", exc["exception_id"]))
                continue

            if v.proposed_rule is not None:
                self.proposals.append(
                    {"predicate": v.proposed_rule, "confidence": v.confidence,
                     "case_id": f"{exc['record_type']}:{exc['record_id']}",
                     "reasoning": v.reasoning})

            if v.usable:
                self.match(exc["record_type"], exc["record_id"], "settlement",
                           v.matched_candidate_id, "llm_resolved", "llm",
                           confidence=v.confidence, explanation=v.reasoning)
                self.conn.execute(
                    "UPDATE exceptions SET status='resolved', reason_text=?"
                    " WHERE exception_id=?", (v.reasoning, exc["exception_id"]))
                self.counts["exceptions"] -= 1
            else:
                # The model declined, or was not confident enough. The record
                # stays open, with its reasoning attached for the human.
                self.conn.execute(
                    "UPDATE exceptions SET reason_text=? WHERE exception_id=?",
                    (f"{exc['reason_text']} | model: {v.residual_explanation}",
                     exc["exception_id"]))

    # ------------------------------------------------------- rule discovery
    DISCOVERABLE = ("timing_window", "refund_pattern")

    def run_discovery_leg(self):
        """Ask about dimensions that never produce an exception.

        Some rule types can never be induced from the exception queue, because
        without the rule there is no question. Settlement timing is the clean
        example: `explain_timing` returns None when no window has been learned,
        so no TIMING_UNEXPLAINED exception is ever raised, so nothing escalates,
        so a timing rule can never be proposed. Zero were, across every run --
        the same chicken-and-egg as the backtest deadlock, in a different
        place.

        So for any instrument missing a discoverable rule type, take a few
        independent samples of records we HAVE resolved and ask what pattern
        they show. Independent samples matter: three proposals drawn from
        disjoint evidence are three real confirmations, which is exactly what
        the occurrence gate is counting. Bounded per batch, and it stops
        entirely once the rule is learned.
        """
        if not self.use_llm or self.reasoner is None:
            return
        cfg = load().get("discovery", {})
        n_samples = cfg.get("samples_per_batch", 3)
        n_examples = cfg.get("examples_per_sample", 5)

        for rule_type in self.DISCOVERABLE:
            for instrument in self.instruments_seen():
                if self.rules.of_type(rule_type, instrument):
                    continue                     # already known; ask nothing
                pool = self.discovery_pool(rule_type, instrument)
                if len(pool) < n_examples:
                    continue
                min_examples = cfg.get("min_examples_per_sample", 3)
                for i in range(n_samples):
                    sample = pool[i * n_examples:(i + 1) * n_examples]
                    if len(sample) < min_examples:
                        break            # a short tail is fine; a stub is not
                    v = self.reasoner.resolve(self.discovery_case(
                        rule_type, instrument, sample))
                    self.counts["discovery_cases"] += 1
                    if v.error:
                        return                   # service is gone; stop asking
                    if v.proposed_rule is not None:
                        self.proposals.append({
                            "predicate": v.proposed_rule,
                            "confidence": v.confidence,
                            "case_id": f"discovery:{rule_type}:{instrument}:{i}",
                            "reasoning": v.reasoning})

    def instruments_seen(self):
        return [r["instrument"] for r in self.conn.execute(
            "SELECT DISTINCT instrument FROM settlements WHERE batch_id=?"
            " ORDER BY instrument", (self.batch_id,))]

    def discovery_pool(self, rule_type, instrument):
        """Resolved records this rule type could plausibly describe."""
        if rule_type == "timing_window":
            # Only orders settled in ONE payout. A split's later legs settle a
            # day or more after the first, so including them makes different
            # samples observe different windows -- (2,2) here, (2,3) there --
            # and the proposals fragment across fingerprints instead of
            # accumulating. The base settlement rhythm is what is being asked
            # about; a split payout is a separate phenomenon.
            sql = ("SELECT o.order_id, o.order_datetime, o.gross_amount_paise,"
                   " s.settlement_txn_id, s.settled_datetime, s.net_amount_paise"
                   " FROM matches m"
                   " JOIN orders o ON o.order_id = m.left_id"
                   " JOIN settlements s ON s.settlement_txn_id = m.right_id"
                   " WHERE m.batch_id=? AND m.left_type='order'"
                   " AND m.right_type='settlement' AND s.instrument=?"
                   " AND o.status='captured'"
                   " AND o.order_id IN (SELECT left_id FROM matches"
                   "   WHERE batch_id=m.batch_id AND left_type='order'"
                   "   AND right_type='settlement'"
                   "   GROUP BY left_id HAVING COUNT(*) = 1)")
        else:   # refund_pattern -- the ledger says these were partly refunded
            sql = ("SELECT o.order_id, o.order_datetime, o.gross_amount_paise,"
                   " o.status, s.settlement_txn_id, s.settled_datetime,"
                   " s.net_amount_paise FROM settlements s"
                   " JOIN orders o ON o.order_id = s.order_id_claimed"
                   " WHERE s.batch_id=? AND s.instrument=?"
                   " AND o.status='refunded_partial'")
        return [dict(r) for r in self.conn.execute(sql, (self.batch_id, instrument))]

    def discovery_case(self, rule_type, instrument, sample) -> dict:
        examples = []
        for r in sample:
            e = dict(r)
            e["working_day_lag"] = working_days_between(
                _day(r["order_datetime"]), _day(r["settled_datetime"]))
            if self.rules.expected_net(r["gross_amount_paise"], instrument) is not None:
                e["expected_net_under_learned_fee_rule"] = self.rules.expected_net(
                    r["gross_amount_paise"], instrument)
            examples.append(e)
        return {"record": {"instrument": instrument},
                "focus": rule_type,
                "reason_code": f"{rule_type.upper()}_NOT_YET_LEARNED",
                "resolved_examples": examples,
                "candidates": []}

    def build_case(self, exc) -> dict:
        table, key = {
            "settlement": ("settlements", "settlement_txn_id"),
            "order": ("orders", "order_id"),
            "bank_credit": ("bank_credits", "utr"),
        }[exc["record_type"]]
        record = self.conn.execute(
            f"SELECT * FROM {table} WHERE {key}=?", (exc["record_id"],)).fetchone()
        candidates = json.loads(exc["candidates_json"] or "[]")[:5]

        # Precompute the calendar arithmetic; the model should reason about
        # the lag, not recompute Indian public holidays.
        lags = {}
        if exc["record_type"] == "settlement" and record is not None:
            for c in candidates:
                oid = c.get("order_id") or c.get("considered")
                o = self.conn.execute("SELECT * FROM orders WHERE order_id=?",
                                      (oid,)).fetchone() if oid else None
                if o is not None:
                    lags[oid] = working_days_between(
                        _day(o["order_datetime"]), _day(record["settled_datetime"]))

        prior = [dict(r) for r in self.conn.execute(
            "SELECT rule_type, scope_instrument, predicate_json, occurrence_count,"
            " status FROM rule_proposals ORDER BY occurrence_count DESC LIMIT 5")]
        return {"record": dict(record) if record is not None
                          else {"id": exc["record_id"]},
                "candidates": candidates, "lags": lags,
                "reason_code": exc["reason_code"], "prior_proposals": prior}

    # ---------------------------------------------------------------- main
    def run(self):
        t0 = time.time()
        orders = _rows(self.conn, "orders", self.batch_id)
        settlements = _rows(self.conn, "settlements", self.batch_id)
        credits = _rows(self.conn, "bank_credits", self.batch_id)

        # Splits first: a multi-leg order would otherwise be picked up by the
        # identity hash join and filed as FEE_UNEXPLAINED, because no fee rule
        # explains a partial leg against the full order gross.
        left_o, left_s = self.run_split_leg(orders, settlements)
        left_o, left_s = self.run_identity_leg(left_o, left_s)
        dist = self.run_assignment_leg(left_o, left_s)
        self.run_bank_leg(settlements, credits)
        self.run_llm_leg()
        self.run_discovery_leg()
        self.update_rule_stats()
        self.rule_changes = process_proposals(self.conn, self.batch_id,
                                              self.proposals)

        total = len(orders) + len(settlements) + len(credits)
        matched_records = len(self.matched_orders) + len(self.matched_settlements)
        at_risk = self.conn.execute(
            "SELECT COALESCE(SUM(money_at_risk_paise),0) v FROM exceptions"
            " WHERE batch_id=? AND status='open'", (self.batch_id,)).fetchone()["v"]

        self.conn.execute(
            "INSERT INTO run_metrics (batch_id,total_records,exact_matches,"
            "rule_matches,assignment_matches,subset_sum_matches,llm_resolved,"
            "exceptions_count,llm_calls,llm_calls_avoided,llm_tokens_in,"
            "llm_tokens_out,match_rate,money_at_risk_paise,"
            "wall_clock_seconds,active_rules_count,component_sizes_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.batch_id, total, self.counts["exact"], self.counts["rule"],
             self.counts["assignment"], self.counts["subset_sum"],
             self.counts["llm_resolved"], self.counts["exceptions"],
             r.calls if (r := self.reasoner) else 0,
             r.calls_avoided if r else 0, r.tokens_in if r else 0,
             r.tokens_out if r else 0,
             matched_records / total if total else 0,
             at_risk, time.time() - t0, len(self.rules), json.dumps(dist)))
        self.conn.commit()
        return dict(self.counts, total_records=total,
                    rules_promoted=len(self.rule_changes["promoted"]),
                    rules_rejected=len(self.rule_changes["rejected"]),
                    match_rate=round(matched_records / total, 3) if total else 0,
                    active_rules=len(self.rules), money_at_risk_paise=at_risk)


def run_batch(conn, batch_id: str, reasoner=None, use_llm=False) -> dict:
    """`use_llm` defaults off so tests and dry runs never spend money or need a
    key; the CLI turns it on unless --no-llm is passed."""
    ingest_batch(conn, batch_id)
    return BatchRun(conn, batch_id, reasoner=reasoner, use_llm=use_llm).run()


def main(argv=None):
    p = argparse.ArgumentParser(description="reconcile one batch")
    p.add_argument("--batch")
    p.add_argument("--reset-db", action="store_true")
    p.add_argument("--no-llm", action="store_true",
                   help="deterministic layers only; makes no API calls")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    conn = reset_db() if a.reset_db else init_db(get_conn())
    if a.batch:
        summary = run_batch(conn, a.batch, use_llm=not a.no_llm)
        print(f"batch {a.batch}: " + "  ".join(f"{k}={v}" for k, v in summary.items()))
    elif a.reset_db:
        print("database reset")


if __name__ == "__main__":
    main()
