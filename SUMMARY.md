# recon-agent — project summary

Everything about this project in one place: what it is, what it does, how it
works, what was measured, what broke along the way, and what is still open.

Built for the Razorpay AI Buildathon, AI Finance Controller track, against
`CLAUDE_CODE_BUILD_SPEC.md`. All nine specified phases are complete.

---

## 1. The problem

An Indian merchant on a payment aggregator holds three independent records of
the same money:

| Source | What it is | Granularity |
|---|---|---|
| Order ledger | the merchant's own record | one row per transaction |
| Settlement report | the aggregator's record, with its own txn ids | one row per transaction |
| Bank statement | the merchant's actual bank account | **one row per payout batch** |

They disagree by construction:

- **Fees.** Cards settle at `gross − MDR − 18% GST on the MDR`. UPI carries no
  MDR at all under the Indian government mandate. So the rule differs by
  payment instrument.
- **Timing.** UPI settles T+1, cards T+2 — in *working* days, so a weekend or
  an Indian public holiday stretches T+2 into four or five calendar days.
- **Aggregation.** The bank shows one bulk credit for sixty transactions.
  Recovering the constituents is a subset-sum problem.
- **Mess.** Partial refunds, full refunds, duplicate ledger rows, missing
  settlements, orphan settlements, chargebacks arriving days later as negative
  rows, split payouts, and paise-level rounding drift.

Reconciliation means proving the three agree and explaining exactly why
wherever they do not.

---

## 2. The idea

**The system is not told the fee structure or the settlement timing. It induces
them from the data.**

The deterministic layer starts with exactly one rule — *a settlement claiming an
order id matches that order*. It knows nothing about MDR, GST or settlement lag.
When it cannot explain a record it escalates to a language model, which proposes
a **machine-checkable predicate**, never a decision. Proposals accumulate; one is
promoted into the deterministic layer only after it has been proposed
independently enough times, cleared a confidence floor, and — the gate that
matters — been replayed against every previously-resolved record without
contradicting one. From then on that entire class of mismatch resolves with no
model call at all.

This follows the Hypotheses-to-Theories framework (Zhu et al.,
[arXiv:2310.07064](https://arxiv.org/abs/2310.07064)): induce rules from
examples, filter by occurrence count and association with correct answers, then
apply the surviving library. The promotion gate is that filter, made
unforgiving because the domain is money.

### The architectural principle

> **Algorithms decide matches. The LLM only proposes rules and writes
> explanations. The LLM never unilaterally decides that two records match.**

The model has exactly two jobs, each with a gate on it:

1. **Propose rules** — returns a predicate, which is schema-validated at
   *proposal* time and backtested before it can take effect.
2. **Explain and disambiguate** — a `match` verdict is stamped
   `resolved_by='llm'`, never `exact`, and is discarded outright below the
   confidence floor regardless of what the model claimed.

`insufficient_information` is prompted for explicitly and treated as a valued
answer. A model that never abstains is a model that manufactures false
positives.

---

## 3. Results

```
 batch  excep   llm  /100rec  avoided   match    prec  recall   FP  rules   cost
     1     68    58     44.3       11   44.3%  100.0%   50.4%    0      1     68
     2     19    20     15.3       10   80.2%  100.0%   92.3%    0      5     19
     3     14     5      3.9       12   84.4%  100.0%   98.2%    0      8     14
     4     12     1      0.8       11   85.6%  100.0%   99.2%    0      9     12
   adv      5     5     10.0        0   76.0%  100.0%   79.5%    0      9      5
```

| | batch 1 | batch 4 |
|---|---|---|
| Open exceptions | 68 | **12** |
| LLM calls per 100 records | 44.3 | **0.8** |
| Match rate | 44.3% | 85.6% |
| Recall | 50.4% | **99.2%** |
| Precision | 100% | **100%** |
| False positives | 0 | **0** |
| Active rules | 1 | 9 |

By batch 4 the deterministic layer answers essentially everything: **fewer than
one model call per hundred records**, down from forty-four. That is the whole
thesis — the model is expensive and fallible, so use it to write rules once,
not to make decisions forever.

Precision is 100% on every batch including the adversarial set, which was built
specifically to induce false positives. The adversarial recall of 79.5% is by
design: its `off_by_one_day` records settle a working day outside the learned
window, so they are flagged for review rather than bound. They are genuine
settlements, so refusing them costs recall — and it is the conservative answer.

Batch 4's residual 12 exceptions are 10 orders with no settlement row at all,
1 orphan settlement claiming an order that does not exist, and 1 unexplained
fee. Those are exactly what a controller should look at.

### What it worked out on its own

```
UPI          settles at par -- no MDR is deducted at all, so there is no GST either
CARD_DEBIT   deducts 0.9% of gross, plus 18% GST on that fee (never on the gross)
CARD_CREDIT  deducts 2% of gross, plus 18% GST on that fee (never on the gross)
NETBANKING   deducts a flat 1200 paise, plus 18% GST on that fee
UPI          settles 1 working day after the order
CARD_DEBIT   settles 2 working days after the order
CARD_CREDIT  settles 2 working days after the order
ALL          a net below the fee-implied net indicates a partial refund
```

Eight induced rules plus the one seeded exact-id rule. Every one matches the
generator's ground truth. Those constants appear in no prompt and in no module
outside `generate_data.py` — `tests/test_generate_data.py` greps every pipeline
module and fails the build if they leak.

### Verified against a live model

The induction step was run against **Gemini 3.1 Flash Lite** over batch 1,
57 escalations, no ground truth in the prompt. It induced all four fee formulas
correctly and unaided, and two were promoted through the gate in a single run:

```
rule 2  CARD_DEBIT   {"rate": 0.009, "gst": 0.18}  tol=10
   occurrence 5/3  confidence 1.0  backtest support=9 correct=9 wrong=0 precision=1.000
rule 3  CARD_CREDIT  {"rate": 0.02,  "gst": 0.18}  tol=3
   occurrence 3/3  confidence 1.0  backtest support=8 correct=8 wrong=0 precision=1.000
```

The headline table above is stub-driven and reported separately from this, on
purpose: a one-batch live run cut short by free-tier quota is not the same
evidence as the full sequence, and merging them would overstate it.

---

## 4. How it works

```
SOURCES  orders.csv · settlements.csv · bank_statement.csv
                        ↓
┌─────────────── DETERMINISTIC LAYER (no API calls) ───────────────┐
│  blocking.py       hash join → sorted-array binary search → DSU  │
│  deterministic.py  learned library, topological precedence       │
│  subset_sum.py     payout-batch hash join, then bitset DP        │
│  assignment.py     Hungarian (1:1), min-cost flow (1:N), sink    │
│  calendar_utils.py prefix sums, O(1) working-day lag             │
│  money.py          integer paise, banker's rounding              │
└──────────────────────────┬───────────────────────────────────────┘
                           │  only what survives
                    llm_reasoner.py    Claude/Gemini + verification tools
                           │  proposals, never matches
                    rule_engine.py     THE GATE
                           │  promoted rules ──┐
                        SQLite ────────────────┘ feeds the next batch
                           ↓
        metrics.py · qa_agent.py · dashboard.py
```

### The five matching legs, in order

1. **Split payouts** — one order paid across several settlements. Identified by
   an exact subset sum over *gross*.
2. **Identity** — hash join on the claimed order id, then the amount must be
   explained by a learned fee rule and the lag by a learned timing window.
3. **Assignment** — surviving records decomposed by Union-Find into small
   components, solved optimally.
4. **Bank** — does one payout batch sum exactly to this credit? If not,
   subset-sum searches for the constituents.
5. **Escalation** — only exceptions that survive, and only kinds where a general
   rule could plausibly exist. Everything skipped is counted.

Then **discovery** (rule types that never raise an exception), **rule stats**,
and the **promotion gate**.

### Algorithms, and why each one

| Technique | Where | Why that one |
|---|---|---|
| Union-Find | `blocking.py` | partitions candidate pairs into independent subproblems; components stay size 1–5, which is what keeps the cubic solver downstream irrelevant |
| Bitset DP | `subset_sum.py` | `reachable \|= reachable << amount` tests every partial sum a word at a time; a parallel reachability array recovers the actual subset, not just feasibility |
| Meet-in-the-middle | `subset_sum.py` | fallback for pools too large for the DP range |
| Hungarian | `assignment.py` | greedy binds the wrong twin on the adversarial set; `linear_sum_assignment` optimises the whole component at once |
| Min-cost max-flow | `assignment.py` | the 1:N and N:1 shapes are not square, so Hungarian does not apply |
| Topological sort | `deterministic.py` | orders rules by priority and scope specificity; a cycle means the library contradicts itself, and raises rather than guessing |
| Prefix sums | `calendar_utils.py` | O(1) working-day lag over the Indian holiday calendar, computed on every candidate pair |
| Heap | `qa_agent.py` | top-N exceptions by money at risk without sorting a queue that, in production, is the table that grows |

Nothing else earns its place at this scale. No tries, no Bloom filters, no
segment trees.

### The unmatched sink

Every node in the assignment problem has an edge to an "unmatched" sink priced
at `unmatched_sink_cost`. If no pairing costs less than that sink, the solver
*chooses* to leave the record unmatched. That edge is the mathematical
statement of "be conservative with money", and it is why precision holds at
100%. There is a load-bearing invariant in `config.yaml`:

```
w_rule × unexplained_penalty > 2 × unmatched_sink_cost
```

The factor of two is because declining leaves *two* records unmatched. Break it
and the solver happily binds records it cannot justify.

### The promotion gate

All four must pass:

1. `occurrence_count >= 3` — one case is an anecdote.
2. `avg_llm_confidence >= 0.8`.
3. **Backtest** — replay against every already-resolved record. Support ≥ 3,
   precision ≥ 0.98, and zero contradicted records beyond the noise band.
4. **Conflict** — no cycle in the precedence DAG, no fully-overlapping
   tolerance interval with an active rule of the same type and scope.

Failing *only* the soft gates (occurrence, confidence, or insufficient
backtest support) leaves the proposal **pending** for a later batch.
Insufficient evidence is not the same as proven wrong, and the audit log
records the difference.

### Where the backtest gets its evidence

Two independent sources, unioned:

1. recorded order↔settlement matches, and
2. settlements the **bank leg** confirmed, joined to the order id they claim.

(2) is essential. It needs no knowledge of fees, so it exists from batch 1 —
without it nothing can be matched until a fee rule is promoted, and no fee rule
can gather support until something is matched. Two legs agreeing is real
evidence; one leg agreeing with itself is not.

### Rule discovery

Some rule types can never be induced from the exception queue, because without
the rule there is no question. Settlement timing is the clean case:
`explain_timing` returns None with no window learned, so no exception is raised,
so nothing escalates, so a window can never be proposed. Zero ever were.

`run_discovery_leg` inverts the direction: for an instrument missing a
discoverable rule type, take a few *independent* samples of records already
resolved and ask what pattern they show. Independence matters — three proposals
from disjoint evidence are three real confirmations, which is what the
occurrence gate counts. Bounded per batch, and it stops once the rule is
learned, so the cost goes to zero rather than becoming a permanent tax.

### Cost model

```
cost_weighted_error = 50 × false_positives + 1 × open_exceptions
```

A false positive books money against the wrong record: nothing flags it, no
queue shows it, and it is found — if ever — by an auditor months later. An open
exception appears on a work queue sorted by money at risk and costs a controller
about two minutes.

Fifty to one is a deliberate, defensible guess rather than a measured constant,
and it is a config knob. What matters is that it is expressed in code, not in
the pitch: the unmatched sink, subset-sum reporting ambiguity rather than
tiebreaking a truncated sample, the zero-tolerance backtest, the confidence
floor, and the licence to abstain. Each trades recall for precision.

---

## 5. Safety properties

| Property | How it is enforced |
|---|---|
| No floating-point money | integer paise throughout; all rounding confined to `money.py` with `Decimal` + `ROUND_HALF_EVEN`. Python's `round()` on floats is not safe for money |
| Ground truth never reaches the pipeline | only `metrics.py` may read `truth.csv`; a test greps every other module from both sides |
| Fee constants never leak | a test greps every pipeline module for `0.009`, `0.02`, `0.18`, `1200` and fails the build |
| LLM output is never executed | a proposed `expr` is matched against a known template and computed structurally in Python; the `calculate` tool parses with `ast` and walks the tree. Eight injection attempts are parametrised tests |
| Ambiguity is never guessed | subset-sum escalates when truncated or genuinely tied; the assignment solver may decline |
| A bad rule is reversible | retirement withdraws every match a degraded rule produced and re-opens them as exceptions |
| Credentials never in source | `.env` is gitignored; verified absent from every `.py`, `.md` and `.yaml` |

---

## 6. What broke, and what it taught

Every one of these was found by *running* the system, not by a unit test. They
are recorded because the failures are more informative than the successes.

**The backtest deadlock.** The backtest only counted order↔settlement matches
as history, and batch 1 has none. No fee rule could gather support, so nothing
was promoted, so no match was ever made. The system could never have learned
anything, and every test passed. Fixed by unioning in bank-confirmed evidence.

**Fifteen confident false positives from the bank leg.** Subset-sum hit its
5-solution cap, the true answer was never enumerated, and the "fewest members"
tiebreaker picked an 8-member subset spanning four payout batches. `truncated`
was being set and ignored. Fixed three ways: truncation now escalates, the pool
window narrowed from 3 days to 1, and a cheap structural hash join runs *before*
the search.

**The fee rule checked the settlement against itself.** It used the
settlement's own gross rather than the order's, which proves the aggregator can
subtract and nothing about the pairing. That is how an orphan settlement got
bound to a fully-refunded order.

**Retirement was retiring correct rules.** The first `times_correct` proxy
counted a match wrong if the bank leg had not confirmed it — but subset-sum does
not resolve every credit, so good rules scored ~80% and were pulled, collapsing
batch 3 from 79% to 30%. Replaced with a real contradiction signal. Absence of
confirmation is not evidence of error.

**A keyless clone took seven minutes to fail.** Each of 57 escalations retried
an auth error five times with backoff. Auth failures are now recognised as
unrecoverable; the run finishes in 5.7 seconds with one clear message. The same
later proved necessary for exhausted quota: 48 minutes of pointless retry became
77 seconds.

**The gate could not tell noise from wrongness.** It rejected four *correct* fee
formulas a live model had induced, each contradicted by a single row of
rounding drift at `tolerance_paise: 0`. The backtest now reports the magnitude
of each disagreement, which separates the two by five orders of magnitude:
**3 paise for drift, 147,468 for a wrong rate.**

**Proposals fragmented and could never accumulate.** The fingerprint included
`tolerance_paise`, so a model refining its guess (0 → 1 → 2 → 3) filed four
*different* hypotheses, each stuck below the occurrence threshold forever. The
claim is the formula; the tolerance is a nuisance parameter about noise. That
single change turned "four correct formulas, none promoted" into promotions.

**`False` where `INAPPLICABLE` belonged.** In the matching path the two are
equivalent — both fall through to the next rule. In the backtest, `False` means
"contradicts a resolved record", so a refund rule accrued a counterexample for
every clean settlement in history, 44 out of 44.

**Early rejection was permanent.** A rule proposed before its evidence existed
was rejected and never reconsidered, however much arrived later. Insufficient
support is now a soft gate.

**`pytest` silently overwrote the demo database.** A dashboard fixture called
`reset_db()` with no path. Caught by `AppTest`, which also caught a
`@st.cache_resource` connection shared across Streamlit's rerun threads — that
one would have crashed the live demo on the first interaction.

---

## 7. Repository

```
recon-agent/
├── README.md                 the pitch, results, honest limitations
├── SUMMARY.md                this document
├── config.yaml               62 tunable knobs; no economics
├── requirements.txt
├── .env.example              placeholders for both providers
├── data/                     4 batches + adversarial set
├── db/recon.db               SQLite (gitignored)
├── src/
│   ├── money.py              integer paise, banker's rounding
│   ├── calendar_utils.py     O(1) working-day arithmetic
│   ├── generate_data.py      synthetic data + traps (owns the constants)
│   ├── schema.sql / db.py    storage
│   ├── blocking.py           hash join, amount index, Union-Find
│   ├── subset_sum.py         bitset DP, recovery, ambiguity
│   ├── assignment.py         Hungarian, min-cost flow, sink
│   ├── deterministic.py      predicates, precedence DAG, backtest
│   ├── llm_client.py         provider adapter (Anthropic / Gemini)
│   ├── llm_reasoner.py       bounded escalation with verification tools
│   ├── rule_engine.py        intake, the gate, retirement
│   ├── metrics.py            scoring — the ONLY reader of ground truth
│   ├── qa_agent.py           settlement Q&A over SQLite
│   └── pipeline.py           orchestration
├── app/dashboard.py          Streamlit
├── tests/                    349 tests, 16 files
└── docs/                     ARCHITECTURE.md + two diagrams
```

**Size:** ~4,000 lines of source, ~3,200 lines of tests, 230 lines of dashboard.
**Tests:** 349, none requiring an API key — every provider is stubbed.
**Git:** 3 commits, 48 tracked files.

### Data generated

| | orders | settlements | bank credits |
|---|---|---|---|
| batch 1 | 62 | 58 | 11 |
| batch 2 | 63 | 59 | 9 |
| batch 3 | 63 | 56 | 9 |
| batch 4 | 64 | 59 | 9 |
| adversarial | 22 | 22 | 6 |

The adversarial set carries four trap families designed to induce false
positives: **amount twins** (identical gross and instrument settling a day
apart), **coincidental subsets** (an unrelated subset summing to the same
total), **near-fee traps** (a UPI refund shaped exactly like a card fee), and
**off-by-one-day** (a settlement a working day outside the window). Trap labels
live only in a truth file the pipeline never reads.

---

## 8. Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # set the key for your provider

python -m src.generate_data --batches 4 --seed 42
python -m src.generate_data --adversarial --seed 99

python -m src.pipeline --reset-db
for b in 1 2 3 4 adversarial; do python -m src.pipeline --batch $b; done

python -m src.metrics --report
streamlit run app/dashboard.py
python -m src.qa_agent "What have you learned about this merchant?"
```

`--no-llm` skips escalation entirely. Without a key the pipeline still runs:
escalation disables itself once, with a message, and the deterministic layers do
their work — you get the batch-1 baseline repeated and no rules promoted, which
is the correct cold-start behaviour.

**Providers.** `config.yaml` selects `anthropic` or `gemini`.
`src/llm_client.py` is the whole adapter — both reasoning layers are written
against one small surface (`client.messages.create(...)` returning content
blocks), which is also the surface the test stubs implement, so the tested path
and the shipped path are the same path. Adding a third provider means
implementing that surface and nothing else.

Two Gemini wrinkles the adapter absorbs: it rejects function calling and a JSON
response schema in the same request (so the JSON contract moves into the prompt,
backed by the existing malformed-response retry), and its schema dialect rejects
several JSON Schema keywords (so tool schemas are filtered).

---

## 9. Limitations, stated plainly

- **Scale.** 50–60 records per batch of synthetic data, against production
  volumes in the millions. The algorithms were chosen to scale, but that is an
  argument, not a measurement.
- **A proof of concept, not a competitor.** Commercial auto-match baselines sit
  above 90%. We reach 85.6% match rate at 99.2% recall on synthetic data, having
  started from zero domain knowledge.
- **The headline table is stub-driven.** The live model run proved induction and
  promotion end to end, but on one batch before free-tier quota ran out.
- **`narration_pattern` has never been induced.** The generator deliberately
  writes narrations carrying no batch id, so there is no pattern to find. The
  rule type is implemented and tested but unused — the honest outcome for data
  built to defeat it.
- **Timing rules never accrue `times_applied`.** A match records the *fee* rule
  as its `rule_id`; the timing window is a secondary check. So the retirement
  gate can never evaluate a timing rule. Retirement is tested and correct, but
  it does not currently cover that rule type.
- **Retirement has never fired live**, because no promoted rule has degraded.
  That is the desired state, not a gap, but it means the rollback path is
  exercised only by tests.
- **One merchant profile, one fee schedule.** Batches are thematically
  consistent by design, which is what makes the structure learnable at all.
- **No real bank file parsing.** No MT940, no CAMT.053, no per-bank narration
  dialects.
- **Single currency, single timezone**, no partial-day settlement cutoffs.

---

## 10. What is still open

1. **A full-quota live run** across all four batches, to make the headline table
   a live result rather than a stub-driven one. Needs a Google project with
   roughly 2,000 requests of headroom, or an Anthropic key.
2. **The pitch video.**
3. **Timing rules in the retirement gate** — currently unreachable, per above.
4. **The noise band is a heuristic.** Five paise separates drift from wrongness
   cleanly on this data. A principled version would estimate the drift
   distribution rather than take a constant.
5. **Discovery covers two rule types.** `narration_pattern` would need data that
   actually contains a pattern.
