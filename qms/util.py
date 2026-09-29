# -*- coding: utf-8 -*-
"""أدوات مشتركة صغيرة بين app.py وقسمي الجودة والمدير."""
import datetime, re


def num(v, d=None):
    try:
        if v in (None, ''):
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def s(v):
    v = (v or '').strip()
    return v or None


def num_unit(v, d=None):
    """استخراج أول قيمة رقمية حتى لو كانت الخلية مثل 90 Cm أو 250M."""
    if v in (None, ''):
        return d
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r'-?\d+(?:[.,]\d+)?', str(v))
    if not m:
        return d
    try:
        return float(m.group(0).replace(',', '.'))
    except ValueError:
        return d


def as_date(date_text):
    try:
        return datetime.date.fromisoformat(date_text) if date_text else datetime.date.today()
    except (TypeError, ValueError):
        return datetime.date.today()


def doc_scope(prefix, date_text=None):
    """نطاق العدّاد + قالب الرقم لسند يومي: PREFIX-YYMMDD-001."""
    d = as_date(date_text)
    stem = f"{prefix}-{d.strftime('%y%m%d')}"
    return stem, stem + '-{n3}'


MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']


def batch_stem(date_text, letter):
    """جذع رقم التشغيلة من التاريخ وحرف الماكينة/المسار: SEP-2610-F-"""
    d = as_date(date_text)
    return d, f"{MONTHS[d.month - 1]}-{d.strftime('%y')}{d.day:02d}-{letter}-"


def fmt_qty(v, digits=2):
    """كمية للعرض: فواصل آلاف وبلا أصفار زائدة (25,000 — 24,850 — 1,242.5)."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return '—'
    s_ = f'{v:,.{digits}f}'.rstrip('0').rstrip('.') if '.' in f'{v:,.{digits}f}' else f'{v:,.{digits}f}'
    return s_ or '0'


def lot_taken(lot):
    """رقم LOT المورّد مستخدم سابقًا في استلام آخر؟ (مطابقة بلا فرق مسافات/حالة أحرف). يعيد رقم الاستلام أو None."""
    import db
    key = ''.join(str(lot or '').split()).upper()
    if not key:
        return None
    for r in db.q("SELECT grn_no, supplier_lot FROM receipts"):
        if ''.join(str(r['supplier_lot'] or '').split()).upper() == key:
            return r['grn_no']
    return None
