"""Тесты расчёта. Запуск: python3 -m unittest discover -s tests -v"""
import csv
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import contract_report as cr  # noqa: E402


def report(as_of: str):
    c, w, p = cr.load_data(ROOT / "data")
    rep = cr.build_report(c, w, p, cr.parse_date(as_of))
    return {r.contract_id: r for r in rep.contracts}, rep


def works(row):
    return {w.work_id: w for w in row.works}


class OfficialDate(unittest.TestCase):
    """Проверочная дата из задания: 10.11.2026."""

    def setUp(self):
        self.c, self.rep = report("10.11.2026")

    def test_d1_totals(self):
        d1 = self.c["D-1"]
        self.assertEqual(d1.amount, D("600000.00"))
        self.assertEqual(d1.works_price, D("500000.00"))
        self.assertEqual(d1.paid_gross, D("365000.00"))
        self.assertEqual(d1.refunded, D("50000.00"))     # P-2 уже вернули 10.09
        self.assertEqual(d1.paid, D("315000.00"))
        self.assertEqual(d1.remaining, D("185000.00"))
        self.assertEqual(d1.queued, D("150000.00"))       # P-4 не выплата
        self.assertEqual(d1.remaining_after_queue, D("35000.00"))
        self.assertEqual(d1.overpaid, D("0.00"))

    def test_d1_works(self):
        w = works(self.c["D-1"])
        self.assertEqual((w["W-11"].paid, w["W-11"].remaining), (D("100000.00"), D("0.00")))
        self.assertEqual((w["W-12"].paid, w["W-12"].remaining), (D("100000.00"), D("0.00")))
        # отменённой работе деньги не достаются, и она не входит в итоги
        self.assertEqual(w["W-13"].status, "cancelled")
        self.assertEqual((w["W-13"].paid, w["W-13"].remaining, w["W-13"].price_counted),
                         (D("0.00"), D("0.00"), D("0.00")))
        # W-14: 15 000 привязанных + 85 000 с аванса, без двойного счёта
        self.assertEqual(w["W-14"].paid_linked, D("15000.00"))
        self.assertEqual(w["W-14"].paid_allocated, D("85000.00"))
        self.assertEqual(w["W-14"].paid, D("100000.00"))
        self.assertEqual(w["W-15"].paid, D("15000.00"))
        self.assertEqual(w["W-15"].remaining, D("85000.00"))
        self.assertEqual(w["W-16"].remaining, D("100000.00"))

    def test_d2_future_events_ignored(self):
        d2 = self.c["D-2"]
        w = works(d2)
        self.assertEqual(d2.paid, D("75000.00"))          # P-8 от 12.11 ещё не было
        self.assertEqual(d2.remaining, D("15000.00"))
        self.assertEqual(w["W-23"].status, "planned")     # принята только 11.11
        self.assertEqual(w["W-23"].paid, D("25000.00"))
        self.assertEqual(w["W-23"].remaining, D("15000.00"))
        self.assertEqual(d2.overpaid, D("0.00"))

    def test_warnings_mention_key_issues(self):
        text = "\n".join(self.rep.warnings)
        self.assertIn("P-4", text)
        self.assertIn("P-3", text)
        self.assertIn("W-13", text)


class OtherDates(unittest.TestCase):
    def test_refund_not_yet_happened(self):
        c, _ = report("2026-09-05")                       # P-2 ушёл 02.09, вернулся 10.09
        d1 = c["D-1"]
        self.assertEqual(d1.paid, D("350000.00"))
        self.assertEqual(d1.refunded, D("0.00"))
        # все работы ещё не приняты, W-13 ещё не отменена: платёж идёт по порядку номеров
        w = works(d1)
        self.assertEqual([w[i].paid for i in ("W-11", "W-12", "W-13", "W-14", "W-15", "W-16")],
                         [D("100000.00")] * 3 + [D("50000.00"), D("0.00"), D("0.00")])

    def test_refund_done(self):
        c, _ = report("2026-09-10")                       # день возврата включается
        self.assertEqual(c["D-1"].paid, D("300000.00"))

    def test_queue_appears_on_creation_date(self):
        self.assertEqual(report("2026-10-31")[0]["D-1"].queued, D("0.00"))
        self.assertEqual(report("2026-11-01")[0]["D-1"].queued, D("150000.00"))

    def test_queue_includes_payment_created_but_not_yet_paid_0831(self):
        # P-1 заведён 30.08, деньги ушли 01.09: на 31.08 он ждёт оплаты
        c, rep = report("2026-08-31")
        d1 = c["D-1"]
        self.assertEqual(d1.queued, D("300000.00"))
        self.assertEqual(d1.queued_ids, ["P-1"])
        self.assertEqual((d1.paid_gross, d1.refunded, d1.paid), (D("0.00"),) * 3)
        self.assertEqual(d1.remaining, D("600000.00"))
        self.assertEqual(d1.remaining_after_queue, D("300000.00"))
        self.assertNotIn("D-2", c)                         # D-2 подписан 20.09

    def test_queue_includes_payment_created_but_not_yet_paid_1024(self):
        # P-3 заведён 24.10, деньги ушли 25.10
        c, _ = report("2026-10-24")
        d1, d2 = c["D-1"], c["D-2"]
        self.assertEqual(d1.queued, D("15000.00"))
        self.assertEqual(d1.queued_ids, ["P-3"])
        self.assertEqual(d1.paid, D("300000.00"))
        self.assertEqual(d1.remaining, D("200000.00"))
        self.assertEqual(d1.remaining_after_queue, D("185000.00"))
        self.assertEqual((d2.queued, d2.paid), (D("0.00"), D("45000.00")))

    def test_queue_includes_payment_created_but_not_yet_paid_1029(self):
        # P-6 заведён 29.10, деньги ушли 30.10
        c, _ = report("2026-10-29")
        d2 = c["D-2"]
        self.assertEqual(d2.queued, D("25000.00"))
        self.assertEqual(d2.queued_ids, ["P-6"])
        self.assertEqual(d2.paid, D("45000.00"))
        self.assertEqual(d2.remaining, D("45000.00"))
        self.assertEqual(d2.remaining_after_queue, D("20000.00"))
        self.assertEqual(c["D-1"].queued, D("0.00"))       # P-3 уже оплачен, P-4 ещё не заведён

    def test_queue_ends_on_paid_date(self):
        # в день оплаты платёж уже не очередь, а выплата (событие в день отчёта состоялось)
        c, _ = report("2026-10-30")
        self.assertEqual(c["D-2"].queued, D("0.00"))
        self.assertEqual(c["D-2"].paid, D("70000.00"))

    def test_after_data_overpayment_d2(self):
        c, rep = report("2026-11-12")
        d2 = c["D-2"]
        self.assertEqual(d2.paid, D("95000.00"))
        self.assertEqual(d2.overpaid, D("5000.00"))
        self.assertEqual(d2.remaining, D("0.00"))
        self.assertEqual(works(d2)["W-23"].status, "done")
        self.assertEqual(works(d2)["W-23"].paid, D("40000.00"))
        self.assertTrue(any("D-2" in t and "больше суммы договора" in t for t in rep.warnings))

    def test_date_beyond_data_warns(self):
        _, rep = report("2027-01-01")
        self.assertTrue(any("позже последней даты" in t for t in rep.warnings))

    def test_contract_not_signed_yet_is_skipped(self):
        c, rep = report("2026-09-01")
        self.assertNotIn("D-2", c)
        self.assertIn("D-1", c)

    def test_invariants_on_many_dates(self):
        day = date(2026, 8, 20)
        while day <= date(2026, 12, 5):
            as_of = day.isoformat()
            c, _ = report(as_of)
            for row in c.values():
                # деньги не теряются: по работам + не отнесённое на работы = выплачено нетто
                self.assertEqual(sum((w.paid for w in row.works), D(0)) + row.unallocated, row.paid, as_of)
                self.assertEqual(row.remaining - row.overpaid, row.works_price - row.paid, as_of)
                self.assertEqual(row.paid, row.paid_gross - row.refunded, as_of)
                self.assertGreaterEqual(row.remaining, 0)
                self.assertGreaterEqual(row.queued, 0)
                for w in row.works:
                    self.assertEqual(w.paid, w.paid_linked + w.paid_allocated)
            day += timedelta(days=1)

    def test_overpay_row_adds_up_on_all_dates(self):
        """Столбцы «Выплачено» и «Переплата» в таблице работ (текст и CSV) складываются в итог договора."""
        c_, w_, p_ = cr.load_data(ROOT / "data")
        seen_overpay_dates = []
        day = date(2026, 8, 20)
        while day <= date(2026, 12, 5):
            rep = cr.build_report(c_, w_, p_, day)
            text = cr.render_text(rep)
            with tempfile.TemporaryDirectory() as tmp:
                wp, cp = cr.write_csv(rep, Path(tmp))
                with wp.open(encoding="utf-8-sig", newline="") as fh:
                    work_rows = list(csv.DictReader(fh, delimiter=";"))
                with cp.open(encoding="utf-8-sig", newline="") as fh:
                    contract_rows = {r["Договор"]: r for r in csv.DictReader(fh, delimiter=";")}
            num = lambda x: D(x.replace(",", "."))  # noqa: E731
            for c in rep.contracts:
                mine = [r for r in work_rows if r["Договор"] == c.contract_id]
                self.assertEqual(sum((num(r["Выплачено"]) for r in mine), D(0)),
                                 num(contract_rows[c.contract_id]["Выплачено нетто"]), (day, c.contract_id))
                self.assertEqual(sum((num(r["Переплата"]) for r in mine), D(0)),
                                 num(contract_rows[c.contract_id]["Переплата"]), (day, c.contract_id))
                has_row = any(r["Название"] == cr.OVERPAY_ROW_TITLE for r in mine)
                self.assertEqual(has_row, c.unallocated > 0, (day, c.contract_id))
                if c.unallocated > 0:
                    seen_overpay_dates.append(day)
            self.assertEqual(text.count(cr.OVERPAY_ROW_TITLE),
                             sum(1 for c in rep.contracts if c.unallocated > 0), day)
            day += timedelta(days=1)
        # на данных задания переплата появляется с 12.11 и дальше не исчезает
        self.assertEqual(seen_overpay_dates[0], date(2026, 11, 12))

    def test_overpay_row_in_text_on_1211(self):
        _, rep = report("2026-11-12")
        text = cr.render_text(rep)
        self.assertIn(cr.OVERPAY_ROW_TITLE, text)
        d2 = {r.contract_id: r for r in rep.contracts}["D-2"]
        self.assertEqual(sum((w.paid for w in d2.works), D(0)), D("90000.00"))
        self.assertEqual(d2.unallocated, D("5000.00"))
        self.assertNotIn(cr.OVERPAY_ROW_TITLE, cr.render_text(report("2026-11-10")[1]))


class Money(unittest.TestCase):
    def test_exact_decimals(self):
        self.assertEqual(cr.parse_money("0.10", "x") + cr.parse_money("0.20", "x"), D("0.30"))

    def test_rejects_sub_kopeck(self):
        with self.assertRaises(cr.DataError):
            cr.parse_money("10.001", "x")

    def test_rejects_garbage_and_non_positive(self):
        for bad in ("", "abc", "-5.00", "0.00", "NaN", "Infinity"):
            with self.assertRaises(cr.DataError, msg=bad):
                cr.parse_money(bad, "x")

    def test_formatting(self):
        self.assertEqual(cr.fmt_money(D("1234567.5")), "1 234 567,50")
        self.assertEqual(cr.csv_money(D("1234567.5")), "1234567,50")


class Dates(unittest.TestCase):
    def test_both_formats(self):
        self.assertEqual(cr.parse_date("10.11.2026"), date(2026, 11, 10))
        self.assertEqual(cr.parse_date("2026-11-10"), date(2026, 11, 10))

    def test_bad_date(self):
        with self.assertRaises(cr.DataError):
            cr.parse_date("31.02.2026")


class Synthetic(unittest.TestCase):
    """Маленькие наборы данных для крайних случаев."""

    def run_case(self, works_rows, payments_rows, amount="100.00", as_of="2026-12-01"):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "contracts.csv").write_text(
                "contract_id,contractor,amount,signed_at\nC,X," + amount + ",2026-01-01\n", encoding="utf-8")
            (t / "works.csv").write_text(
                "work_id,contract_id,title,price,status,completed_at,cancelled_at\n" + "\n".join(works_rows) + "\n",
                encoding="utf-8")
            (t / "payments.csv").write_text(
                "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n"
                + "\n".join(payments_rows) + "\n", encoding="utf-8")
            c, w, p = cr.load_data(t)
            return cr.build_report(c, w, p, cr.parse_date(as_of)).contracts[0]

    def test_linked_payment_above_price_is_overpay(self):
        row = self.run_case(["W1,C,a,50.00,done,2026-02-01,"],
                            ["P1,C,W1,x,70.00,paid,2026-02-01,2026-02-01,"])
        self.assertEqual(row.works[0].overpaid, D("20.00"))
        self.assertEqual(row.works[0].remaining, D("0.00"))
        self.assertEqual(row.overpaid, D("20.00"))

    def test_payment_on_cancelled_work_flagged(self):
        row = self.run_case(["W1,C,a,50.00,cancelled,,2026-03-01", "W2,C,b,50.00,planned,,"],
                            ["P1,C,W1,x,10.00,paid,2026-02-01,2026-02-01,"])
        w = {x.work_id: x for x in row.works}
        self.assertEqual(w["W1"].overpaid, D("10.00"))
        self.assertIn("возврат", w["W1"].note)

    def test_cents_do_not_drift(self):
        row = self.run_case(["W1,C,a,0.30,done,2026-02-01,"],
                            ["P1,C,,x,0.10,paid,2026-02-01,2026-02-01,",
                             "P2,C,,x,0.20,paid,2026-02-02,2026-02-02,"], amount="0.30")
        self.assertEqual(row.remaining, D("0.00"))
        self.assertEqual(row.paid, D("0.30"))

    def test_fifo_by_acceptance_date_not_by_id(self):
        row = self.run_case(["W1,C,a,50.00,done,2026-03-01,", "W2,C,b,50.00,done,2026-02-01,"],
                            ["P1,C,,x,50.00,paid,2026-01-05,2026-01-05,"])
        w = {x.work_id: x for x in row.works}
        self.assertEqual(w["W2"].paid, D("50.00"))        # принята раньше - закрывается первой
        self.assertEqual(w["W1"].paid, D("0.00"))

    def test_refunded_payment_before_its_paid_date_has_no_effect(self):
        # заведён 01.02, ушёл 10.02, возвращён 20.02
        data = (["W1,C,a,100.00,planned,,"], ["P1,C,,x,100.00,refunded,2026-02-01,2026-02-10,2026-02-20"])
        before = self.run_case(*data, as_of="2026-02-05")
        self.assertEqual((before.paid_gross, before.refunded, before.paid), (D("0.00"),) * 3)
        self.assertEqual((before.queued, before.queued_ids), (D("100.00"), ["P1"]))
        paid = self.run_case(*data, as_of="2026-02-15")
        self.assertEqual((paid.paid_gross, paid.refunded, paid.paid, paid.queued),
                         (D("100.00"), D("0.00"), D("100.00"), D("0.00")))
        back = self.run_case(*data, as_of="2026-02-20")
        self.assertEqual((back.paid_gross, back.refunded, back.paid, back.queued),
                         (D("100.00"), D("100.00"), D("0.00"), D("0.00")))
        not_created = self.run_case(["W1,C,a,100.00,planned,,"],
                                    ["P1,C,,x,100.00,refunded,2026-02-08,2026-02-10,2026-02-20"],
                                    as_of="2026-02-05")
        self.assertEqual((not_created.paid_gross, not_created.queued), (D("0.00"), D("0.00")))

    def test_unknown_work_raises(self):
        with self.assertRaises(cr.DataError):
            self.run_case(["W1,C,a,50.00,planned,,"], ["P1,C,W9,x,10.00,paid,2026-02-01,2026-02-01,"])

    def test_paid_without_date_raises(self):
        with self.assertRaises(cr.DataError):
            self.run_case(["W1,C,a,50.00,planned,,"], ["P1,C,,x,10.00,paid,2026-02-01,,"])


class Cli(unittest.TestCase):
    def test_cli_runs_and_writes_csv(self):
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cr.main(["--date", "10.11.2026", "--csv-dir", tmp])
            self.assertEqual(code, 0)
            self.assertIn("ОСТАЛОСЬ ОПЛАТИТЬ", buf.getvalue())
            self.assertTrue((Path(tmp) / "works_2026-11-10.csv").exists())
            self.assertTrue((Path(tmp) / "contracts_2026-11-10.csv").exists())

    def test_cli_bad_date_is_error_not_crash(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cr.main(["--date", "вчера"]), 2)


if __name__ == "__main__":
    unittest.main()
