#!/usr/bin/env python3
"""Отчёт по оплате договоров подрядчикам на заданную дату.

По каждой работе: цена, выплачено, осталось оплатить.
По каждому договору: итоги.

Запуск:
    python3 contract_report.py --date 10.11.2026
    python3 contract_report.py --date 2026-11-10 --csv-dir out/
    python3 contract_report.py --date 10.11.2026 --json

Только стандартная библиотека Python 3.9+. Все деньги - Decimal, без float.
Решения и допущения описаны в DECISIONS.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

OVERPAY_ROW_TITLE = "Переплата, не отнесённая на работы"

STATUS_RU = {
    "done": "выполнена",
    "planned": "в плане",
    "cancelled": "отменена",
}


class DataError(Exception):
    """Данные в файлах повреждены или противоречивы настолько, что считать нельзя."""


# --------------------------------------------------------------------------
# Разбор значений
# --------------------------------------------------------------------------

def parse_money(raw: str, where: str) -> Decimal:
    """Сумма строго с точностью до копейки; всё остальное - ошибка, а не округление."""
    text = (raw or "").strip().replace(" ", "").replace(" ", "")
    if not text:
        raise DataError(f"{where}: пустая сумма")
    try:
        value = Decimal(text.replace(",", "."))
    except InvalidOperation:
        raise DataError(f"{where}: не число: {raw!r}") from None
    if not value.is_finite():
        raise DataError(f"{where}: недопустимая сумма: {raw!r}")
    if value != value.quantize(CENT):
        raise DataError(f"{where}: сумма точнее копейки: {raw!r}")
    if value <= 0:
        raise DataError(f"{where}: сумма должна быть положительной: {raw!r}")
    return value.quantize(CENT)


def parse_date(raw: str, where: str = "дата") -> date:
    """Принимает ГГГГ-ММ-ДД и ДД.ММ.ГГГГ."""
    text = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise DataError(f"{where}: не удалось разобрать дату {raw!r} (нужно ДД.ММ.ГГГГ или ГГГГ-ММ-ДД)")


def parse_opt_date(raw: str, where: str) -> Optional[date]:
    return parse_date(raw, where) if (raw or "").strip() else None


def fmt_date(d: Optional[date]) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def fmt_money(d: Decimal) -> str:
    """100000.5 -> '100 000,50'."""
    s = f"{d:,.2f}"  # 100,000.50
    return s.replace(",", " ").replace(".", ",")


def csv_money(d: Decimal) -> str:
    """Для CSV под русский Excel: без разделителей тысяч, запятая как десятичный знак."""
    return f"{d:.2f}".replace(".", ",")


# --------------------------------------------------------------------------
# Модель данных
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: str
    completed_at: Optional[date]
    cancelled_at: Optional[date]


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: Optional[str]
    purpose: str
    amount: Decimal
    status: str
    created_at: date
    paid_at: Optional[date]
    refunded_at: Optional[date]


@dataclass
class WorkRow:
    work_id: str
    title: str
    status: str                 # состояние на дату отчёта: done / planned / cancelled
    price: Decimal              # цена по выгрузке
    price_counted: Decimal      # цена, которая входит в итоги (у отменённой - 0)
    paid_linked: Decimal        # платежи, привязанные к работе явно
    paid_allocated: Decimal     # часть платежей по договору, отнесённая на работу
    paid: Decimal               # всего выплачено по работе
    remaining: Decimal          # осталось оплатить
    overpaid: Decimal           # выплачено больше цены (или оплата по отменённой работе)
    note: str = ""


@dataclass
class ContractRow:
    contract_id: str
    contractor: str
    signed_at: date
    amount: Decimal             # сумма договора по документу
    works_price: Decimal        # цена действующих (не отменённых) работ
    cancelled_price: Decimal    # цена отменённых работ
    paid_gross: Decimal         # деньги, ушедшие подрядчику платежами к дате
    refunded: Decimal           # из них возвращено подрядчиком к дате
    paid: Decimal               # выплачено нетто = paid_gross - refunded
    paid_for_done: Decimal      # из выплаченного: за принятые работы
    advance_for_planned: Decimal  # из выплаченного: аванс за ещё не принятые работы
    remaining: Decimal          # осталось оплатить по действующим работам
    overpaid: Decimal           # выплачено сверх цены работ
    queued: Decimal             # платежи в очереди (деньги ещё не ушли)
    remaining_after_queue: Decimal
    unallocated: Decimal        # выплачено сверх цены всех действующих работ: не отнесено ни на одну работу
    works: List[WorkRow] = field(default_factory=list)
    queued_ids: List[str] = field(default_factory=list)


@dataclass
class Report:
    as_of: date
    contracts: List[ContractRow]
    warnings: List[str]


# --------------------------------------------------------------------------
# Загрузка
# --------------------------------------------------------------------------

def _read_csv(path: Path, required: List[str]) -> List[Dict[str, str]]:
    if not path.exists():
        raise DataError(f"нет файла {path}")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise DataError(f"{path.name}: нет колонок {', '.join(missing)}")
        rows = []
        for row in reader:
            rows.append({k: (v or "").strip() for k, v in row.items() if k is not None})
        return rows


def load_data(data_dir: Path):
    contracts: List[Contract] = []
    for r in _read_csv(data_dir / "contracts.csv", ["contract_id", "contractor", "amount", "signed_at"]):
        cid = r["contract_id"]
        contracts.append(Contract(
            cid, r["contractor"],
            parse_money(r["amount"], f"договор {cid}"),
            parse_date(r["signed_at"], f"договор {cid}: signed_at"),
        ))

    works: List[Work] = []
    for r in _read_csv(data_dir / "works.csv", [
        "work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"]):
        wid = r["work_id"]
        status = r["status"]
        if status not in STATUS_RU:
            raise DataError(f"работа {wid}: неизвестный статус {status!r}")
        completed = parse_opt_date(r["completed_at"], f"работа {wid}: completed_at")
        cancelled = parse_opt_date(r["cancelled_at"], f"работа {wid}: cancelled_at")
        if status == "done" and not completed:
            raise DataError(f"работа {wid}: статус done, но нет completed_at")
        if status == "cancelled" and not cancelled:
            raise DataError(f"работа {wid}: статус cancelled, но нет cancelled_at")
        works.append(Work(wid, r["contract_id"], r["title"],
                          parse_money(r["price"], f"работа {wid}"), status, completed, cancelled))

    payments: List[Payment] = []
    for r in _read_csv(data_dir / "payments.csv", [
        "payment_id", "contract_id", "work_id", "purpose", "amount", "status",
        "created_at", "paid_at", "refunded_at"]):
        pid = r["payment_id"]
        status = r["status"]
        if status not in ("paid", "queued", "refunded"):
            raise DataError(f"платёж {pid}: неизвестный статус {status!r}")
        paid_at = parse_opt_date(r["paid_at"], f"платёж {pid}: paid_at")
        refunded_at = parse_opt_date(r["refunded_at"], f"платёж {pid}: refunded_at")
        if status in ("paid", "refunded") and not paid_at:
            raise DataError(f"платёж {pid}: статус {status}, но нет paid_at")
        if status == "refunded" and not refunded_at:
            raise DataError(f"платёж {pid}: статус refunded, но нет refunded_at")
        if status == "queued" and (paid_at or refunded_at):
            raise DataError(f"платёж {pid}: в очереди, но есть даты оплаты/возврата")
        payments.append(Payment(
            pid, r["contract_id"], r["work_id"] or None, r["purpose"],
            parse_money(r["amount"], f"платёж {pid}"), status,
            parse_date(r["created_at"], f"платёж {pid}: created_at"), paid_at, refunded_at))

    _check_integrity(contracts, works, payments)
    return contracts, works, payments


def _check_integrity(contracts, works, payments) -> None:
    for kind, items, attr in (("договор", contracts, "contract_id"),
                              ("работа", works, "work_id"),
                              ("платёж", payments, "payment_id")):
        seen = set()
        for it in items:
            key = getattr(it, attr)
            if key in seen:
                raise DataError(f"{kind} {key} встречается дважды")
            seen.add(key)
    cids = {c.contract_id for c in contracts}
    wmap = {w.work_id: w for w in works}
    for w in works:
        if w.contract_id not in cids:
            raise DataError(f"работа {w.work_id}: неизвестный договор {w.contract_id}")
    for p in payments:
        if p.contract_id not in cids:
            raise DataError(f"платёж {p.payment_id}: неизвестный договор {p.contract_id}")
        if p.work_id:
            w = wmap.get(p.work_id)
            if w is None:
                raise DataError(f"платёж {p.payment_id}: неизвестная работа {p.work_id}")
            if w.contract_id != p.contract_id:
                raise DataError(
                    f"платёж {p.payment_id}: привязан к работе {w.work_id} договора "
                    f"{w.contract_id}, а сам записан на договор {p.contract_id}")


# --------------------------------------------------------------------------
# Состояние на дату
# --------------------------------------------------------------------------

def work_state(w: Work, as_of: date) -> str:
    """Статус работы на дату отчёта. Выгрузка показывает «сейчас», поэтому смотрим на даты."""
    if w.status == "cancelled" and w.cancelled_at <= as_of:
        return "cancelled"
    if w.status == "done" and w.completed_at <= as_of:
        return "done"
    return "planned"


def money_out(p: Payment, as_of: date) -> Decimal:
    """Сколько денег этот платёж забрал у нас к дате (до учёта возврата)."""
    if p.status == "queued":
        return ZERO
    return p.amount if p.paid_at <= as_of else ZERO


def money_back(p: Payment, as_of: date) -> Decimal:
    """Сколько из него подрядчик уже вернул к дате."""
    if p.status == "refunded" and p.refunded_at <= as_of and p.paid_at <= as_of:
        return p.amount
    return ZERO


def is_queued(p: Payment, as_of: date) -> bool:
    """Платёж заведён к дате, а деньги к этой дате ещё не ушли.

    Это и платёж, который в выгрузке так и остался в очереди, и платёж, который
    потом был оплачен (paid_at позже даты отчёта): на дату отчёта он ждал оплаты.
    """
    if p.created_at > as_of:
        return False
    return p.status == "queued" or (p.paid_at is not None and p.paid_at > as_of)


# --------------------------------------------------------------------------
# Расчёт
# --------------------------------------------------------------------------

def build_report(contracts: List[Contract], works: List[Work], payments: List[Payment],
                 as_of: date) -> Report:
    warnings: List[str] = []
    rows: List[ContractRow] = []

    latest = max([p.created_at for p in payments] + [p.paid_at for p in payments if p.paid_at]
                 + [p.refunded_at for p in payments if p.refunded_at]
                 + [w.completed_at for w in works if w.completed_at]
                 + [w.cancelled_at for w in works if w.cancelled_at], default=None)
    if latest and as_of > latest:
        warnings.append(
            f"Дата отчёта {fmt_date(as_of)} позже последней даты в выгрузке ({fmt_date(latest)}): "
            "платежи из очереди могли уйти, а работы - измениться после выгрузки.")

    for c in sorted(contracts, key=lambda x: x.contract_id):
        if c.signed_at > as_of:
            warnings.append(f"Договор {c.contract_id} подписан {fmt_date(c.signed_at)}, "
                            f"после даты отчёта - в отчёт не включён.")
            continue
        rows.append(_contract_row(c, [w for w in works if w.contract_id == c.contract_id],
                                  [p for p in payments if p.contract_id == c.contract_id],
                                  as_of, warnings))
    return Report(as_of, rows, warnings)


def _contract_row(c: Contract, works: List[Work], payments: List[Payment],
                  as_of: date, warnings: List[str]) -> ContractRow:
    states = {w.work_id: work_state(w, as_of) for w in works}

    # --- деньги по договору на дату ---
    gross = sum((money_out(p, as_of) for p in payments), ZERO)
    refunded = sum((money_back(p, as_of) for p in payments), ZERO)
    queued_list = [p for p in payments if is_queued(p, as_of)]
    queued = sum((p.amount for p in queued_list), ZERO)

    def net(p: Payment) -> Decimal:
        return money_out(p, as_of) - money_back(p, as_of)

    linked_by_work: Dict[str, Decimal] = {}
    linked_ids: Dict[str, List[str]] = {}
    pool = ZERO  # платежи без привязки к работе, нетто
    for p in payments:
        n = net(p)
        if p.work_id:
            linked_by_work[p.work_id] = linked_by_work.get(p.work_id, ZERO) + n
            if n > 0:
                linked_ids.setdefault(p.work_id, []).append(p.payment_id)
        else:
            pool += n

    # --- порядок погашения: сначала принятые (по дате приёмки), потом ещё не принятые ---
    active = [w for w in works if states[w.work_id] != "cancelled"]
    done = sorted((w for w in active if states[w.work_id] == "done"),
                  key=lambda w: (w.completed_at, w.work_id))
    planned = sorted((w for w in active if states[w.work_id] == "planned"),
                     key=lambda w: w.work_id)
    order = done + planned

    allocated: Dict[str, Decimal] = {}
    for w in order:
        need = max(ZERO, w.price - linked_by_work.get(w.work_id, ZERO))
        take = min(pool, need)
        allocated[w.work_id] = take
        pool -= take
    unallocated = pool  # денег больше, чем стоит всё действующее: переплата по договору

    # --- строки по работам ---
    work_rows: List[WorkRow] = []
    for w in sorted(works, key=lambda x: x.work_id):
        st = states[w.work_id]
        counted = ZERO if st == "cancelled" else w.price
        linked = linked_by_work.get(w.work_id, ZERO)
        alloc = allocated.get(w.work_id, ZERO)
        paid = linked + alloc
        remaining = max(ZERO, counted - paid)
        overpaid = max(ZERO, paid - counted)
        work_rows.append(WorkRow(
            w.work_id, w.title, st, w.price, counted, linked, alloc, paid, remaining, overpaid,
            _work_note(w, st, linked, alloc, linked_ids.get(w.work_id, []), as_of, overpaid)))

    paid_total = gross - refunded
    works_price = sum((r.price_counted for r in work_rows), ZERO)
    cancelled_price = sum((r.price for r in work_rows if r.status == "cancelled"), ZERO)
    remaining_total = sum((r.remaining for r in work_rows), ZERO)
    overpaid_total = sum((r.overpaid for r in work_rows), ZERO) + unallocated
    paid_done = sum((r.paid for r in work_rows if r.status == "done"), ZERO)
    paid_planned = sum((r.paid for r in work_rows if r.status == "planned"), ZERO)

    # самопроверка: деньги не потерялись и не появились
    assert sum((r.paid for r in work_rows), ZERO) + unallocated == paid_total, "баланс платежей не сходится"
    assert remaining_total - overpaid_total == works_price - paid_total, "баланс остатка не сходится"

    row = ContractRow(
        c.contract_id, c.contractor, c.signed_at, c.amount, works_price, cancelled_price,
        gross, refunded, paid_total, paid_done, paid_planned, remaining_total, overpaid_total,
        queued, max(ZERO, remaining_total - queued), unallocated, work_rows,
        [p.payment_id for p in queued_list])

    _contract_warnings(c, row, work_rows, payments, works, as_of, warnings)
    return row


def _work_note(w: Work, st: str, linked: Decimal, alloc: Decimal, linked_ids: List[str],
               as_of: date, overpaid: Decimal) -> str:
    parts: List[str] = []
    if st == "cancelled":
        parts.append(f"отменена {fmt_date(w.cancelled_at)}, в итоги не входит")
        if linked > 0:
            parts.append(f"оплата по отменённой работе {fmt_money(linked)} ({', '.join(linked_ids)}) - "
                         "нужен возврат или зачёт")
        return "; ".join(parts)
    if st == "planned" and w.status == "done":
        parts.append(f"по выгрузке принята {fmt_date(w.completed_at)}, т.е. после даты отчёта")
    if st == "planned" and alloc > 0:
        parts.append("аванс: работа ещё не принята")
    if linked > 0:
        parts.append(f"в т.ч. {fmt_money(linked)} - платёж {', '.join(linked_ids)} привязан к работе")
    if overpaid > 0:
        parts.append(f"переплата {fmt_money(overpaid)}")
    return "; ".join(parts)


def _contract_warnings(c: Contract, row: ContractRow, work_rows: List[WorkRow],
                       payments: List[Payment], works: List[Work], as_of: date,
                       warnings: List[str]) -> None:
    cid = c.contract_id
    if row.works_price != c.amount:
        cancelled_ids = [r.work_id for r in work_rows if r.status == "cancelled"]
        extra = f" Причина - отменённые работы: {', '.join(cancelled_ids)}." if cancelled_ids else ""
        warnings.append(
            f"{cid}: сумма договора {fmt_money(c.amount)} не равна цене действующих работ "
            f"{fmt_money(row.works_price)} (разница {fmt_money(c.amount - row.works_price)}).{extra} "
            "В отчёте остаток считается по ценам работ; подтвердите, изменился ли договор.")
    if row.paid > c.amount:
        warnings.append(f"{cid}: выплачено {fmt_money(row.paid)} - больше суммы договора "
                        f"{fmt_money(c.amount)} на {fmt_money(row.paid - c.amount)}.")
    elif row.overpaid > 0:
        warnings.append(f"{cid}: выплачено на {fmt_money(row.overpaid)} больше, чем стоят действующие работы.")
    if row.queued > 0:
        warnings.append(f"{cid}: в очереди платежи на {fmt_money(row.queued)} "
                        f"({', '.join(row.queued_ids)}) - деньги ещё не ушли, в «выплачено» не входят.")
    if row.advance_for_planned > 0:
        warnings.append(f"{cid}: {fmt_money(row.advance_for_planned)} выплачено вперёд за работы, "
                        "которые на дату отчёта ещё не приняты.")
    for p in payments:
        if p.work_id and p.paid_at and p.paid_at <= as_of:
            warnings.append(f"{cid}: платёж {p.payment_id} ({fmt_money(p.amount)}, «{p.purpose}») "
                            f"привязан к работе {p.work_id} и засчитан в её оплату - подтвердите, "
                            "что это часть цены работы, а не отдельный расход.")
        if p.paid_at and p.paid_at < c.signed_at:
            warnings.append(f"{cid}: платёж {p.payment_id} от {fmt_date(p.paid_at)} раньше подписания "
                            f"договора ({fmt_date(c.signed_at)}).")
        if p.paid_at and p.refunded_at and p.refunded_at < p.paid_at:
            warnings.append(f"{cid}: платёж {p.payment_id}: возврат раньше оплаты.")
    for w in works:
        for d, what in ((w.completed_at, "принята"), (w.cancelled_at, "отменена")):
            if d and d < c.signed_at:
                warnings.append(f"{cid}: работа {w.work_id} {what} {fmt_date(d)}, раньше подписания договора.")


# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------

def _table(headers: List[str], rows: List[List[str]], right: List[int]) -> List[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def line(cells: List[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            out.append(cell.rjust(widths[i]) if i in right else cell.ljust(widths[i]))
        return "  ".join(out).rstrip()

    return [line(headers), "  ".join("-" * w for w in widths)] + [line(r) for r in rows]


def render_text(rep: Report) -> str:
    out: List[str] = [f"ОТЧЁТ ПО ОПЛАТЕ ДОГОВОРОВ НА {fmt_date(rep.as_of)}", ""]
    for c in rep.contracts:
        out.append(f"ДОГОВОР {c.contract_id} - {c.contractor} (подписан {fmt_date(c.signed_at)})")
        body = []
        for w in c.works:
            body.append([w.work_id, w.title, STATUS_RU[w.status], fmt_money(w.price_counted) if w.status != "cancelled" else "-",
                         fmt_money(w.paid), fmt_money(w.remaining), w.note])
        if c.unallocated > 0:
            body.append(["", OVERPAY_ROW_TITLE, "", "-", fmt_money(c.unallocated), "-",
                         "деньги сверх цены всех действующих работ"])
        body.append(["", "ИТОГО по работам", "", fmt_money(c.works_price), fmt_money(c.paid),
                     fmt_money(c.remaining), ""])
        out += _table(["Работа", "Название", "Статус", "Цена", "Выплачено", "Осталось", "Примечание"],
                      body, right=[3, 4, 5])
        out += ["", "  Итоги по договору:"]
        summary = [
            ("Сумма договора (по документу)", c.amount),
            ("Цена действующих работ", c.works_price),
            ("  в т.ч. отменено (не считается)", c.cancelled_price),
            ("Ушло подрядчику платежами", c.paid_gross),
            ("  из них вернул подрядчик", c.refunded),
            ("ВЫПЛАЧЕНО (нетто)", c.paid),
            ("  за принятые работы", c.paid_for_done),
            ("  вперёд, за не принятые работы", c.advance_for_planned),
            ("ОСТАЛОСЬ ОПЛАТИТЬ", c.remaining),
            ("Платежи в очереди (деньги не ушли)", c.queued),
            ("Останется после оплаты очереди", c.remaining_after_queue),
        ]
        if c.overpaid > 0:
            summary.append(("ПЕРЕПЛАТА", c.overpaid))
        w = max(len(k) for k, _ in summary)
        vw = max(len(fmt_money(v)) for _, v in summary)
        out += [f"    {k.ljust(w)}  {fmt_money(v).rjust(vw)}" for k, v in summary]
        out.append("")
    total_paid = sum((c.paid for c in rep.contracts), ZERO)
    total_rem = sum((c.remaining for c in rep.contracts), ZERO)
    out.append(f"ВСЕГО по договорам: выплачено {fmt_money(total_paid)}, осталось оплатить {fmt_money(total_rem)}")
    out.append("")
    if rep.warnings:
        out.append("ЧТО ТРЕБУЕТ ВНИМАНИЯ / ПОДТВЕРЖДЕНИЯ:")
        out += [f"  {i}. {t}" for i, t in enumerate(rep.warnings, 1)]
    return "\n".join(out) + "\n"


WORK_COLS = ["Договор", "Работа", "Название", "Статус на дату", "Цена", "Цена в итогах",
             "Выплачено", "из них привязано к работе", "из них отнесено с договора",
             "Осталось оплатить", "Переплата", "Примечание"]
CONTRACT_COLS = ["Договор", "Подрядчик", "Подписан", "Сумма договора", "Цена действующих работ",
                 "Отменено", "Ушло платежами", "Возвращено", "Выплачено нетто", "За принятые работы",
                 "Вперёд за не принятые", "Осталось оплатить", "Переплата", "В очереди",
                 "Останется после очереди"]


def write_csv(rep: Report, directory: Path) -> List[Path]:
    """CSV для русского Excel: разделитель «;», запятая в суммах, UTF-8 с BOM."""
    directory.mkdir(parents=True, exist_ok=True)
    wp = directory / f"works_{rep.as_of.isoformat()}.csv"
    cp = directory / f"contracts_{rep.as_of.isoformat()}.csv"
    with wp.open("w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.writer(fh, delimiter=";")
        wr.writerow(WORK_COLS)
        for c in rep.contracts:
            for w in c.works:
                wr.writerow([c.contract_id, w.work_id, w.title, STATUS_RU[w.status], csv_money(w.price),
                             csv_money(w.price_counted), csv_money(w.paid), csv_money(w.paid_linked),
                             csv_money(w.paid_allocated), csv_money(w.remaining), csv_money(w.overpaid), w.note])
            if c.unallocated > 0:
                wr.writerow([c.contract_id, "", OVERPAY_ROW_TITLE, "", csv_money(ZERO), csv_money(ZERO),
                             csv_money(c.unallocated), csv_money(ZERO), csv_money(c.unallocated),
                             csv_money(ZERO), csv_money(c.unallocated),
                             "деньги сверх цены всех действующих работ"])
    with cp.open("w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.writer(fh, delimiter=";")
        wr.writerow(CONTRACT_COLS)
        for c in rep.contracts:
            wr.writerow([c.contract_id, c.contractor, fmt_date(c.signed_at), csv_money(c.amount),
                         csv_money(c.works_price), csv_money(c.cancelled_price), csv_money(c.paid_gross),
                         csv_money(c.refunded), csv_money(c.paid), csv_money(c.paid_for_done),
                         csv_money(c.advance_for_planned), csv_money(c.remaining), csv_money(c.overpaid),
                         csv_money(c.queued), csv_money(c.remaining_after_queue)])
    return [wp, cp]


def to_json(rep: Report) -> str:
    def conv(o):
        if isinstance(o, Decimal):
            return f"{o:.2f}"
        if isinstance(o, date):
            return o.isoformat()
        raise TypeError(type(o))

    from dataclasses import asdict
    return json.dumps({"as_of": rep.as_of, "contracts": [asdict(c) for c in rep.contracts],
                       "warnings": rep.warnings}, default=conv, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Отчёт по оплате договоров на дату.")
    ap.add_argument("--date", required=True, help="дата отчёта: ДД.ММ.ГГГГ или ГГГГ-ММ-ДД")
    ap.add_argument("--data", default=str(Path(__file__).resolve().parent / "data"),
                    help="папка с contracts.csv, works.csv, payments.csv (по умолчанию ./data)")
    ap.add_argument("--csv-dir", help="дополнительно записать CSV (для Excel) в эту папку")
    ap.add_argument("--json", action="store_true", help="вывести JSON вместо текста")
    args = ap.parse_args(argv)

    try:
        as_of = parse_date(args.date, "--date")
        contracts, works, payments = load_data(Path(args.data))
        rep = build_report(contracts, works, payments, as_of)
    except DataError as e:
        print(f"Ошибка данных: {e}", file=sys.stderr)
        return 2

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(to_json(rep) + "\n" if args.json else render_text(rep))
    if args.csv_dir:
        for p in write_csv(rep, Path(args.csv_dir)):
            print(f"записан файл: {p}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
