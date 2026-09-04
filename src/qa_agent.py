"""Settlement Q&A. Claude with tool use over the reconciled SQLite database.

The rule here is narrower than "don't hallucinate": the model may not state any
figure it did not receive from a tool, and every answer must cite the record
ids it relied on. A reconciliation assistant that rounds a number in its head
is worse than no assistant, because it sounds exactly as confident as one that
looked it up.

"I don't have that in the reconciled data" is a correct answer and the prompt
says so explicitly.
"""
from __future__ import annotations

import heapq
import json
import logging
from datetime import date

from src.config import load
from src.db import get_conn
from src.money import format_paise
from src.rule_engine import plain_english

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the settlement reconciliation assistant for an Indian merchant using a \
payment aggregator. You answer questions about reconciled data by calling tools.

Absolute rules:

1. Never state a number, amount, date, id or count that a tool did not return \
   to you in this conversation. Not an estimate, not a rounding, not a total \
   you worked out yourself unless a tool returned every term of it.
2. End every answer with the record ids you relied on, on their own line, \
   prefixed "Sources: ". If you used no records, say "Sources: none".
3. If the tools do not contain the answer, reply with exactly this sentence:
   I don't have that in the reconciled data
   Then stop. Do not guess, and do not offer a plausible figure with a \
   caveat -- a caveated wrong number is still a wrong number.
4. Amounts come back as integer paise plus a formatted rupee string. Quote the \
   formatted string. Never convert paise to rupees yourself.
5. This system learned the merchant's fee and timing structure from the data \
   rather than being told it. When asked what you know about the merchant, \
   call list_learned_rules and report what was actually induced, including how \
   many records back each rule and when it was promoted.

Be brief and concrete. A controller is reading this between other tasks.
"""

TOOLS = [
    {"name": "get_transaction",
     "description": "Look up one record by order id, settlement txn id or UTR, "
                    "with its match and exception state.",
     "input_schema": {"type": "object",
                      "properties": {"record_id": {"type": "string"}},
                      "required": ["record_id"]}},
    {"name": "list_exceptions",
     "description": "Open reconciliation exceptions, largest money at risk "
                    "first.",
     "input_schema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["open", "resolved", "escalated"]},
         "min_money_at_risk_paise": {"type": "integer"},
         "batch_id": {"type": "string"},
         "limit": {"type": "integer"}}}},
    {"name": "explain_match",
     "description": "Why one match was made: the rule that produced it, its "
                    "cost, and what else was considered.",
     "input_schema": {"type": "object",
                      "properties": {"match_id": {"type": "integer"}},
                      "required": ["match_id"]}},
    {"name": "sum_fees",
     "description": "Total MDR and GST the aggregator deducted over a date "
                    "range, optionally for one instrument.",
     "input_schema": {"type": "object", "properties": {
         "date_from": {"type": "string"}, "date_to": {"type": "string"},
         "instrument": {"type": "string"}},
         "required": ["date_from", "date_to"]}},
    {"name": "list_learned_rules",
     "description": "The rule library this system induced from the data, in "
                    "plain English, with backtest stats and promotion dates. "
                    "Also returns rules that were rejected and why.",
     "input_schema": {"type": "object", "properties": {
         "include_rejected": {"type": "boolean"}}}},
    {"name": "trace_bulk_credit",
     "description": "A bank credit and the individual settlements that were "
                    "disaggregated out of it.",
     "input_schema": {"type": "object",
                      "properties": {"utr": {"type": "string"}},
                      "required": ["utr"]}},
    {"name": "check_contract_compliance",
     "description": "Audit the fees actually deducted against the merchant's "
                    "contracted rates. Returns per-instrument agreement, total "
                    "fee leakage in rupees, and the worst individual "
                    "transactions. Use this for any question about being "
                    "overcharged, fee correctness, or money leaking.",
     "input_schema": {"type": "object",
                      "properties": {"batch_id": {"type": "string"}}}},
    {"name": "get_batch_metrics",
     "description": "Run metrics for one batch, or the learning curve across "
                    "all batches if batch_id is omitted.",
     "input_schema": {"type": "object",
                      "properties": {"batch_id": {"type": "string"}}}},
]


def _money(p) -> dict:
    p = int(p or 0)
    return {"paise": p, "formatted": format_paise(p)}


class SettlementQA:
    def __init__(self, conn=None, client=None, model=None):
        self.conn = conn or get_conn()
        self._client = client
        self.cfg = load()["llm"]
        self.model = model or self.cfg["model"]
        self.calls = self.tokens_in = self.tokens_out = 0

    @property
    def client(self):
        if self._client is None:
            from src.llm_client import make_client
            self._client = make_client(self.cfg.get("provider", "anthropic"))
        return self._client

    # ------------------------------------------------------------- the tools
    def get_transaction(self, record_id: str) -> dict:
        for table, key in (("orders", "order_id"),
                           ("settlements", "settlement_txn_id"),
                           ("bank_credits", "utr")):
            row = self.conn.execute(f"SELECT * FROM {table} WHERE {key}=?",
                                    (record_id,)).fetchone()
            if row is None:
                continue
            rec = dict(row)
            for k in list(rec):
                if k.endswith("_paise"):
                    rec[k] = _money(rec[k])
            return {"found": True, "record_type": table[:-1], "record": rec,
                    "matches": [dict(m) for m in self.conn.execute(
                        "SELECT * FROM matches WHERE left_id=? OR right_id=?",
                        (record_id, record_id))],
                    "exceptions": [dict(e) for e in self.conn.execute(
                        "SELECT * FROM exceptions WHERE record_id=?",
                        (record_id,))]}
        return {"found": False,
                "note": f"{record_id!r} is not in the reconciled data"}

    def list_exceptions(self, status="open", min_money_at_risk_paise=0,
                        batch_id=None, limit=20) -> dict:
        """Triage order. A heap because we want the top N by money at risk
        without sorting the whole queue -- in production this table is the
        thing that grows."""
        sql = "SELECT * FROM exceptions WHERE money_at_risk_paise >= ?"
        params = [int(min_money_at_risk_paise or 0)]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if batch_id:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        rows = self.conn.execute(sql, params).fetchall()
        top = heapq.nlargest(int(limit or 20), rows,
                             key=lambda r: r["money_at_risk_paise"])
        return {"count_matching": len(rows), "returned": len(top),
                "total_money_at_risk": _money(
                    sum(r["money_at_risk_paise"] for r in rows)),
                "exceptions": [
                    {"exception_id": r["exception_id"], "batch_id": r["batch_id"],
                     "record_type": r["record_type"], "record_id": r["record_id"],
                     "reason_code": r["reason_code"], "reason": r["reason_text"],
                     "money_at_risk": _money(r["money_at_risk_paise"]),
                     "status": r["status"],
                     "candidates_considered": json.loads(r["candidates_json"]
                                                         or "[]")}
                    for r in top]}

    def explain_match(self, match_id: int) -> dict:
        m = self.conn.execute("SELECT * FROM matches WHERE match_id=?",
                              (int(match_id),)).fetchone()
        if m is None:
            return {"found": False,
                    "note": f"no match {match_id} in the reconciled data"}
        out = {"found": True, "match": dict(m)}
        if m["rule_id"]:
            r = self.conn.execute("SELECT * FROM rules WHERE rule_id=?",
                                  (m["rule_id"],)).fetchone()
            if r:
                out["rule"] = {
                    "rule_id": r["rule_id"],
                    "plain_english": plain_english(json.loads(r["predicate_json"])),
                    "promoted_at": r["promoted_at"],
                    "times_applied": r["times_applied"],
                    "times_correct": r["times_correct"]}
        out["alternatives_considered"] = [
            dict(e) for e in self.conn.execute(
                "SELECT candidates_json FROM exceptions WHERE record_id=?",
                (m["right_id"],))]
        return out

    def sum_fees(self, date_from: str, date_to: str, instrument=None) -> dict:
        sql = ("SELECT COUNT(*) n, COALESCE(SUM(mdr_paise),0) mdr,"
               " COALESCE(SUM(gst_on_mdr_paise),0) gst,"
               " COALESCE(SUM(gross_amount_paise),0) gross,"
               " COALESCE(SUM(net_amount_paise),0) net FROM settlements"
               " WHERE date(settled_datetime) BETWEEN ? AND ?")
        params = [str(date_from)[:10], str(date_to)[:10]]
        if instrument:
            sql += " AND instrument = ?"
            params.append(instrument)
        r = self.conn.execute(sql, params).fetchone()
        return {"date_from": params[0], "date_to": params[1],
                "instrument": instrument or "ALL",
                "settlement_count": r["n"], "gross": _money(r["gross"]),
                "mdr": _money(r["mdr"]), "gst_on_mdr": _money(r["gst"]),
                "total_deducted": _money(r["mdr"] + r["gst"]),
                "net": _money(r["net"])}

    def list_learned_rules(self, include_rejected=True) -> dict:
        active = []
        for r in self.conn.execute(
                "SELECT * FROM rules WHERE status='active' ORDER BY priority"):
            pred = json.loads(r["predicate_json"])
            audit = self.conn.execute(
                "SELECT detail_json FROM rule_audit WHERE event='promoted'"
                " AND rule_id=?", (r["rule_id"],)).fetchone()
            bt = json.loads(audit["detail_json"])["backtest"] if audit else None
            active.append({
                "rule_id": r["rule_id"], "type": r["rule_type"],
                "instrument": r["scope_instrument"],
                "plain_english": plain_english(pred),
                "promoted_at": r["promoted_at"],
                "was_induced_from_data": bool(r["promoted_from_proposal_id"]),
                "times_applied": r["times_applied"],
                "times_correct": r["times_correct"],
                "backtest": {"records_agreed": bt["correct_matches"],
                             "records_contradicted": bt["wrong_matches"],
                             "precision": round(bt["precision"], 4)}
                if bt else None})

        out = {"active_rules": active, "active_count": len(active)}
        if include_rejected:
            rejected = []
            for p in self.conn.execute(
                    "SELECT * FROM rule_proposals WHERE status='rejected'"
                    " ORDER BY occurrence_count DESC LIMIT 10"):
                rejected.append({
                    "proposal_id": p["proposal_id"],
                    "plain_english": plain_english(json.loads(p["predicate_json"])),
                    "times_proposed": p["occurrence_count"],
                    "rejected_because": p["rejection_reason"]})
            out["rejected_proposals"] = rejected
            out["rejected_count"] = len(rejected)
        return out

    def trace_bulk_credit(self, utr: str) -> dict:
        credit = self.conn.execute("SELECT * FROM bank_credits WHERE utr=?",
                                   (utr,)).fetchone()
        if credit is None:
            return {"found": False,
                    "note": f"no bank credit {utr!r} in the reconciled data"}
        members = self.conn.execute(
            "SELECT s.*, m.confidence, m.explanation FROM matches m"
            " JOIN settlements s ON s.settlement_txn_id = m.right_id"
            " WHERE m.left_type='bank_credit' AND m.left_id=?", (utr,)).fetchall()
        constituents = [{
            "settlement_txn_id": s["settlement_txn_id"],
            "order_id_claimed": s["order_id_claimed"],
            "settled_datetime": s["settled_datetime"],
            "instrument": s["instrument"],
            "net": _money(s["net_amount_paise"]),
            "confidence": s["confidence"]} for s in members]
        total = sum(s["net_amount_paise"] for s in members)
        return {"found": True, "utr": utr,
                "credit_amount": _money(credit["credit_amount_paise"]),
                "credit_datetime": credit["credit_datetime"],
                "narration": credit["narration"],
                "constituent_count": len(constituents),
                "constituents": constituents,
                "constituents_total": _money(total),
                "reconciles_exactly": total == credit["credit_amount_paise"],
                "how_it_was_resolved": members[0]["explanation"] if members
                else "not yet disaggregated"}

    def check_contract_compliance(self, batch_id=None) -> dict:
        """Contracted rates vs what was actually deducted."""
        from src.contract import compare_to_contract, leakage_report
        rows = compare_to_contract(self.conn, batch_id)
        lk = leakage_report(self.conn, batch_id)
        return {
            "batch_id": batch_id or "all batches",
            "per_instrument": [
                {"instrument": r["instrument"], "contracted": r["contracted"],
                 "observed_in_data": r.get("observed", "no data"),
                 "transactions": r["samples"],
                 "matches_contract": r["agrees"]} for r in rows],
            "instruments_deviating": [r["instrument"] for r in rows
                                      if r["agrees"] is False],
            "total_fee_leakage": lk["total_leaked"],
            "total_fee_leakage_paise": lk["total_leaked_paise"],
            "transactions_overcharged": lk["transactions_overcharged"],
            "transactions_audited": lk["transactions_checked"],
            "worst_transactions": [
                {**w, "charged": _money(w["charged_paise"])["formatted"],
                 "agreed": _money(w["agreed_paise"])["formatted"],
                 "over_by": _money(w["leaked_paise"])["formatted"]}
                for w in lk["worst_offenders"][:5]],
        }

    def get_batch_metrics(self, batch_id=None) -> dict:
        from src.metrics import learning_curve, score_batch
        if batch_id:
            s = score_batch(self.conn, str(batch_id))
            s.pop("false_positives", None)     # detail, not summary
            return s
        return {"learning_curve": learning_curve(self.conn)}

    # ------------------------------------------------------------- dispatch
    def run_tool(self, name, args) -> str:
        fn = getattr(self, name, None)
        if fn is None or name not in {t["name"] for t in TOOLS}:
            return json.dumps({"error": f"no such tool {name}"})
        try:
            return json.dumps(fn(**args), default=str)
        except TypeError as e:
            return json.dumps({"error": f"bad arguments for {name}: {e}"})
        except Exception as e:                       # a broken tool is data
            log.exception("tool %s failed", name)
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    # ----------------------------------------------------------------- ask
    def ask(self, question: str, max_turns=10) -> dict:
        messages = [{"role": "user", "content": question}]
        used_tools = []
        for _ in range(max_turns):
            try:
                resp = self.client.messages.create(
                    model=self.model, max_tokens=4096,
                    system=SYSTEM_PROMPT, tools=TOOLS, messages=messages)
            except Exception as e:
                unauthenticated = "authentication" in str(e).lower() or \
                    type(e).__name__ in ("AuthenticationError",
                                         "PermissionDeniedError")
                note = ("Set ANTHROPIC_API_KEY to enable chat. The reconciled "
                        "data itself is available without it -- try "
                        "`python -m src.metrics --report`."
                        if unauthenticated else
                        "The reasoning service is unreachable right now.")
                return {"answer": f"I have no answer to give you. {note}",
                        "error": f"{type(e).__name__}: {e}",
                        "tools_used": used_tools}
            self.calls += 1
            if getattr(resp, "usage", None):
                self.tokens_in += resp.usage.input_tokens or 0
                self.tokens_out += resp.usage.output_tokens or 0

            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content
                         if getattr(b, "type", "") == "tool_use"]
            if not tool_uses:
                text = "\n".join(b.text for b in resp.content
                                 if getattr(b, "type", "") == "text")
                return {"answer": text, "tools_used": used_tools,
                        "calls": self.calls}
            results = []
            for tu in tool_uses:
                used_tools.append({"tool": tu.name, "input": tu.input})
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": self.run_tool(tu.name, tu.input)})
            messages.append({"role": "user", "content": results})

        return {"answer": "I could not answer that within the tool budget.",
                "tools_used": used_tools, "calls": self.calls}


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="ask about the reconciled data")
    p.add_argument("question", nargs="*")
    a = p.parse_args(argv)
    qa = SettlementQA()
    q = " ".join(a.question) or "What have you learned about this merchant?"
    print(f"Q: {q}\n")
    print(qa.ask(q)["answer"])


if __name__ == "__main__":
    main()
