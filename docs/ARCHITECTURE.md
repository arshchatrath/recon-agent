# Architecture

## The pipeline

![architecture](architecture_diagram.png)

The same thing in text, for grepping:

```
             data/batch_n/{orders,settlements,bank_statement}.csv
                                  │
                                  ▼
                          ┌───────────────┐
                          │   ingestion   │  db.py — integer paise only
                          └───────┬───────┘
                                  ▼
        ┌─────────────────────────────────────────────────┐
        │                DETERMINISTIC LAYER              │
        │                                                 │
        │  blocking.py    hash join on order id           │
        │                 sorted-array binary search      │
        │                 Union-Find → components         │
        │                          │                      │
        │  deterministic.py  ◄─────┘  rule library         │
        │                 topological sort of precedence  │
        │                 evaluate(predicate, pair)       │
        │                          │                      │
        │  subset_sum.py   payout-batch hash join         │
        │                 bitset DP + subset recovery     │
        │                 ambiguity → escalate            │
        │                          │                      │
        │  assignment.py   Hungarian (1:1)                │
        │                 min-cost max-flow (1:N)         │
        │                 unmatched sink edge             │
        └─────────────────────────┬───────────────────────┘
                                  │  only what survives
                                  ▼
                        ┌───────────────────┐
                        │  llm_reasoner.py  │  Claude + tools:
                        │                   │   calculate()
                        │  proposes rules   │   check_rule_against_history()
                        │  explains cases   │   get_working_day_lag()
                        │  may ABSTAIN      │
                        └─────────┬─────────┘
                                  │  proposals, never matches
                                  ▼
                        ┌───────────────────┐
                        │  rule_engine.py   │  THE GATE
                        │                   │   occurrence ≥ 3
                        │                   │   confidence ≥ 0.8
                        │                   │   backtest: 0 contradictions
                        │                   │   no DAG cycle, no overlap
                        └─────────┬─────────┘
                                  │  promoted rules
                                  ▼
                        ┌───────────────────┐
                        │      SQLite       │ ──► metrics.py  (reads truth)
                        │  matches          │ ──► qa_agent.py
                        │  exceptions       │ ──► dashboard
                        │  rules + audit    │
                        └───────────────────┘
                                  │
                                  └──── feeds the next batch's rule library
```

The loop at the bottom is the whole idea: what the gate promotes in batch *n*
is deterministic infrastructure in batch *n+1*, so the LLM is asked fewer
questions every batch.

## Rule discovery: learning what never raises an exception

Some rule types can never be induced from the exception queue, because without
the rule there is no question to ask. Settlement timing is the clean case:
`explain_timing` returns None when no window has been learned, so no
`TIMING_UNEXPLAINED` exception is ever raised, so nothing escalates, so a timing
rule can never be proposed. Zero were, across every run — the same
chicken-and-egg as the backtest deadlock, in a different place.

So `run_discovery_leg` inverts the direction. For any instrument missing a
discoverable rule type, it takes a few *independent* samples of records already
resolved and asks what pattern they show. Independence matters: three proposals
drawn from disjoint evidence are three real confirmations, which is exactly what
the occurrence gate counts. It is bounded per batch and stops entirely once the
rule is learned, so the cost falls to zero rather than becoming a permanent tax.

Two things had to be excluded from the timing evidence, both for the same
reason. A split payout's later legs settle a day or more after the first, so
they make different samples observe different windows — (2,2) here, (2,3) there
— and the proposals fragment across fingerprints instead of accumulating. They
also contradict a correct window *in the backtest*. The base settlement rhythm
is what is being asked about; a split payout is a separate phenomenon. Note the
backtest reads split orders from the settlements table rather than the matches
table, because history includes pairs confirmed by the bank leg that were never
matched order-to-settlement — a match-based filter silently misses exactly the
unmatched split legs that break the rule.

## Split payouts

A fee rule speaks to a full settlement, so a leg covering 40% of an order is
`INAPPLICABLE` to it and the assignment solver correctly declines. That left
every split payout as an exception and was the single largest cause of missed
recall (about 5 points).

The constraint that identifies a split is exact and needs no new rule type: the
legs' **gross** amounts sum to the order's gross. That is a subset sum, and the
solver was already in the repository. Summing over gross rather than net is what
makes it safe — net carries fees and rounding drift, so an exact match on gross
is a much stronger claim, and it is what stops an orphan that merely happens to
be smaller than some order from being bound to it. Each leg must additionally be
internally consistent with a learned fee schedule, checked leg-against-itself
rather than leg-against-order.

## Why deterministic-first

**Cost.** An LLM call per record does not survive contact with production
volumes. Batch 1 spends 43.5 calls per 100 records; batch 4 spends 9.8 for
better results. The calls that are *not* made are the point, and they are
counted (`llm_calls_avoided`), not estimated.

**Provability.** A match produced by the Hungarian algorithm is optimal under a
declared cost function, and we can say so. A match produced by a model's
judgement is optimal under nothing. When an auditor asks why two records were
bound together, "rule 4 explains the deduction and the assignment was optimal
within a component of 3 orders and 3 settlements at cost 0.02" is an answer;
"the model was confident" is not.

**Auditability.** Every rule carries the backtest report that let it through,
every rejection carries the counterexamples that blocked it, and every match
carries its rule id and cost. `rule_audit` is append-only.

## Where each algorithm sits, and why that one

**Union-Find (`blocking.py`)** — after the hash join and the amount/date window
filter, the surviving candidate pairs form a sparse graph. DSU with path
compression and union-by-rank partitions it into connected components in near
linear time, and each component becomes an independent assignment problem. In
practice components are size 1–5 (logged every run), which is what keeps the
cubic solver downstream irrelevant to runtime.

**Bitset DP (`subset_sum.py`)** — `reachable |= reachable << amount` tests every
partial sum a machine word at a time. A parallel `prev[k]` array of reachability
states lets us walk the choices back out and recover the actual subset, not just
feasibility. Masking above `target + delta` bounds the integer width by the
target rather than the pool total, so a 45-row payout batch summing to crores is
still cheap. Meet-in-the-middle is implemented as a fallback for pools too large
for the DP range.

**Hungarian vs greedy (`assignment.py`)** — greedy takes the locally cheapest
pair and, on the adversarial set's amount twins, binds the wrong one. That is a
false positive, the expensive error. `scipy.optimize.linear_sum_assignment`
optimises the whole component at once in O(n³), which at component size ≤5 is
free. `test_hungarian_beats_greedy_on_amount_twins` constructs the case and
asserts the difference.

**Min-cost max-flow (`networkx`)** — the 1:N and N:1 shapes (split settlements,
bundled payouts) are not square, so Hungarian does not apply. Capacities encode
how many bindings an order may take.

**Topological sort (`deterministic.py`)** — rules are ordered by two independent
sources of precedence: a lower priority number, and a more specific instrument
scope. Kahn's algorithm produces the evaluation order. If the two sources
disagree in a cycle — a UPI-scoped rule that specificity says wins, at a
priority that says it loses — the library contradicts itself about which rule
applies, and `RuleConflictError` is raised rather than an order guessed.

**Prefix sums (`calendar_utils.py`)** — a working-day count array plus its
inverse (the ordered list of working days) makes both `working_days_between`
and `add_working_days` array lookups. The lag is computed on every candidate
pair, so this is genuinely on the hot path.

**Heap (`qa_agent.list_exceptions`)** — `heapq.nlargest` gets the top N by money
at risk without sorting a queue that, in production, is the table that grows.

Nothing else earns its place at this scale. There are no tries, no Bloom
filters, no segment trees.

## How the LLM's authority is bounded

Two jobs, and a gate on each.

**Job 1 — propose rules.** The model returns a predicate, never a decision. The
predicate is validated against a small schema at *proposal* time, so an
unparseable rule can never reach the apply path. A proposed `expr` string is
matched against a known template and then computed structurally in Python: an
LLM-authored expression is never `eval`'d. The `calculate` tool parses with
`ast` and walks the tree, rejecting anything but arithmetic — eight injection
attempts are parametrised tests.

**Job 2 — explain and disambiguate.** A verdict of `match` is written as
`match_kind='llm_resolved'`, `resolved_by='llm'`, never `exact`. Below the
confidence floor (0.75) the verdict is discarded regardless of what the model
said, and the record stays an open exception with the reasoning attached for a
human. `insufficient_information` is prompted for explicitly and is a valued
answer — a model that never abstains is a model that manufactures false
positives.

The model can also call `check_rule_against_history` to backtest a rule
*before* proposing it. Giving it the same gate it will be judged by is cheaper
than rejecting it afterwards.

## The promotion gate

All four must pass:

1. `occurrence_count >= 3` — one case is an anecdote.
2. `avg_llm_confidence >= 0.8`.
3. **Backtest.** Replay the predicate against every already-resolved record.
   `correct` = it agrees with a recorded match; `wrong` = it contradicts one;
   `INAPPLICABLE` costs nothing. Requires support ≥ 3, precision ≥ 0.98, and
   **zero** contradicted records involving money.
4. **Conflict.** No cycle in the precedence DAG, no fully-overlapping tolerance
   interval with an active rule of the same type and scope.

Failing only gates 1 or 2 leaves the proposal *pending* for a later batch —
insufficient evidence is a different thing from proven wrong, and the audit log
records the difference.

### Where the backtest gets its evidence

This is the subtlest part of the design and it was a bug first. The backtest
initially replayed only against recorded order↔settlement matches — and batch 1
has none, because nothing can be matched until a fee rule exists. No rule could
gather support, so none was promoted, so no match was ever made. The system was
permanently deadlocked and every unit test passed.

The way out is a second, independent source of resolved evidence: settlements
the **bank leg** confirmed. Subset-sum proves which settlements compose a real
bulk credit using nothing but arithmetic on the bank statement, so it works from
batch 1 with an empty rule library. Joining those to the order id they claim
gives the fee rules something to be scored against. Two legs agreeing is real
evidence; one leg agreeing with itself is not.

The backtest also skips pairs whose order status is not `captured`. The
merchant's own ledger says the order was refunded, so a short settlement is
expected — scoring a fee rule down for it would be marking it on a question it
was never asked. That status column is ledger data, not ground truth.

## What is verified, and what is not

The deterministic layers, the gate, the metrics and the storage are exercised
by 349 tests and by full runs over five batches, with the proposal step driven
by a test double.

The proposal step has separately been run live (Gemini 3.1 Flash Lite, batch 1,
57 escalations). It induced the fee formulas correctly and unaided, and two of
them — CARD_DEBIT at 0.9% and CARD_CREDIT at 2%, both plus 18% GST on the fee —
were promoted by the gate at backtest precision 1.000 with zero contradicted
records. The full loop is therefore verified end to end with a live model:
induction, backtest, promotion. The README carries the audit entries.

Live running exposed two things the test suite could not, because the test
double proposed one fixed sensible tolerance and never refined it:

**The gate cannot distinguish "wrong rule" from "right rule, noisy data".** Both
present as a contradicted record. At `tolerance_paise: 0` a correct fee formula
is contradicted by the ~5% of rows carrying a paise or two of rounding drift,
and dies at 0.889 precision. The magnitudes differ enormously — 1 counterexample
in 9 versus 83 in 83 for a deliberately wrong rate — and a future version should
use that rather than treating any contradiction involving money as fatal.

**A hypothesis is the claim, not the noise allowance.** Fingerprinting on the
whole predicate meant a model refining its tolerance (0 → 1 → 2 → 3) filed four
*different* hypotheses, none of which could ever reach the occurrence threshold.
Proposals are now fingerprinted on the claim with tolerances merged to the
widest, capped. This is what converted "four correct formulas, none promoted"
into promotions.

## The failure story

A 2.5% UPI fee rate, proposed four times at 0.95 confidence. Plausible: 2.5% is
a normal card rate, and the case that prompted it — a UPI order partially
refunded by exactly what a card fee plus GST would have taken — looks exactly
like a fee at that rate. That case is `near_fee_trap` in the adversarial set,
built specifically to induce this mistake.

The gate's verdict, from `rule_audit`:

```json
{"predicate": {"type": "fee_formula", "instrument": "UPI",
               "params": {"rate": 0.025, "gst": 0.18}, "tolerance_paise": 2},
 "gates": {"occurrence":  {"value": 4, "required": 3, "passed": true},
           "confidence":  {"value": 0.95, "required": 0.8, "passed": true},
           "backtest":    {"support": 83, "precision": 0.0,
                           "wrong_matches": 83, "passed": false},
           "conflict":    {"passed": true}},
 "failed_gates": ["backtest"],
 "counterexamples": [
   {"order_id": "ORD-1-0003", "gross_amount_paise": 1507323,
                              "net_amount_paise": 1507323},
   {"order_id": "ORD-1-0011", "gross_amount_paise": 1611167,
                              "net_amount_paise": 1611167}]}
```

Two gates passed. The rule was proposed often enough and confidently enough.
The backtest found 83 already-resolved UPI settlements that arrived at exactly
their gross amount — every one of which the rule declares wrong. Precision 0.0.
Rejected, logged, and surfaced on the dashboard's *Rules the gate BLOCKED*
panel.

Without gate 3 this rule goes live and silently mis-explains every UPI
settlement from that batch onward, each one a confident wrong answer in a
ledger. That is the failure mode the entire architecture exists to prevent.

## Cost model

`cost_weighted_error = 50 × false_positives + 1 × open_exceptions`

A false positive books money against the wrong record. Nothing flags it, no
queue shows it, and it is found — if ever — by an auditor months later, at
which point the investigation costs orders of magnitude more than the
transaction. An open exception appears on a work queue sorted by money at risk
and costs a controller about two minutes.

Fifty to one is a deliberate, defensible guess at that asymmetry rather than a
measured constant, and it is a `config.yaml` knob. What matters is that it is
*large*, and that it is expressed in the code rather than in the pitch:

- the unmatched sink edge, priced so an unexplained pair costs more than
  declining it (`w_rule × unexplained_penalty > 2 × unmatched_sink_cost`, an
  invariant with a comment and a test);
- subset-sum reporting ambiguity rather than tiebreaking a truncated sample;
- the zero-tolerance backtest gate;
- the confidence floor that discards the model's own `match` verdict;
- `insufficient_information` prompted for as a valued answer.

Each of those trades recall for precision. The measured result is 88.9% recall
at 100% precision with zero false positives across five runs, including the
adversarial set. We would rather hand a controller 24 open questions than one
silent error.
