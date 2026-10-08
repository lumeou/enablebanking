"""Local SQLite storage for accounts, balances, transactions and the sync log.

All methods are BLOCKING: call them through hass.async_add_executor_job.
A single connection is shared and protected by a lock.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections import Counter
from typing import Any

from .categories import match_category

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA_V1 = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE accounts (
    hash TEXT PRIMARY KEY, uid TEXT, title TEXT, iban TEXT, currency TEXT,
    details_json TEXT, initial_sync_at TEXT, first_seen TEXT
);
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_hash TEXT NOT NULL, dedup_key TEXT NOT NULL, entry_reference TEXT, status TEXT,
    booking_date TEXT, value_date TEXT, transaction_date TEXT, effective_date TEXT,
    amount REAL, amount_text TEXT, currency TEXT, indicator TEXT,
    counterparty TEXT, counterparty_iban TEXT, remittance TEXT, note TEXT, mcc TEXT,
    bank_code TEXT, bank_code_desc TEXT, balance_after REAL, reference_number TEXT,
    raw_json TEXT NOT NULL, content_hash TEXT,
    category TEXT, category_source TEXT,
    first_seen TEXT, last_seen TEXT, revisions INTEGER NOT NULL DEFAULT 0,
    UNIQUE (account_hash, dedup_key)
);
CREATE INDEX idx_tx_acc_date ON transactions (account_hash, effective_date DESC, id DESC);
CREATE INDEX idx_tx_category ON transactions (category);
CREATE TABLE balances_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_hash TEXT NOT NULL, fetched_at TEXT NOT NULL,
    balance_type TEXT, name TEXT, amount REAL, amount_text TEXT, currency TEXT,
    reference_date TEXT, last_change TEXT, raw_json TEXT NOT NULL
);
CREATE INDEX idx_bal_acc ON balances_history (account_hash, fetched_at);
CREATE TABLE sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
    reason TEXT, result TEXT, http_status INTEGER, error_code TEXT, message TEXT,
    accounts INTEGER DEFAULT 0, new_tx INTEGER DEFAULT 0, updated_tx INTEGER DEFAULT 0,
    api_calls INTEGER DEFAULT 0
);
"""

SCHEMA_V2 = """
ALTER TABLE transactions ADD COLUMN fingerprint TEXT;
CREATE INDEX idx_tx_fingerprint ON transactions (account_hash, fingerprint);
"""

MIGRATIONS: dict[int, str] = {1: SCHEMA_V1, 2: SCHEMA_V2}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_transaction(tx: dict[str, Any]) -> dict[str, Any]:
    """Flatten the fields we filter/aggregate on; the raw dict is kept separately."""
    amount = tx.get("transaction_amount") or {}
    amount_text = str(amount.get("amount", ""))
    value = _to_float(amount_text)
    indicator = tx.get("credit_debit_indicator")
    if value is not None:
        if indicator == "DBIT":
            value = -abs(value)
        elif indicator == "CRDT":
            value = abs(value)
    outgoing = indicator == "DBIT"
    party = tx.get("creditor" if outgoing else "debtor") or {}
    party_account = tx.get("creditor_account" if outgoing else "debtor_account") or {}
    code = tx.get("bank_transaction_code") or {}
    balance_after = _to_float((tx.get("balance_after_transaction") or {}).get("amount"))
    content = {k: v for k, v in tx.items() if k != "transaction_id"}  # id may change between fetches
    return {
        "entry_reference": tx.get("entry_reference") or None,
        "status": tx.get("status") or "BOOK",
        "booking_date": tx.get("booking_date"),
        "value_date": tx.get("value_date"),
        "transaction_date": tx.get("transaction_date"),
        "effective_date": tx.get("booking_date") or tx.get("value_date") or tx.get("transaction_date") or "",
        "amount": value,
        "amount_text": amount_text,
        "currency": amount.get("currency"),
        "indicator": indicator,
        "counterparty": party.get("name"),
        "counterparty_iban": party_account.get("iban"),
        "remittance": " | ".join(tx.get("remittance_information") or []),
        "note": tx.get("note"),
        "mcc": tx.get("merchant_category_code"),
        "bank_code": code.get("code"),
        "bank_code_desc": code.get("description"),
        "balance_after": balance_after,
        "reference_number": tx.get("reference_number"),
        "raw_json": json.dumps(tx, ensure_ascii=False, sort_keys=True),
        "content_hash": hashlib.sha1(json.dumps(content, sort_keys=True).encode()).hexdigest(),
    }


def _normalize_text(value: str | None) -> str:
    """Whitespace-collapse + casefold, so trivial formatting differences (double
    spaces, upper/lower case) don't produce a different fingerprint for what is
    otherwise the exact same bank-reported text."""
    if not value:
        return ""
    return " ".join(value.split()).casefold()


def _normalize_amount(amount_text: str | None) -> str:
    """"6", "6.00" and 6 must all produce the same fingerprint. Falls back to the
    stripped raw text if it isn't parseable as a number (better than crashing)."""
    value = _to_float(amount_text)
    if value is not None:
        return f"{abs(value):.2f}"
    return (amount_text or "").strip()


def _fingerprint(n: dict[str, Any]) -> str:
    """Stable identity for "is this the same real-world movement", independent of
    which strong identifier (entry_reference/reference_number) the bank has
    attached to it so far - those can legitimately be null on one sync and
    populated on the next for the SAME transaction. Deliberately excludes
    balance_after (can be absent or shift) and the three raw date fields
    (replaced by the already-resolved effective_date)."""
    basis = json.dumps(
        [n["effective_date"], _normalize_amount(n["amount_text"]), n["currency"], n["indicator"],
         _normalize_text(n["counterparty"]), _normalize_text(n["counterparty_iban"]),
         _normalize_text(n["remittance"])],
        sort_keys=True,
    )
    return hashlib.sha1(basis.encode()).hexdigest()


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Store:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------ lifecycle
    def open(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn = conn
            self._migrate()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _migrate(self) -> None:
        assert self._conn is not None
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for target in range(version + 1, SCHEMA_VERSION + 1):
            with self._conn:
                self._conn.executescript(MIGRATIONS[target])
                if target == 2:
                    self._backfill_fingerprints()
                self._conn.execute(f"PRAGMA user_version={target}")
            _LOGGER.debug("Database migrated to schema %s", target)

    def _backfill_fingerprints(self) -> None:
        """Schema v2: computes the new content fingerprint for every row that
        already exists, from the structured columns already stored for it, so
        the dedup-promotion lookup in upsert_transactions() has something to
        match against on the very next sync after this migration runs."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT id, effective_date, amount_text, currency, indicator, counterparty, "
            "counterparty_iban, remittance FROM transactions"
        ).fetchall()
        self._conn.executemany(
            "UPDATE transactions SET fingerprint=? WHERE id=?",
            [(_fingerprint(dict(r)), r["id"]) for r in rows],
        )

    @staticmethod
    def delete_files(path: str) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass

    @property
    def conn(self) -> sqlite3.Connection:
        assert self._conn is not None, "Store is not open"
        return self._conn

    # ----------------------------------------------------------------- meta
    def get_meta_all(self) -> dict[str, str]:
        with self._lock:
            return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM meta")}

    def set_meta(self, values: dict[str, str | None]) -> None:
        with self._lock, self.conn:
            for key, value in values.items():
                if value is None:
                    self.conn.execute("DELETE FROM meta WHERE key=?", (key,))
                else:
                    self.conn.execute(
                        "INSERT INTO meta(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ------------------------------------------------------------- accounts
    def upsert_accounts(self, accounts: list[dict[str, Any]], now: str) -> None:
        with self._lock, self.conn:
            for a in accounts:
                self.conn.execute(
                    "INSERT INTO accounts(hash, uid, title, iban, currency, first_seen) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(hash) DO UPDATE SET uid=excluded.uid, title=excluded.title, "
                    "iban=excluded.iban, currency=excluded.currency",
                    (a["identification_hash"], a["uid"], a["title"], a.get("iban"), a.get("currency"), now))

    def accounts_needing_initial(self) -> list[str]:
        with self._lock:
            return [r["hash"] for r in self.conn.execute(
                "SELECT hash FROM accounts WHERE initial_sync_at IS NULL")]

    def mark_initial_done(self, account_hash: str, now: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE accounts SET initial_sync_at=COALESCE(initial_sync_at, ?) WHERE hash=?",
                (now, account_hash))

    def has_details(self, account_hash: str) -> bool:
        with self._lock:
            row = self.conn.execute("SELECT details_json FROM accounts WHERE hash=?", (account_hash,)).fetchone()
            return bool(row and row["details_json"])

    def set_details(self, account_hash: str, details: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE accounts SET details_json=? WHERE hash=?",
                              (json.dumps(details, ensure_ascii=False), account_hash))

    def latest_effective_date(self, account_hash: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(effective_date) AS d FROM transactions WHERE account_hash=? AND status!='PDNG' "
                "AND effective_date!=''", (account_hash,)).fetchone()
            return row["d"] if row else None

    # ------------------------------------------------------------- balances
    def add_balances(self, account_hash: str, balances: list[dict[str, Any]], fetched_at: str) -> None:
        with self._lock, self.conn:
            for b in balances:
                amt = b.get("balance_amount") or {}
                self.conn.execute(
                    "INSERT INTO balances_history(account_hash, fetched_at, balance_type, name, amount, "
                    "amount_text, currency, reference_date, last_change, raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (account_hash, fetched_at, b.get("balance_type"), b.get("name"), _to_float(amt.get("amount")),
                     str(amt.get("amount", "")), amt.get("currency"), b.get("reference_date"),
                     b.get("last_change_date_time"), json.dumps(b, ensure_ascii=False)))

    def latest_balances(self, account_hash: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT raw_json FROM balances_history WHERE account_hash=? AND fetched_at="
                "(SELECT MAX(fetched_at) FROM balances_history WHERE account_hash=?) ORDER BY id",
                (account_hash, account_hash)).fetchall()
        return [json.loads(r["raw_json"]) for r in rows]

    # --------------------------------------------------------- transactions
    def upsert_transactions(
        self, account_hash: str, txs: list[dict[str, Any]], now: str,
        rules: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Insert/update; returns {"new": [normalized booked rows], "updated": n, "unchanged": n}.

        `rules` (from categories.py's load_rules), when given, are matched against
        every row and included in the INSERT - but only take effect for rows that
        are genuinely new. For a row that already exists, the ON CONFLICT clause
        below does not touch category/category_source, so whatever category was
        there before (set by a rule on an earlier sync, or by hand) is left alone;
        the computed value for that row is simply discarded by SQLite.

        Dedup-key promotion: a bank can report a transaction's strong identifier
        (entry_reference, and secondarily reference_number) as null on the first
        sync and populate it days later for the SAME movement. If that happened,
        the earlier sync already stored the row under a content-fingerprint key
        (fp:...). Minting a fresh ref:/refnum: key for the later sync would create
        a duplicate row instead of updating it. So before minting such a key we
        check whether an existing, not-yet-promoted fp:-keyed row has the same
        content fingerprint; if so we reuse ITS dedup_key, which makes the INSERT
        below land on ON CONFLICT and simply backfill entry_reference/
        reference_number onto that same row.
        """
        norm = [normalize_transaction(t) for t in txs]
        for n in norm:
            n["fingerprint"] = _fingerprint(n)

        ref_counts = Counter(n["entry_reference"] for n in norm
                             if n["entry_reference"] and n["status"] != "PDNG")
        refnum_counts = Counter(n["reference_number"] for n in norm
                                if n["reference_number"] and n["status"] != "PDNG")

        new_rows: list[dict[str, Any]] = []
        updated = unchanged = 0
        with self._lock, self.conn:
            existing_rows = self.conn.execute(
                "SELECT dedup_key, content_hash, fingerprint FROM transactions "
                "WHERE account_hash=? AND status!='PDNG'", (account_hash,)).fetchall()
            existing = {r["dedup_key"]: r["content_hash"] for r in existing_rows}
            # Only rows not yet promoted to a strong-identifier key are reuse candidates.
            fp_to_dedup_key = {
                r["fingerprint"]: r["dedup_key"] for r in existing_rows
                if r["fingerprint"] and r["dedup_key"].startswith("fp:")
            }

            # Pending rows are volatile: drop and recreate from this fetch.
            self.conn.execute("DELETE FROM transactions WHERE account_hash=? AND status='PDNG'", (account_hash,))

            seen: Counter[str] = Counter()
            rows: list[dict[str, Any]] = []
            for n in reversed(norm):  # oldest first, so newer rows get higher ids
                fp = n["fingerprint"]
                if n["status"] == "PDNG":
                    seen["p" + fp] += 1
                    n["dedup_key"] = f"pdng:{fp}#{seen['p' + fp]}"
                elif n["entry_reference"] and ref_counts[n["entry_reference"]] == 1:
                    n["dedup_key"] = fp_to_dedup_key.get(fp, f"ref:{n['entry_reference']}")
                elif n["reference_number"] and refnum_counts[n["reference_number"]] == 1:
                    n["dedup_key"] = fp_to_dedup_key.get(fp, f"refnum:{n['reference_number']}")
                else:  # no (or duplicated) strong identifier: content fingerprint + occurrence number
                    seen[fp] += 1
                    n["dedup_key"] = f"fp:{fp}#{seen[fp]}"
                category = match_category(rules, n["counterparty"], n["remittance"], n["mcc"]) if rules else None
                n["category"] = category
                n["category_source"] = "rule" if category else None
                rows.append(n)

            for n in rows:
                if n["status"] != "PDNG":
                    if n["dedup_key"] not in existing:
                        new_rows.append(n)
                    elif existing[n["dedup_key"]] != n["content_hash"]:
                        updated += 1
                    else:
                        unchanged += 1
                self.conn.execute(
                    """INSERT INTO transactions (account_hash, dedup_key, entry_reference, status, booking_date,
                        value_date, transaction_date, effective_date, amount, amount_text, currency, indicator,
                        counterparty, counterparty_iban, remittance, note, mcc, bank_code, bank_code_desc,
                        balance_after, reference_number, raw_json, content_hash, fingerprint, category,
                        category_source, first_seen, last_seen)
                       VALUES (:account_hash, :dedup_key, :entry_reference, :status, :booking_date, :value_date,
                        :transaction_date, :effective_date, :amount, :amount_text, :currency, :indicator,
                        :counterparty, :counterparty_iban, :remittance, :note, :mcc, :bank_code, :bank_code_desc,
                        :balance_after, :reference_number, :raw_json, :content_hash, :fingerprint, :category,
                        :category_source, :now, :now)
                       ON CONFLICT(account_hash, dedup_key) DO UPDATE SET
                        entry_reference=excluded.entry_reference,
                        status=excluded.status, booking_date=excluded.booking_date, value_date=excluded.value_date,
                        transaction_date=excluded.transaction_date, effective_date=excluded.effective_date,
                        amount=excluded.amount, amount_text=excluded.amount_text, currency=excluded.currency,
                        indicator=excluded.indicator, counterparty=excluded.counterparty,
                        counterparty_iban=excluded.counterparty_iban, remittance=excluded.remittance,
                        note=excluded.note, mcc=excluded.mcc, bank_code=excluded.bank_code,
                        bank_code_desc=excluded.bank_code_desc, balance_after=excluded.balance_after,
                        reference_number=excluded.reference_number, raw_json=excluded.raw_json,
                        revisions=transactions.revisions + (transactions.content_hash != excluded.content_hash),
                        content_hash=excluded.content_hash, fingerprint=excluded.fingerprint,
                        last_seen=excluded.last_seen""",
                    {**n, "account_hash": account_hash, "now": now})
        return {"new": new_rows, "updated": updated, "unchanged": unchanged}

    def has_transaction_id(self, transaction_id: int) -> bool:
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM transactions WHERE id=?", (transaction_id,)
            ).fetchone() is not None

    def query_transactions(
        self,
        account_hashes: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        direction: str | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        text: str | None = None,
        category: str | None = None,
        include_pending: bool = False,
        limit: int = 50,
        offset: int = 0,
        order: str = "desc",
        include_raw: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        where, params = [], []
        if account_hashes is not None:
            where.append("account_hash IN (%s)" % ",".join("?" * len(account_hashes)) if account_hashes else "0")
            params += account_hashes
        if not include_pending:
            where.append("status!='PDNG'")
        if date_from:
            where.append("effective_date>=?"); params.append(date_from)
        if date_to:
            where.append("effective_date<=?"); params.append(date_to)
        if direction == "income":
            where.append("amount>0")
        elif direction == "expense":
            where.append("amount<0")
        if min_amount is not None:
            where.append("ABS(amount)>=?"); params.append(min_amount)
        if max_amount is not None:
            where.append("ABS(amount)<=?"); params.append(max_amount)
        if text:
            where.append("(counterparty LIKE ? ESCAPE '\\' OR remittance LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\')")
            params += [_like(text)] * 3
        if category:
            if category.lower() in ("none", "uncategorized"):
                where.append("category IS NULL")
            else:
                where.append("category=?"); params.append(category)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        direction_sql = "ASC" if order == "asc" else "DESC"
        columns = ("id, account_hash, status, entry_reference, booking_date, value_date, transaction_date, "
                   "effective_date, amount, currency, indicator, counterparty, counterparty_iban, remittance, "
                   "note, mcc, bank_code, bank_code_desc, balance_after, reference_number, category, "
                   "category_source, first_seen, revisions" + (", raw_json" if include_raw else ""))
        with self._lock:
            total = self.conn.execute(f"SELECT COUNT(*) FROM transactions {clause}", params).fetchone()[0]
            rows = self.conn.execute(
                f"SELECT {columns} FROM transactions {clause} "
                f"ORDER BY effective_date {direction_sql}, id {direction_sql} LIMIT ? OFFSET ?",
                [*params, limit, offset]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if include_raw:
                d["raw"] = json.loads(d.pop("raw_json"))
            out.append(d)
        return out, total

    # ------------------------------------------------------------- snapshot
    def snapshot(self, account_hashes: list[str]) -> dict[str, dict[str, Any]]:
        """Everything the entities need, read from local data only."""
        result: dict[str, dict[str, Any]] = {}
        for h in account_hashes:
            with self._lock:
                acc = self.conn.execute("SELECT details_json FROM accounts WHERE hash=?", (h,)).fetchone()
                last = self.conn.execute(
                    "SELECT raw_json, category FROM transactions WHERE account_hash=? AND status!='PDNG' "
                    "ORDER BY effective_date DESC, id DESC LIMIT 1", (h,)).fetchone()
                count = self.conn.execute(
                    "SELECT COUNT(*) FROM transactions WHERE account_hash=? AND status!='PDNG'", (h,)).fetchone()[0]
                pending = self.conn.execute(
                    "SELECT COUNT(*) FROM transactions WHERE account_hash=? AND status='PDNG'", (h,)).fetchone()[0]
            result[h] = {
                "details": json.loads(acc["details_json"]) if acc and acc["details_json"] else {},
                "balances": self.latest_balances(h),
                "last_transaction": json.loads(last["raw_json"]) if last else None,
                "last_transaction_category": last["category"] if last else None,
                "transaction_count": count,
                "pending_count": pending,
            }
        return result

    # ------------------------------------------------------------- sync log
    def start_run(self, reason: str, now: str) -> int:
        with self._lock, self.conn:
            return self.conn.execute(
                "INSERT INTO sync_log(started_at, reason, result) VALUES(?,?,'running')", (now, reason)).lastrowid

    def finish_run(self, run_id: int, now: str, result: str, http_status: int | None, error_code: str | None,
                   message: str | None, accounts: int, new_tx: int, updated_tx: int, api_calls: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE sync_log SET finished_at=?, result=?, http_status=?, error_code=?, message=?, accounts=?, "
                "new_tx=?, updated_tx=?, api_calls=? WHERE id=?",
                (now, result, http_status, error_code, (message or "")[:500], accounts, new_tx, updated_tx,
                 api_calls, run_id))

    def runs_since(self, since_iso: str) -> list[str]:
        """Start times of runs that actually fetched data (rate-limited / expired runs don't count)."""
        with self._lock:
            return [r["started_at"] for r in self.conn.execute(
                "SELECT started_at FROM sync_log WHERE started_at>=? AND result IN ('ok','partial','error','running') "
                "ORDER BY started_at", (since_iso,))]

    def last_run_started(self) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT MAX(started_at) AS s FROM sync_log").fetchone()
            return row["s"] if row else None

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,))]
