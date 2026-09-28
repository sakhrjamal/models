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
