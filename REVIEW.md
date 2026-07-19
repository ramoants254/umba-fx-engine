# Code Review — Planted Bugs in AI Baseline

Below is a detailed review of the baseline code in `planted_bugs/` sorted by severity.

---

## 1. Float Conversion in Quote Math

- **Severity:** Blocker
- **File:** [fx.py:L60-L63](file:///home/relego/Documents/PROJECTS/fx_takehome_candidate/fx_takehome/planted_bugs/fx.py#L60-L63)
- **What's Wrong:** The engine converts `Decimal` amounts and rates to `float` before multiplying:
  ```python
  final = float(amount) * float(rate)
  final_decimal = Decimal(str(final)).quantize(QUANTUM, rounding=ROUND_HALF_UP)
  ```
- **Why It Matters in Production:** Floating-point numbers are represented in binary (IEEE 754) and cannot accurately represent base-10 decimals. In financial transactions, this leads to rounding errors, leaving fractional cents unaccounted for or creating money out of thin air. Over millions of transactions, this causes ledger imbalances.
- **How to Fix it:** Keep calculations in `Decimal` throughout:
  ```python
  final_decimal = (amount * rate).quantize(QUANTUM, rounding=ROUND_HALF_UP)
  ```

---

## 2. Missing Customer Balance Ledger

- **Severity:** Blocker
- **File:** [fx.py:L134-L160](file:///home/relego/Documents/PROJECTS/fx_takehome_candidate/fx_takehome/planted_bugs/fx.py#L134-L160)
- **What's Wrong:** The `execute_quote` method records the transaction details in the database, but it never debits the source balance or credits the destination balance for the customer. In fact, there is no `balances` or `customers` table in the database schema ([db.py](file:///home/relego/Documents/PROJECTS/fx_takehome_candidate/fx_takehome/planted_bugs/db.py)).
- **Why It Matters in Production:** The core function of the FX engine is to exchange value between customer balances. Without this, the system is just a ledger of historical trades without performing any actual transfers.
- **How to Fix it:** Implement a `balances` table, fetch/lock balance rows during execution, check for sufficient funds, and perform balance updates (debit + credit) in the execution transaction.

---

## 3. Concurrency TOCTOU Bug via Application Lock

- **Severity:** Blocker
- **File:** [fx.py:L123-L139](file:///home/relego/Documents/PROJECTS/fx_takehome_candidate/fx_takehome/planted_bugs/fx.py#L123-L139)
- **What's Wrong:** 
  1. The code reads the quote's status (`row["executed"]`) outside the lock:
     ```python
     if row["executed"]:
         raise ValueError("quote already executed")
     ```
  2. It then acquires a thread lock (`with _execute_lock:`) only *after* validation to update the database.
  3. `_execute_lock` is an in-memory Python `threading.Lock` instance.
- **Why It Matters in Production:** 
  - **Race Condition (TOCTOU):** Two concurrent requests can both read `executed = 0` at the same time, bypass the validation check, enter the lock sequentially, and execute the quote twice.
  - **In-Memory Lock:** In production, APIs run on multiple worker processes (e.g., Gunicorn/Uvicorn workers) or across multiple nodes. An in-memory lock does not coordinate across processes, allowing double-execution.
- **How to Fix it:** Use database-level row locking via `SELECT ... FOR UPDATE` on the quote row and run the check inside the same transaction:
  ```sql
  SELECT status FROM quotes WHERE id = $1 FOR UPDATE;
  ```

---

## 4. Idempotency Key Race Condition

- **Severity:** Blocker
- **File:** [fx.py:L102-L110](file:///home/relego/Documents/PROJECTS/fx_takehome_candidate/fx_takehome/planted_bugs/fx.py#L102-L110)
- **What's Wrong:** The idempotency lookup is checked outside the main execution transaction and uses a separate database connection/transaction.
- **Why It Matters in Production:** Under high concurrency (e.g., rapid retries from a client due to a timeout), two requests with the same idempotency key can check the database simultaneously, see that no response has been cached yet, and both proceed to execute the trade, resulting in a duplicate transfer.
- **How to Fix it:** Check and insert the idempotency key *inside* the single execution transaction.

---

## 5. Recalculating Rates at Execution Time

- **Severity:** Major
- **File:** [fx.py:L126-L132](file:///home/relego/Documents/PROJECTS/fx_takehome_candidate/fx_takehome/planted_bugs/fx.py#L126-L132)
- **What's Wrong:** During execution, the engine re-fetches the rate:
  ```python
  current_rate = self._effective_rate(row["from_currency"], row["to_currency"])
  ```
  Instead of using the rate stored inside the quote row (`row["rate"]`).
- **Why It Matters in Production:** The contract of an FX quote is that the rate is locked for its TTL (60 seconds). If rates fluctuate between the quote generation and execution, the customer will receive a different amount of money than they were quoted, violating trust and SLA contracts.
- **How to Fix it:** Use the rate locked in the quote row:
  ```python
  rate = Decimal(row["rate"])
  final = Decimal(row["final_amount"])
  ```

---

## 6. Inverse Rate Math Destroys Spreads

- **Severity:** Major
- **File:** [fx.py:L189-L190](file:///home/relego/Documents/PROJECTS/fx_takehome_candidate/fx_takehome/planted_bugs/fx.py#L189-L190)
- **What's Wrong:** When resolving an inverse rate (e.g. converting KES to USD derived from USD/KES), the engine averages buy and sell to get mid, then inverts:
  ```python
  mid = (inverse["buy"] + inverse["sell"]) / 2
  return Decimal("1") / mid
  ```
- **Why It Matters in Production:** Doing this completely eliminates the spread. The bank sells/buys the inverse currency at the mid-rate, meaning zero profit margin is captured for that leg of the trade.
- **How to Fix it:** For inverse pairs, calculate the rate using the sell rate of the base pair to ensure the customer receives less destination currency (retaining the spread margin):
  ```python
  return Decimal("1") / inverse["sell"]
  ```

---

## 7. Broken Cross-Rate Resolution Math

- **Severity:** Major
- **File:** [fx.py:L193-L200](file:///home/relego/Documents/PROJECTS/fx_takehome_candidate/fx_takehome/planted_bugs/fx.py#L193-L200)
- **What's Wrong:** When crossing rates via USD, the code gets the leg values but does not check if they need inversion:
  ```python
  leg1 = self.rates.get(f"{from_ccy}/USD") or self.rates.get(f"USD/{from_ccy}")
  ```
  It multiplies `leg1["sell"] * leg2["sell"]` directly.
- **Why It Matters in Production:** If KES/USD doesn't exist but USD/KES does, the code takes the USD/KES rate (e.g., 129.50) and multiplies it directly. This yields a rate that is off by several orders of magnitude (multiplying by 129.5 instead of dividing).
- **How to Fix it:** Properly invert the leg if the database stores the inverse pair (e.g. `1 / buy`).

---

## 8. Multi-Connection Transaction Leak

- **Severity:** Major
- **File:** [fx.py:L100-L179](file:///home/relego/Documents/PROJECTS/fx_takehome_candidate/fx_takehome/planted_bugs/fx.py#L100-L179)
- **What's Wrong:** The code opens separate connection context blocks via `with get_db() as conn` for the idempotency check, the quote lookup, and execution.
- **Why It Matters in Production:** In SQLite or Postgres, each context manager call opens/commits a separate connection. The execution is fragmented into multiple individual transactions, making it impossible to roll back the entire sequence if a mid-process failure occurs.
- **How to Fix it:** Wrap the entire operation in a single database session context and manage the transaction transactionally:
  ```python
  with get_db() as conn:
      # Perform all checks and writes here...
  ```
