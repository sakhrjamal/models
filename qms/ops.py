# -*- coding: utf-8 -*-
"""قائمة المشغلين الرئيسية: اختيار من قائمة لا كتابة حرة. يعدّلها مدير النظام فقط من «المشغلون»."""
from flask import session, g

import db


def list_ops(dept=None):
    sql, args = 'SELECT * FROM operators WHERE active=1', []
    if dept:
        sql += ' AND dept=?'
        args.append(dept)
    return db.q(sql + ' ORDER BY id', tuple(args))


def resolve(name, dept=None):
    """الاسم إن كان مشغلًا فعّالًا في القائمة وإلا None."""
    name = (name or '').strip()
    if not name:
        return None
    r = db.one('SELECT name FROM operators WHERE name=? AND active=1' + (' AND dept=?' if dept else ''),
               (name, dept) if dept else (name,))
    return r['name'] if r else None


def default(dept='production'):
    """المشغل الافتراضي في القائمة: آخر مختار في الجلسة وإلا اسم المستخدم إن كان في القائمة."""
    last = session.get(f'_op_{dept}')
    if last and resolve(last, dept):
        return last
    u = getattr(g, 'user', None)
    if u and resolve(u['full_name'], dept):
        return u['full_name']
    return None


def remember(dept, name):
    if name:
        session[f'_op_{dept}'] = name
