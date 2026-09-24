# recon-agent: how the code works, in plain language

This guide explains the project from start to finish, in the order things happen
when it runs. It keeps the real file and function names so you can open them
during a demo, but it explains every idea in everyday words.

---

## The project in 30 seconds

A shop (the **merchant**) sells things online. For every sale there are three
separate records of the same money:

1. **The shop's own list of orders.** "Customer X paid ₹4,015.97."
2. **The payment company's report** (like Razorpay). "We collected ₹4,015.97,
   kept ₹94.78 as our fee, and sent you ₹3,921.19."
3. **The bank statement.** "You received ₹1,25,100.52 today." This is one big
   lump that bundles many sales together.

These three never line up neatly. Fees get cut, money arrives a few days late
(weekends and holidays make it later), and many sales arrive as one lump.
**Reconciliation** means proving that all three agree, and pointing out exactly
where they don't.

**What this project does:**
- Matches the three records using ordinary code (maths and rules).
- Checks that the payment company charged the fee the shop *agreed to* in its
  contract, and reports any overcharge in rupees.
- Uses an AI model only to **suggest** new rules, such as "credit cards are
  charged 2% plus 18% tax on that fee". A suggested rule is used only after
  it passes a strict test against past data. **The AI is never allowed to
  decide that two records match.**

---

## Why this was needed: before and now

### The problem

Every business that takes online payments has to answer three questions,
over and over:

1. **Did I get paid for every sale?**
2. **Did the payment company take the right fee?**
3. **Did the money actually reach my bank?**

Answering these is hard because the three records never line up neatly:

- **The fee is already cut.** You sell for ₹4,015.97 but receive ₹3,921.19,
  and the fee is different for cards, UPI and netbanking.
- **Money arrives late.** It comes 1 or 2 working days later, and more calendar
  days when there is a weekend or holiday in between.
- **Payments are bundled.** The bank shows one deposit of ₹1,25,100.52 for
  dozens of sales, with no list of which ones.
- **Messy cases.** Refunds, half-refunds, payments split into two parts,
  chargebacks, duplicate entries, and payments that name an order that doesn't
  exist.

If nobody checks carefully, two kinds of money go missing quietly:
- **Sales that were never paid out.**
- **Fees higher than the contract allows.** A small overcharge (2.1% instead of
  2%) is invisible to the eye but adds up over thousands of sales.

### How it's usually done today (without a tool like this)

- **By hand in Excel.** Someone from finance downloads the three files and
  matches them row by row, with lookups and filters. It is slow, it has to be
  redone every week or month, and mistakes are easy.
- **Bundled bank deposits are the painful part.** Working out which sales make
  up a lump deposit is a trial-and-error puzzle by hand.
- **Fees usually aren't checked line by line.** People check that "the money
  roughly arrived", not that each fee matches the contract. So overcharges slip
  through.
- **Auto-matching software** exists, but someone has to write and maintain its
  matching rules by hand, and every new fee or payment method means updating
  them.
- **Using AI to match everything** is the newer temptation. It is fast to
  build, but the AI can be confidently wrong, and a wrong match in accounts is
  worse than no match.

### How this project itself used to work (earlier versions)

The project went through versions that were wrong in instructive ways. These
are good stories for the interview, because each one shows why the current
design is the way it is.

**Mistake 1: learning the fees from the payment company's own data.**
- **Before:** the system looked at what the payment company deducted and
  *learned* that as "the fee".
- **Problem:** if the company quietly charged 2.1% instead of the agreed 2%,
  the system would learn "2.1%", decide everything looked fine, and **approve
  every overcharge**, while reporting 100% accuracy.
- **Now:** the agreed rates from the contract are an **input**
  (`config.yaml → contract`). Every batch compares the actual fee against the
  agreed fee (`contract.py`). In the batch-5 test, the company raised its
  rates, and the system refused the new rates and reported ₹399.33 in
  overcharges across 17 sales.

**Mistake 2: letting the AI make matches.**
- **Before:** if the AI said "these match" with high confidence, the system
  saved it as a match.
- **Problem:** in a real run, **all** the wrong matches came from the AI (53 in
  the full run, 39 in batch 1 alone), each with confidence 1.0. The AI was only
  checking that the payment's own numbers added up, which doesn't prove which
  order it belongs to.
- **Now:** the AI's "match" opinion is **only a note** for a human
  (`pipeline.py:403`). The next live run had **zero wrong matches**, and no
  correct matches were lost.

**Other things that broke and were fixed** (all found by actually running it):

| Before | Problem | Now |
|---|---|---|
| Rules were tested only on already-matched orders | Batch 1 has none, so no rule could ever pass the test, and the system could never learn | Payments proven by the bank check count as evidence too |
| The bank check tried every combination and picked the "smallest" answer | It made 15 confident wrong matches | It tries whole payout groups first, and refuses to pick when unsure |
| A rule wrong by 2 paise of rounding was rejected | 4 *correct* fee rules were thrown away | Mistakes of 5 paise or less count as rounding noise; a wrong rate is off by thousands of paise |
| "2% rule, tolerance 2" and "2% rule, tolerance 3" counted as different ideas | Votes were split, so no rule reached 3 votes | Votes are counted on the rule itself, ignoring tolerance |
| Timing was never asked about | No timing exception ever happened, so the AI was never asked | A "pattern search" stage asks the AI directly |
| With no API key, each case retried 5 times | The run took 7 minutes just to fail | Stops after the first login error or 3 failures in a row; about 6 seconds |

### How it works now

| | Before (by hand, or AI-only) | Now (this project) |
|---|---|---|
| Who matches records | A person in Excel, or an AI | Plain code and maths. The AI never matches. |
| Bundled bank deposits | Trial and error by hand | Code works out which payments add up to each deposit, and flags it when unsure |
| Fees | Rarely checked line by line | Every sale checked against the contract, with overcharges shown in rupees |
| New patterns (fees, timing) | Someone writes the rule by hand | The AI suggests rules, and code tests them strictly before using them |
| When unsure | A guess, or a confident AI answer | Left as an exception for a human, biggest money first |
| Over time | The same work every month | Each learned rule becomes plain code, so fewer cases need the AI or a human |
| Proof for an auditor | "Someone checked it" | Every match records which rule made it; every rule records the test it passed |

**The pitch in one line:** *Plain code does the matching, the contract decides
what's correct, and the AI only suggests rules that must prove themselves on
past data before they are used.*

---

## A few words you'll see

| Word | Simple meaning |
|---|---|
| **paise** | 1/100 of a rupee. All money is stored as whole paise, so ₹4,015.97 is stored as `401597`. This avoids decimal rounding errors. |
| **order** | A sale in the shop's own records. |
| **settlement** | The payment company's record of what it actually paid the shop for one sale, after taking its fee. |
| **bank credit** | One deposit in the bank statement. It usually covers many settlements added together. |
| **fee (MDR)** | What the payment company keeps. Cards pay a percentage, netbanking pays a fixed ₹12, UPI pays nothing. There is also 18% GST (tax) on the fee. |
| **gross / net** | Gross is what the customer paid. Net is what the shop received after the fee. |
| **working days** | Weekdays that are not Indian public holidays. |
| **match** | A saved note saying "these two records are the same money". |
| **exception** | A saved note saying "we couldn't prove this one; a human should look". |
| **rule** | A small pattern the system trusts, e.g. "CARD_CREDIT fee = 2% + 18% GST". |
| **proposal** | A rule the AI *suggested* that is not trusted yet. |
| **the gate** | The set of checks a proposal must pass to become a trusted rule. |

---

## 1. How the program is started

### The key idea: nothing runs on its own

This is **not** a website or an app that is always running. It is a set of
**small programs you run one at a time by typing commands in a terminal.** Each
command does one job, saves its results into a database file
(`db/recon.db`), and then **stops**. The next command opens that same file
and carries on.

Think of an office where each person does one job and leaves the finished work
in a shared cupboard. The database file is the cupboard.

```
 you type a command → it runs → it saves results into db/recon.db → it stops
 you type the next command → it reads db/recon.db → ... → it stops
```

### The commands, in the order you run them

| # | You type | Which file runs | What it does |
|---|---|---|---|
| 1 | `python -m src.pipeline --reset-db` | `src/pipeline.py` | Deletes the old database and makes a fresh, empty one |
| 2 | `python -m src.pipeline --batch 1` | `src/pipeline.py` | **The main one.** Reads batch 1's CSVs, matches them, saves the results |
| 3 | `python -m src.pipeline --batch 2` (then 3, 4, 5, adversarial) | `src/pipeline.py` | Same thing for the next batch, now using the rules learned so far |
| 4 | `python -m src.metrics --report` | `src/metrics.py` | Reads the saved results, compares them with the answer key, prints scores |
| 5 | `python -m src.qa_agent "your question"` | `src/qa_agent.py` | Answers a question about the saved results (uses AI) |
| 6 | `streamlit run app/dashboard.py` | `app/dashboard.py` | Opens a web page in your browser showing the saved results |

You don't have to run all of them. For a demo with data already in the
database, you only need number 6. The input data is a fixed dataset already
committed in `data/`, so there is nothing to generate first.

**The quickest demo:** copy the recorded live run into place, then open the
dashboard. It shows the rules the AI learned, with no API calls and no waiting.
```powershell
Copy-Item db\live_final.db db\recon.db -Force
streamlit run app/dashboard.py        # then open http://localhost:8501
```

### What `--no-llm` does (and why it exists)

The code only matches a sale if it can **explain why money is missing** (the
fee). At the start it knows no fee rules, so it flags almost every sale. The AI
is what works out the rule ("credit cards: 2% + 18% GST"); once the gate
approves it, plain code uses it and matches those sales by itself.

`--no-llm` switches that learning step **off**. The results are *not* the same:

| | Batch 1 | Batch 2 | Batch 3 | Batch 4 |
|---|---|---|---|---|
| With AI: flagged sales | 68 | 21 | 23 | 19 |
| `--no-llm`: flagged sales | 68 | 68 | 67 | 69 |

Use `--no-llm` when you have no API key, for tests, or for a quick check that
the code still works. It takes seconds and is free. With the AI on, all six
batches took about 36 minutes and cost about $0.35 (499 calls) in the recorded
run. Either way, precision stays at 100% and batch 5's overcharge is caught.

### What `python -m src.pipeline --batch 1` actually means

Word by word:
- `python`: start Python.
- `-m src.pipeline`: open and run the file **`src/pipeline.py`**. (`src.pipeline`
  is Python's way of writing `src/pipeline.py`.)
- `--batch 1`: an instruction passed to that program: "work on batch 1".

### What Python does inside `src/pipeline.py`

Python reads the file from top to bottom:

1. **Top of the file ([lines 23-32](src/pipeline.py#L23-L32)):** the `import`
   lines load the other files (`db.py`, `blocking.py` and so on) so their
   functions can be used. **Nothing runs yet.** It's like taking tools out of a
   box.
2. **Middle of the file:** `class BatchRun` and `def main()` only *define*
   code. **Still nothing runs.** It's like writing a recipe without cooking.
3. **The last two lines ([lines 667-668](src/pipeline.py#L667-L668)) are the
   "on switch":**
   ```python
   if __name__ == "__main__":
       main()
   ```
   This means: "if this file was started directly from the terminal, call
   `main()`". **This is the exact line where the program actually starts.**
   Every runnable file in `src/` ends with these same two lines.

### What `main()` does, in order ([pipeline.py:648](src/pipeline.py#L648))

```
main()
 ├─ 1. read what you typed: --batch 1          (argparse)
 ├─ 2. open the database file db/recon.db      (db.get_conn, db.init_db)
 │      └─ creates the tables if missing, adds the one starting rule
 ├─ 3. run_batch(conn, "1")
 │      ├─ ingest_batch()   → copy the 3 CSV files into the database
 │      └─ BatchRun(...).run()
 │            ├─ load the trusted rules
 │            ├─ stages A → F  (split, id check, pairing, bank, AI, AI pattern search)
 │            ├─ the gate      (turn good AI suggestions into rules)
 │            ├─ contract check (overcharges)
 │            └─ save the batch's numbers
 └─ 4. print a one-line summary, and the program ENDS
```

Some things are set up only the first time they are needed:
- **Settings** from `config.yaml` (and the API key from `.env`) are read the
  first time any code asks for a setting.
- **The working-day calendar** is built the first time a date is compared.
- **The AI connection** opens only when there is an actual question for the AI.
  If you add `--no-llm`, it never opens.

### How the dashboard starts (it's a bit different)

`streamlit run app/dashboard.py` starts a small local web server and opens your
browser. Streamlit runs `dashboard.py` from top to bottom to draw the page, and
**runs the whole file again every time you click something.** The dashboard
**does not match anything.** It only reads what the pipeline already saved in
`db/recon.db`. If that file doesn't exist, it shows "No database yet".

It opens on the latest numbered batch and has **six tabs**:

| Tab | What it shows |
|---|---|
| **Summary** | Where the money went: total sales, fees, GST, net settled, received in bank, overcharged vs contract. "Orders reconciled: X of Y". The **money bridge** (a waterfall from total sales down to net settled, with a one-line summary), fees by payment method, and a per-payment-method table. |
| **Contract audit** | Agreed rate vs actual rate per payment method, and the worst overcharged sales |
| **How it learned** | Flagged sales and AI calls falling batch by batch, precision staying at 100% |
| **Rule library** | Rules the AI learned, and suggestions the gate blocked |
| **Exceptions** | The open work list, biggest money first |
| **Ask** | Questions in plain English (the only tab that needs the API key) |

The sidebar has three **filters for the Summary tab**: payment method, order
date and status. The totals are worked out in `summary_totals()` in
[app/summary.py](app/summary.py#L75), kept out of `dashboard.py` so they can be
tested on their own. If a filter makes a number impossible to know, the card
says **"n/a"** with a tooltip instead of showing a wrong number (a bank deposit
mixes all payment methods, so it can't be split by method).

**The money bridge** answers "customers paid ₹17.28 lakh, so why did only
₹14.11 lakh arrive?" in batch 5. `money_bridge()` in `app/summary.py` works out
every step from the data:

```
₹17.28L sold
  − ₹2.53L never settled   (no payout at all, full refunds, duplicate ledger rows)
  − ₹28.8K charged back
  − ₹32.4K refunded        (partial refunds taken out of payouts)
  − ₹10.9K fees and GST
  + ₹8.0K settled for orders not in the ledger
  = ₹14.11L settled
```

Anything the named steps can't explain goes into an "other" amount, and it is
₹0 on every batch. A test checks that the steps add up to the paisa.

The page also works on narrow screens (cards stack, the sidebar starts
collapsed) and in dark mode.

### How the files connect

```
 data/batch_N/*.csv   (fixed dataset, committed)
                                   │
 pipeline.py ──reads CSVs, matches, writes──> db/recon.db
                                                  │
        ┌─────────────────────────┬───────────────┼──────────────┐
        ▼                         ▼               ▼              ▼
   metrics.py               qa_agent.py     dashboard.py    (next pipeline run
   (scores, reads          (answers with    (shows it all)   uses the rules
    truth.csv too)          AI)                               saved here)
```

---

## 2. What happens, step by step

When you run `python -m src.pipeline --batch 1`:

### Step 1: load the files
`run_batch()` calls **`ingest_batch()`** in [src/db.py](src/db.py#L132).
- **Takes:** the batch number.
- **Does:** reads the three CSV files from `data/batch_1/` and copies the rows
  into the database. The settlements file is in **Razorpay's official
  settlement-report format**, so `read_razorpay_settlements()` translates it
  first: Razorpay's fee includes GST (so fee = `fee − tax`), refunds are
  separate rows that get folded into their payment, and chargebacks become
  negative rows. The order id (`order_receipt`) is saved as
  `order_id_claimed`, because the payment company only *claims* which order it
  belongs to, and that claim could be wrong.
- It **never reads `truth.csv`**, the answer key. Only the scoring step may
  read it.
- **Returns:** how many rows were loaded.
- Then `run_batch()` **deletes any old results for this batch** (matches,
  exceptions, metrics), so running a batch twice gives the same numbers as
  running it once.

### Step 2: load the known rules
`BatchRun(...)` in [src/pipeline.py](src/pipeline.py#L52) calls
`RuleSet.load()` in [src/deterministic.py](src/deterministic.py#L240).
- **Does:** reads every trusted rule from the database and puts them in a
  fixed order, so it always knows which rule to check first.
- In batch 1 there is only the starting rule. The system knows nothing about
  fees yet.

### Step 3: run six stages in a row
`BatchRun.run()` ([pipeline.py:572](src/pipeline.py#L572)) passes the records
through six stages. Each stage handles what it can and passes the rest on.

```
split payments -> id check -> smart pairing -> bank check -> ask AI -> AI pattern search
```

**Stage A: split payments, `run_split_leg()`** ([pipeline.py:157](src/pipeline.py#L157))
Sometimes one sale is paid out in two parts. This stage looks for settlements
whose amounts add up to *exactly* the order amount, and matches them together.
It runs first so the id check doesn't mistake a half-payment for a wrong
amount.

**Stage B: id check, `run_identity_leg()`** ([pipeline.py:123](src/pipeline.py#L123))
- It pairs each settlement with the order whose id it names (a quick
  dictionary lookup in `blocking.hash_join`).
- For each pair it asks: **"Does a known fee rule explain why the shop got
  less money?"** (`explain_amount`). It also asks whether the payment arrived
  in the expected number of days (`explain_timing`).
- If both answers are yes, it saves a **match**.
- If no rule explains the fee, it saves an **exception** called
  `FEE_UNEXPLAINED`. In batch 1 this happens to almost everything, because no
  fee rules have been learned yet.

**Stage C: smart pairing, `run_assignment_leg()`** ([pipeline.py:211](src/pipeline.py#L211))
This is for records whose ids *don't* line up.
- It finds settlements that *could* belong to each order: a similar amount,
  and paid within 6 working days (`blocking.candidate_pairs`).
- It groups records that might be connected into small groups
  (`blocking.components`), so each small group can be solved separately.
- For each group it finds **the best overall pairing**, using the Hungarian
  algorithm or min-cost flow ([src/assignment.py](src/assignment.py)). Think of
  it as solving the whole puzzle at once instead of grabbing the first piece
  that fits.
- Every pairing has a "cost". A pair that no rule can explain costs 120.
  Leaving both records unmatched costs 100. So **the code prefers to leave
  records unmatched rather than guess.** That is why it almost never makes a
  wrong match.
- Only a **fee rule or refund rule** counts as explaining a pair, because only
  those explain the *money* (`pair_cost()` in `assignment.py`). A timing rule
  agrees with any two records paid on the usual day, whatever the amounts, so
  on its own it never makes a pair look explained.
- Records it cannot pair become exceptions: `NO_SETTLEMENT` (an order with no
  payment) or `ORPHAN_SETTLEMENT` (a payment with no order).

**Stage D: bank check, `run_bank_leg()`** ([pipeline.py:247](src/pipeline.py#L247))
The bank shows one big deposit made of many settlements. This stage works out
which settlements make up each deposit.
- **Easy way first:** the payment company groups settlements into payout
  batches. If one group adds up exactly to a deposit, it matches them all.
- **Harder way for the rest:** it tries combinations of settlements that add up
  to the deposit ([src/subset_sum.py](src/subset_sum.py)). If *more than one*
  combination fits, it **refuses to guess** and saves an exception.
- This stage needs no knowledge of fees, so it works from day one. That matters
  later (see Step 5).

**Stage E: ask the AI, `run_llm_leg()`** ([pipeline.py:374](src/pipeline.py#L374)). **Uses AI.**
- It takes the remaining exceptions, biggest money first.
- Only fee, timing and bank-grouping questions are sent to the AI. Others,
  such as a missing payment, have no pattern to find, so they are skipped. The
  code counts how many AI calls were saved this way.
- The AI can reply with two things:
  - **A suggested rule**, e.g. "credit cards: 2% + 18% GST". This is saved as a
    proposal.
  - **"I think this matches X."** This is **only written as a note on the
    exception** for a human to read. It is never turned into a match.
- Why the second one is only a note: in a real test run, the AI made 39 wrong
  matches in batch 1, all with full confidence. So its opinion became advice
  only.

**Stage F: AI pattern search, `run_discovery_leg()`** ([pipeline.py:435](src/pipeline.py#L435)). **Uses AI.**
Some patterns never cause an exception, so the AI would never be asked about
them. Timing is the example: with no timing rule, nothing is ever flagged as
"late". So this stage shows the AI a few *already-matched* records and asks,
"What pattern do you see?" It stops asking once the rule is learned.

### Step 4: keep score on the rules
`update_rule_stats()` ([pipeline.py:327](src/pipeline.py#L327)) counts how often
each rule was used, and whether it caused a conflict (for example, two orders
claiming the same payment). A rule that keeps causing problems gets removed
later.

### Step 5: the gate, where suggestions become rules
`process_proposals()` in [src/rule_engine.py](src/rule_engine.py#L323).

A suggestion must pass **four checks** to become a trusted rule:

| Check | What it means |
|---|---|
| Repeated | The AI suggested the same rule at least **3 times**. |
| Confident | Its average confidence was at least **0.8**. |
| Tested on past data | The rule is replayed on every record already proven correct, **including those the bank check proved**. It must not get any of them wrong (beyond a few paise of rounding). |
| No clash | It doesn't clash with a rule the system already trusts. |

- **Passes all four:** it becomes a trusted rule.
- **Just needs more evidence:** it waits for the next batch.
- **Clearly wrong:** it is rejected, and the reason is saved.
- **A trusted rule that later performs badly is removed, and every match it
  made is undone** and turned back into an exception.

This is the key idea: **next batch, the new rule is ordinary code.** The AI is
never asked about that kind of case again, so AI calls go down every batch.

### Step 6: contract check (were you overcharged?)
`leakage_report()` in [src/contract.py](src/contract.py#L157).
For each normal sale it compares:
- **fee actually taken** = what the customer paid − what the shop received
- **fee the contract allows** = the rates written in `config.yaml`

Differences of 5 paise or less are ignored as rounding. Anything larger is
reported as **overcharged money**, with the worst transactions named.

It reports two totals:
- **Net leakage** (`total_leaked_paise`): overcharges minus any undercharges.
- **Gross overcharge** (`gross_overcharged_paise`): only the overcharged sales,
  added up. An undercharge on one sale can never hide an overcharge on another.
  It also counts the undercharged sales (`transactions_undercharged`).

### Step 7: save the results
Numbers such as the match rate, exceptions, AI calls and overcharge amount are
saved into the `run_metrics` table ([pipeline.py:602](src/pipeline.py#L602)),
and a summary is printed.

### Step 8 (separate command): scoring
`python -m src.metrics --report` runs `score_batch()` in
[src/metrics.py](src/metrics.py#L59). It compares every match against the
answer key (`truth.csv`) and reports:
- **Precision:** of the matches made, how many were right.
- **Recall:** of all the correct matches possible, how many were found.
- **False positives:** wrong matches. This is the number that matters most.

---

## 3. Following one sale through the system

Our example sale is **ORD-1-0002**, a credit card sale in batch 1.

**Starting point: the CSV files**
```
Order:       ORD-1-0002, paid 401597 paise (₹4,015.97), CARD_CREDIT, 9 Jan (Thursday)
Settlement:  one row of Razorpay's settlement export (settlements.csv)
             entity_id STL-H1HQHS3V, type payment, amount 401597,
             fee 9478 (this INCLUDES the GST), tax 1446 (the GST part),
             credit 392119 (what the shop receives), order_receipt ORD-1-0002,
             settlement_id SB-1-20250113, method card / card_type credit,
             settled_at = 13 Jan (Monday), as a unix timestamp
```
The hidden maths: 2% of 401597 = 8032 is the fee, 18% GST on that = 1446, so
Razorpay's fee column shows 8032 + 1446 = 9478, and 401597 − 9478 = 392119.
Thursday to Monday = 2 working days.

**1. Loaded into the database.** `read_razorpay_settlements()` in `src/db.py`
turns the Razorpay row into the internal shape: fee 8032 (= 9478 − 1446), GST
1446, net 392119, the order id (`order_receipt`) saved as `order_id_claimed`,
and CARD_CREDIT as the payment method. It then sits in the database tables.

**2. Split payments stage.** Only one settlement names this order, so nothing
happens.

**3. Id check (batch 1).** The ids match, but no fee rule is known yet, so it
can't explain why ₹94.78 is missing. An **exception** is saved:
```
FEE_UNEXPLAINED: "ORD-1-0002 settled 392119p against 401597p gross after
2 working days; no active rule explains the deduction"   money at risk: 9478
```

**4. Bank check.** The 7 settlements in group SB-1-20250113 add up exactly to
one bank deposit that day, so the settlement is matched to that deposit. The
money did arrive, even though the fee is still unexplained.

**5. Ask the AI.** The AI gets this case, uses its calculator tool, and replies:
> "Match with ORD-1-0002, confidence 1.0. Suggested rule: CARD_CREDIT = 2% fee
> + 18% GST on the fee."

The **suggested rule is saved as a proposal.** The "match" opinion is only
added as a note on the exception.

**6. The gate.** Batch 1 has 12 credit card orders (10 of them settled), so the
AI suggests the same rule several times. It is tested on past data and passes. **It becomes trusted
rule #5.**

**7. Next batch: no AI needed.** In batch 2, a similar sale (**ORD-2-0002**,
₹10,650.87) arrives. The id check now uses rule #5:
```
fee = 2% of 1065087 = 21302,  tax = 3834
expected amount = 1065087 − 21302 − 3834 = 1039951  = what was actually paid ✓
```
So it is **matched straight away by plain code**, with no exception and no AI
call. This is the "it gets cheaper as it learns" idea, shown on one sale.

**8. Contract check.** The contract says 2% for credit cards. The fee charged
was exactly 2%, so no overcharge.

**9. Scoring.** The answer key says ORD-2-0002 goes with STL-HLP66OFU, so the
match is counted as correct.

**How the data changes shape along the way:**
```
CSV line → database row → Python dictionary → (order, settlement) pair
→ either a "match" row or an "exception" row
→ (if sent to the AI) question → AI answer → suggested rule
→ (if it passes the gate) trusted rule → used by plain code next batch
→ score and dashboard
```

---

## 4. Which parts use AI and which don't

| Part | Uses AI? |
|---|---|
| Load files | No |
| Split payments, id check, smart pairing, bank check | No, plain code and maths |
| **Ask the AI about exceptions** (`run_llm_leg`) | **Yes** |
| **AI pattern search** (`run_discovery_leg`) | **Yes** |
| The gate (testing suggestions) | No |
| Contract check | No |
| Scoring | No |
| **Question answering** (`qa_agent.py`, dashboard "Ask" tab) | **Yes** |
| Rest of the dashboard | No |

**The AI never creates a match.** It only suggests rules and writes notes.

### AI use #1 and #2: suggesting rules ([src/llm_reasoner.py](src/llm_reasoner.py))
- **What the AI receives:** instructions (`SYSTEM_PROMPT`), the unsolved record,
  possible matches, the number of working days (already worked out), and the
  rules already known.
- **Tools it can use** instead of guessing:
  - `calculate`: a safe calculator. It only does maths and cannot run any code.
  - `check_rule_against_history`: tests a rule on past data, using the same
    test as the gate.
  - `get_working_day_lag`: counts working days.
- **What it replies with:** a small JSON object containing its answer,
  confidence, reasoning and an optional suggested rule.
- **How the answer is checked:**
  1. If the reply isn't valid JSON, it is asked again (up to 5 tries).
  2. A suggested rule that doesn't fit one of the 4 allowed shapes is thrown
     away.
  3. A surviving suggestion still has to pass the four-check gate.
- **If the AI service is down or out of quota:** after 3 failures in a row,
  the code stops asking, marks those cases `LLM_UNAVAILABLE`, and everything
  else still finishes.

### AI use #3: answering questions ([src/qa_agent.py](src/qa_agent.py))
- `SettlementQA.ask()` sends your question to the AI along with **8 read-only
  lookup tools**. Examples: look up a transaction, list the biggest exceptions,
  total the fees, check the contract.
- The instructions tell it: never state a number a tool didn't give you, list
  your sources, and say "I don't have that in the reconciled data" if you don't
  know. They also say that **fees are always judged against the contract in
  `config.yaml`**, never against the rules the AI learned.
- Note: these instructions are only in the prompt. No code double-checks the
  answer. But the tools can only *read* data, so the AI can't damage anything.

### Switching AI providers ([src/llm_client.py](src/llm_client.py))
The code is written for Claude's message format. For Gemini and OpenRouter,
this file translates messages back and forth. `config.yaml` currently uses
**OpenRouter** (`provider: "openrouter"`, model `google/gemini-3.1-flash-lite`),
with the key in `.env` as `OPENROUTER_API_KEY`.

The model name follows the provider: with `provider: "anthropic"` the code uses
`anthropic_model`; with any other provider it uses `model`.

---

## 5. Where the key numbers are worked out

| What | Where |
|---|---|
| **Match rate** | `BatchRun.run()` in [pipeline.py:602](src/pipeline.py#L602): matched orders + matched settlements, divided by all records |
| **Precision, recall, wrong matches** | `score_batch()` in [metrics.py:59](src/metrics.py#L59) |
| **Exceptions are created** | `BatchRun.exception()` in [pipeline.py:83](src/pipeline.py#L83), called from each stage |
| **Exceptions are listed, biggest first** | `list_exceptions()` in [qa_agent.py:154](src/qa_agent.py#L154), and the dashboard's Exceptions tab |
| **Question answers** | `SettlementQA.ask()` in [qa_agent.py:337](src/qa_agent.py#L337) |
| **Overcharge amount** (net and gross) | `leakage_report()` in [contract.py:157](src/contract.py#L157) |
| **Summary tab totals and "Orders reconciled"** | `summary_totals()` in [app/summary.py:75](app/summary.py#L75) |
| **Money bridge** (sales → settled, step by step) | `money_bridge()` and `bridge_sentence()` in [app/summary.py](app/summary.py) |
| **Learned rules in plain English** | `plain_english()` in [rule_engine.py:334](src/rule_engine.py#L334) |

---

## 6. What each file does

| File | What it does | Works with |
|---|---|---|
| [src/pipeline.py](src/pipeline.py) | **The main engine.** Runs all the stages for one batch. | almost every other file |
| [src/db.py](src/db.py) | Opens the database, creates tables, loads the CSV files. `read_razorpay_settlements()` translates Razorpay's settlement export. | schema.sql |
| [src/schema.sql](src/schema.sql) | The layout of all database tables. | db.py |
| [src/config.py](src/config.py) + [config.yaml](config.yaml) | All settings: contract rates, limits, which AI to use. | every file |
| [src/money.py](src/money.py) | All money maths, in whole paise, with careful rounding. | many files |
| [src/calendar_utils.py](src/calendar_utils.py) | Counts working days, skipping weekends and Indian holidays. | many files |
| [src/blocking.py](src/blocking.py) | Finds which records *could* match, and groups them. | pipeline |
| [src/assignment.py](src/assignment.py) | Picks the best overall pairing within each group. | pipeline |
| [src/subset_sum.py](src/subset_sum.py) | Finds which settlements add up to a bank deposit. | pipeline |
| [src/deterministic.py](src/deterministic.py) | How rules are written, checked, ordered and tested on past data. | pipeline, rule_engine, AI |
| [src/llm_reasoner.py](src/llm_reasoner.py) | Asks the AI for rule suggestions, with tools and safety checks. | llm_client, deterministic |
| [src/llm_client.py](src/llm_client.py) | Connects to Claude, Gemini or OpenRouter. | llm_reasoner, qa_agent |
| [src/rule_engine.py](src/rule_engine.py) | The gate: accepts, rejects or removes rules. | deterministic |
| [src/contract.py](src/contract.py) | Checks fees against the contract (overcharge check). | pipeline, dashboard, Q&A |
| [src/metrics.py](src/metrics.py) | Scores results against the answer key. **Only this file reads it.** | db |
| [src/qa_agent.py](src/qa_agent.py) | Answers questions about the data using AI. | llm_client, db |
| [data/](data/) | The fixed synthetic dataset: batches 1-5 plus a tricky adversarial set, each with an answer key (`truth.csv`). | read by db.py; answer keys only by metrics.py |
| [app/dashboard.py](app/dashboard.py) | The web dashboard (6 tabs). | summary, metrics, contract, qa_agent |
| [app/summary.py](app/summary.py) | The totals behind the Summary tab, with the filters, and the money bridge. | contract, db |
| [tests/](tests/) | 409 automatic tests. They need no API key. `test_dataset.py` checks the data itself (the maths adds up, Razorpay's column layout, no holidays). | everything |

---

## 7. The whole flow in one picture

```
   Fixed dataset (data/)
        |
        v
   3 CSV files: orders, settlements, bank statement
        |
        v
   Load into database (db.py)
        |
        v
  +---------------- PLAIN CODE (no AI) ----------------+
  |  A. Split payments  - parts that add up to 1 order  |
  |  B. Id check        - same id? fee explained?       |
  |  C. Smart pairing   - best overall pairing,         |
  |                       prefers "unmatched" to guess  |
  |  D. Bank check      - which payments make up        |
  |                       each bank deposit?            |
  +-----------------------------------------------------+
        |  things still unsolved
        v
  +---------------- AI (suggest only) ------------------+
  |  E. Ask AI about exceptions                         |
  |  F. Ask AI to spot patterns                         |
  |     -> suggested rules      (match opinions = notes)|
  +-----------------------------------------------------+
        |
        v
   The gate (rule_engine.py): repeated 3x? confident?
   right on past data? no clash?
        |  yes -> trusted rule -------> used by plain code NEXT batch
        v
   Contract check (contract.py): overcharged?
        |
        v
   Save results (run_metrics)
        |
        +--> Scoring (metrics.py, uses answer key)
        +--> Question answering (qa_agent.py, uses AI)
        +--> Dashboard (app/dashboard.py)
```

---

## 8. Weak spots an interviewer might ask about

### Fixed in this revision

1. ~~Running the same batch twice doubled the results.~~ **Fixed:** `run_batch()`
   now clears the batch's old matches, exceptions and metrics first. (Rule
   suggestions from an earlier run still keep their votes; that part is open.)
2. ~~A timing rule alone could cause wrong matches.~~ **Fixed:** only fee and
   refund rules count as explaining a pair (`pair_cost()`), with a test.
3. ~~Switching to Claude sent a Gemini model name.~~ **Fixed:** the model now
   follows the provider.
4. ~~The overcharge total could hide overcharges.~~ **Fixed:** the report now
   also gives the gross overcharge and the number of undercharged sales.

### Still open (be ready to talk about these)

1. **The batch 1 match rate (44.3%) sounds better than it is.** A settlement
   counts as "matched" if the *bank* confirmed it, even if it isn't matched to
   its order. In batch 1, all 58 matches are bank matches and there are 0
   order-to-payment matches. **Precision and recall are the honest numbers.**
   The Summary tab's "Orders reconciled" line shows the stricter, order-level
   view.

2. **Input files are only partly checked.** The Razorpay reader rejects row
   types it doesn't understand, refunds whose payment is missing, and payment
   methods the contract doesn't cover (like wallet or EMI), naming every one.
   But:
   - A missing column in the orders or bank file crashes the run.
   - An amount written as "1,234.56" is stored wrongly.
   - A date outside 2024 to 2027 crashes the calendar.

3. **The same id in a later file silently replaces the old record.**

4. **An exact duplicate row in one file is silently merged**, with no warning.

5. **If a payment names the wrong order, the mistake is never corrected.** After
   the id check pairs two records, they skip the smart-pairing stage.

6. **Refunded and split sales are not checked against the contract.**

7. **Timing rules can never be removed**, because they are always counted as
   "correct".

8. **Nothing checks the Q&A answers.** The "only use real numbers" instruction
   is in the prompt only.

9. **AI availability.** The recorded run hit Gemini's free limit (15 requests)
   on some cases. The code handles it without crashing, but learning stops.
   OpenRouter is pay-per-use, so that limit is gone, but a live run still
   depends on the service being up.

10. **Customer names are sent to the AI.** Someone could put tricky text in a
    name to confuse it. The damage is limited, because the AI can't make
    matches or change data.

11. **The recorded runs used Razorpay-like ids, not real ones.** Settlement ids
    stay `STL-`/`CB-` (not Razorpay's `pay_`) so they line up with the recorded
    runs; card network and issuer are blank; all values are synthetic.

### Values written directly into the code
- Bank deposit date window: 1 day ([pipeline.py:40](src/pipeline.py#L40))
- Match confidence values 0.99 / 0.95 / 0.9 (pipeline.py)
- A settlement can be as low as 40% of the order amount and still be a
  candidate ([blocking.py:102](src/blocking.py#L102))
- Rule priority numbers 50 and 80 ([rule_engine.py:33](src/rule_engine.py#L33))
- AI limits: 8 tool steps per question, 4096 output tokens
- Only two fee styles are supported: percentage or fixed amount
- Indian national holidays only, 2024 to 2027, rupees only

### To make it ready for real use
1. Read real bank file formats, and check every file properly on load.
2. Never load the same file twice, and count rule suggestions once per case.
3. Use real Razorpay ids and card details when reading a live export.
4. Use a proper database (Postgres) instead of a single file.
5. Handle much more data. The "test on past data" step currently runs one
   database query per past record.
6. Support more complex fee plans (fees by amount band or card type, contracts
   that change over time).
7. Use a paid AI plan, and run AI calls in parallel.
8. Have a human approve new rules before they go live.
9. Add alerts when overcharges or exceptions jump.
10. Handle time zones and cut-off times properly.

---

## 9. Likely interview questions, with simple answers

**0. What problem does this solve, and why did you build it?**
> Every merchant has three records of the same money: their orders, the payment
> company's report and the bank statement. They never line up neatly, because of
> fees, delays and bundled deposits. So finance teams match them by hand, and
> they rarely check each fee against the contract. That means unpaid sales and
> small overcharges slip through. My system matches the records with plain code,
> checks every fee against the contract, and uses AI only to suggest new rules,
> which must pass a strict test before they are used.

**1. Why not let the AI do the matching?**
> Because a wrong match is the worst mistake. It silently messes up the books
> and nobody notices for months. When I let the AI make matches in a real run,
> *every* wrong match came from the AI, and it was 100% confident each time. It
> was only checking that the payment's own numbers added up, which says nothing
> about which order it belongs to. So now its opinion is just a note, and wrong
> matches dropped to zero.

**2. So what is the AI for?**
> Suggesting rules and writing explanations. A suggestion only becomes a rule
> after passing four checks: suggested 3+ times, confident, correct on all past
> data, and no clash with existing rules. After that, plain code handles those
> cases, and the AI isn't needed for them any more.

**3. How can you test a rule on past data when nothing has been matched yet?**
> The bank check. It needs no fee knowledge; it just proves that certain
> payments add up to a real bank deposit. Those proven payments are the first
> "past data" the rules are tested on.

**4. Why use the Hungarian algorithm instead of just taking the closest match?**
> Taking the closest match first can go wrong when two sales look almost
> identical. My tricky test data includes exactly this case. The Hungarian
> algorithm looks at the whole group at once and finds the best overall answer.
> Groups are tiny (1 to 5 records), so it's fast.

**5. How does it know when *not* to match?**
> Leaving two records unmatched costs 100. Matching a pair that no rule explains
> costs 120. So the code always prefers to leave it unmatched rather than guess.
> And only a fee or refund rule counts as explaining a pair, because only
> those explain the money. A timing rule alone never does.

**6. Why store money in paise instead of rupees with decimals?**
> Computers can't store some decimals exactly, so tiny errors build up. Whole
> paise have no such errors. All rounding happens in one file, `money.py`.

**7. If it learns fees from the data, won't it also "learn" an overcharge?**
> That was my first design, and it was a mistake: it would accept whatever was
> charged. Now the contract rates are an *input*, and every batch checks the
> actual fee against the agreed fee. In batch 5 the payment company quietly
> raised rates. The system refused to learn the new rates and reported
> ₹399.33 of overcharges across 17 sales.

**8. How do you stop a bad rule from causing damage?**
> Four layers: suggestions must fit a strict format; they must pass the four
> checks; they can't clash with existing rules; and a rule that later performs
> badly is removed, with all its matches undone. Every decision is logged.

**9. Isn't "which payments add up to this deposit" a very slow problem?**
> In theory, yes. First I try the easy way: does one payout group add up
> exactly? Only the leftovers go to the slower search, and there are safe limits
> on it. The real risk isn't speed but *more than one answer fitting*. When that
> happens, it refuses to pick one and flags it for a human.

**10. What if the AI service is down?**
> Everything else still runs. After 3 failures in a row it stops asking, marks
> those cases "AI unavailable", and finishes the matching, contract check and
> scoring. All 409 tests run without an AI key.

**11. Is the data real? Does it match what Razorpay actually sends?**
> The values are synthetic, but the settlement file uses Razorpay's real
> settlement-report layout: the same 26 columns, fee *including* GST with the
> GST shown separately, refunds and chargebacks as their own rows, amounts in
> paise, Unix timestamps. `read_razorpay_settlements()` translates it for the
> pipeline, and a test checks that translating it back gives the original data.

**12. Razorpay's report tells you the bank UTR. Why do you still work out which
payments make up each bank deposit?**
> Because that UTR is the payment company's own claim, and the payment company's
> report is what we're auditing. The bank check proves the grouping from the
> bank statement, which is independent evidence. That independent evidence is
> also what the gate tests new rules against.

**13. What does `--no-llm` do?**
> It switches off the learning. The code still matches what it can, and the
> contract check still catches overcharges, but no new rules are learned, so it
> stays at the batch-1 level (about 44% match rate). With the AI, it reaches
> 80% by batch 4. It's for running without a key, and for tests.

**Tricky follow-ups:**
- *"Batch 1 has 44% match rate but no order matches?"* Yes. That number includes
  payments confirmed by the bank. Point to precision and recall, or the
  Summary tab's "Orders reconciled" line.
- *"What if I run batch 1 twice?"* You get the same numbers: the old results
  for that batch are cleared first.
- *"Where's the code that generated the data?"* It was removed once the dataset
  was final. The data is fixed and committed, and `tests/test_dataset.py`
  checks it (the maths adds up, every bank deposit equals its payout group,
  nothing settles on a holiday).

---

## 10. What changed in this revision

**Bug fixes**
- Re-running a batch replaces its results instead of doubling them
  (`run_batch()` in `pipeline.py`).
- A timing rule alone no longer lets unrelated records match (`pair_cost()` in
  `assignment.py`).
- The model name follows the provider (`llm_reasoner.py`, `qa_agent.py`).

**New features**
- OpenRouter support (`llm_client.py`), now the default provider.
- The dashboard's **Summary** tab with filters (`app/summary.py`), and its
  **money bridge**, which explains every rupee between total sales and net
  settled (`money_bridge()`).
- The contract report's gross overcharge and undercharged count
  (`contract.py`).
- The dashboard works on narrow screens and in dark mode, and opens on the
  latest real batch.

**Checkable results and clearer errors**
- The recorded live runs are now committed: `db/live_final.db` (the canonical
  results) and `db/live.db` (the run before the AI's match verdict became
  advice only). A fresh clone can check every number in the README.
- A Razorpay file with a payment method the contract doesn't cover (wallet,
  EMI) now fails with a clear message naming every such method.
- `python -m src.metrics --report` prints calls per 100 records correctly
  (34.8, not 34.9, for batch 4), and its summary line compares batch 1 with
  batch 5 instead of the adversarial set.

**Data**
- The data generator was removed. The dataset is fixed and committed in
  `data/`.
- The settlement files use **Razorpay's official settlement-report format**,
  read by `read_razorpay_settlements()` in `db.py`. Every batch gives exactly the
  same results as before the change.

**Cleanup**
- Removed unused code: the `narration_pattern` rule type, `to_paise`,
  `split_proportional`, `db.query`, the `db.py` command line, `first_match`, an
  unread latency field.
- Removed the unused `matplotlib` dependency, an out-of-date learning-curve
  image, and `SUMMARY.md`.
- Out-of-date text fixed: the API-key messages, the config header, the Q&A
  prompt's rule about fees.

**Docs**
- README: one canonical results table (`db/live_final.db`), each number
  labelled with the run it comes from; new Dashboard and Changelog sections.
- ARCHITECTURE.md: the AI's match verdict is described as advice only.

Tests: **409**, all passing, none needing an API key.

---

## Before the demo

- **Make the dashboard show the learned rules:** `db/recon.db` must hold the
  recorded live run. Copy it in with
  `Copy-Item db\live_final.db db\recon.db -Force` (already done once).
- **Start the dashboard:** `streamlit run app/dashboard.py`, then open
  http://localhost:8501. A good click order: Summary on batch 5 (₹399.33
  overcharged), Contract audit, How it learned, Rule library, then batch 1 for
  the "before" picture.
- **After any code change, restart the dashboard** (Ctrl+C, then run it
  again). Refreshing the page reloads `dashboard.py` but not `app/summary.py`,
  and a stale server shows an ImportError.
- **The Ask tab** uses your OpenRouter key and costs a little per question.
- **Close Excel** before running the pipeline. An open CSV is locked and the
  run can't read or write it.
- `python -m pytest` runs 409 tests, all passing, in about 3.5 minutes. It's a
  safe thing to show.
