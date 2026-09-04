# Review request: is this solving a real problem, or manufacturing one?

You are being asked to critically evaluate a software project. **Your job is
not to be encouraging.** The person asking suspects this may be a solution in
search of a problem, and wants that suspicion tested honestly rather than
soothed.

Read the whole brief before answering. It deliberately includes the facts that
argue *against* the project, because a review that only sees the pitch is
worthless.

---

## 1. What was built

A reconciliation system for an Indian merchant using a payment aggregator
(Razorpay). It takes three records of the same money and proves they agree:

1. the merchant's own **order ledger** (one row per transaction)
2. the aggregator's **settlement report** (one row per transaction, its own ids)
3. the merchant's **bank statement** (one row per *payout batch* — a bulk credit
   representing many transactions)

They disagree because of fees (MDR plus 18% GST on the MDR, differing by payment
instrument), settlement timing (T+1 for UPI, T+2 for cards, in *working* days
against the Indian holiday calendar), and aggregation (the bank shows one lump
sum for ~60 transactions). Plus partial refunds, full refunds, duplicate ledger
rows, missing settlements, orphan settlements, chargebacks arriving later as
negative rows, split payouts, and paise-level rounding drift.

The output, per record, is either a match with a machine-readable reason, or an
exception flagged for a human with an explanation.

## 2. The distinguishing claim

**The system is not told the fee structure or the settlement timing. It induces
them from the data.**

It starts with exactly one rule: "a settlement claiming an order id matches that
order." It knows nothing about fees. When it cannot explain a record, it
escalates to an LLM, which proposes a *machine-checkable predicate* — never a
decision. A proposed rule is promoted into the deterministic layer only after:

- it has been independently proposed at least 3 times,
- average model confidence ≥ 0.8,
- and it has been replayed against every previously-resolved record without
  contradicting one of them,
- and it creates no cycle or overlap in the existing rule set.

Stated principle: *algorithms decide matches; the LLM only proposes rules and
writes explanations; the LLM never unilaterally decides that two records match.*

Deterministic components: hash-join blocking, Union-Find decomposition, bitset-DP
subset sum for disaggregating bulk credits, Hungarian algorithm and min-cost
max-flow for optimal pairing (with an "unmatched" sink edge so the solver may
decline), topological sort for rule precedence, prefix sums for O(1) working-day
arithmetic.

## 3. What was measured

Four sequential batches of ~130 records each, plus an adversarial set built to
induce false positives.

```
 batch  exceptions  LLM calls/100 rec  match rate  precision  recall  false positives
     1          68               44.3       44.3%     100.0%   50.4%                0
     2          19               15.3       80.2%     100.0%   92.3%                0
     3          14                3.9       84.4%     100.0%   98.2%                0
     4          12                0.8       85.6%     100.0%   99.2%                0
```

It induced, unaided: UPI = 0% MDR, debit = 0.9%, credit = 2%, netbanking = flat
₹12, all + 18% GST on the fee; UPI settles T+1, cards T+2; and "a net below the
fee-implied net indicates a partial refund." These match ground truth exactly.

A live model (Gemini 3.1 Flash Lite) independently induced the same fee formulas
and had two promoted through the gate. It also once proposed "UPI charges 2.5%",
which the gate rejected after finding 83 already-resolved UPI settlements that
arrived with no fee at all.

~4,000 lines of source, ~3,200 lines of tests, 349 tests.

---

## 4. Facts that argue AGAINST the project

Weigh these seriously. They are the reason this review was commissioned.

**The data is entirely synthetic, and we wrote the generator.** Every difficulty
the system handles — the fee structure, the timing, the refunds, the chargebacks,
the rounding drift — was invented by the same project that then solves it. The
generator and the solver were written by the same author in the same week. This
is the strongest form of "creating a problem to fix it," and no amount of
internal test rigour addresses it.

**The fee schedule is not actually a secret.** A merchant's MDR rates are in
their Razorpay contract and on Razorpay's public pricing page. Settlement timing
(T+1/T+2) is documented. So the headline capability — *discovering* these — may
be solving a problem nobody has. A four-line config file would encode the same
knowledge with no LLM, no gate, and no risk.

**Razorpay already provides reconciliation tooling.** The aggregator publishes
settlement reports specifically designed to be reconciled against, with
transaction-level fee breakdowns already itemised (our own synthetic data
includes `mdr_paise` and `gst_on_mdr_paise` columns — meaning the fee is *given
to us*, and we elaborately re-derive it).

**Mature commercial products exist.** Enterprise reconciliation software is a
decades-old category with auto-match rates reported above 90%. This reaches
85.6% match rate on 130 synthetic records.

**Scale is unproven.** ~130 records per batch versus production volumes in the
millions. The algorithms were chosen with scale in mind, but nothing here
measures it.

**The learning may be economically pointless.** The system spends ~58 LLM calls
in batch 1 to discover four numbers that a human could type in once, in a minute,
from a contract. The "LLM calls drop to 0.8 per 100 records" curve is impressive
as a graph, but the total is still greater than zero, versus zero for a config
file.

---

## 5. What we think the steelman is

Do not accept these; test them.

- Contracted rates and *actual* deducted rates diverge in practice (promotional
  tiers, network-specific rates, per-bank netbanking fees, mid-contract changes).
  A system that reads what actually happened may catch what a config file cannot.
- The reconciliation problem remains real even if the fees are known: the bank
  leg (one credit ↔ many transactions) is genuinely a search problem, and
  refunds/chargebacks/splits genuinely need resolving.
- The gate is arguably the real contribution — a general pattern for letting an
  LLM contribute to a financial system without letting it decide anything. The
  fee induction is a demonstration vehicle for that pattern.
- The measured behaviour on the adversarial set (100% precision, zero false
  positives, including refusing to guess when a bulk credit had multiple valid
  decompositions) is a property most LLM-in-the-loop systems do not have.

---

## 6. Questions to answer

Answer each directly. Where you don't know, say so.

1. **Is the underlying problem real?** Do Indian merchants on payment aggregators
   actually spend meaningful effort reconciling these three sources, or is this
   largely solved by existing tooling?

2. **Is the specific capability — inducing the fee and timing rules — valuable,
   or is it solving a non-problem?** Would a config file be strictly better?

3. **How much does the synthetic data invalidate the results?** Is there anything
   in the measured numbers that survives the objection "you wrote the exam"?

4. **What would this system do on real data that it does not do here?** Name the
   specific things most likely to break first.

5. **Is the promotion gate a genuine contribution**, or ceremony around a problem
   that could be avoided by not using an LLM at all?

6. **Who, concretely, would use this?** If nobody, say nobody. If someone,
   describe them and what they use today.

7. **What is the single strongest argument that this should not exist?**

---

## 7. How to answer

- Lead with a verdict, then justify it. Do not build up to it.
- Be specific. "Interesting approach with some limitations" is a non-answer.
- If the honest verdict is "this is a well-engineered solution to a problem that
  does not need solving," say exactly that. That outcome is useful and is the
  reason this document exists.
- If you think it is worthwhile, say what the actual value is in one sentence
  that would survive a sceptical reader.
- Do not soften. The author would rather find out now.

End with:

```
VERDICT: [real problem, well solved / real problem, wrong solution /
          manufactured problem / genuinely useful but mis-pitched / other]
ONE-LINE REASON:
STRONGEST OBJECTION:
WOULD YOU BUILD IT: yes / no / only if <condition>
```
