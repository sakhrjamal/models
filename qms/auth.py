# -*- coding: utf-8 -*-
"""المستخدمون والأدوار والتوقيع الإلكتروني.

الأدوار وصلاحياتها معرّفة صراحةً في constants.PERMS (v12):
  • viewer   : عرض فقط
  • operator : إدخال بيانات الإنتاج (استلام، أسليتر، طي، فرز، تغليف، دورات)
  • qc       : مراقب جودة — تسجيل سجلات الجودة اليومية والدورية
  • qa       : ضمان جودة — اعتماد الفحص والإفراج وسجلات QA + تعديل قوالب الجودة
  • manager  : مدير المصنع — إصدار أوامر الإنتاج وأرقام التشغيلات + التقارير
  • admin    : كل ما سبق + إدارة المستخدمين + النسخ الاحتياطي
"""
import functools, datetime
from flask import session, redirect, url_for, request, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
import db
from constants import ROLES, PERMS

PW_METHOD = 'pbkdf2:sha256'


def hash_pw(pw):
    return generate_password_hash(pw or '', method=PW_METHOD)


def verify_pw(stored, pw):
    try:
        return check_password_hash(stored or '', pw or '')
    except Exception:
        return False


# --------------------------------------------------------------- دورة الجلسة
def load_logged_in_user():
    uid = session.get('uid')
    g.user = db.one('SELECT * FROM users WHERE id=? AND active=1', (uid,)) if uid else None


def attempt_login(username, password):
    u = db.one('SELECT * FROM users WHERE username=? AND active=1', ((username or '').strip(),))
    if not u or not verify_pw(u['pw_hash'], password):
        return None
    db.run('UPDATE users SET last_login=? WHERE id=?',
           (datetime.datetime.now().isoformat(sep=' ', timespec='seconds'), u['id']))
    csrf = session.get('_csrf')
    session.clear()
    session['uid'] = u['id']
    if csrf:
        session['_csrf'] = csrf
    return u


def logout():
    csrf = session.get('_csrf')
    session.clear()
    if csrf:
        session['_csrf'] = csrf


# --------------------------------------------------------------- التحقق من الصلاحية
def can(perm):
    """هل يملك المستخدم الحالي الصلاحية ‎perm‎ (انظر constants.PERMS)؟"""
    u = getattr(g, 'user', None)
    return bool(u) and perm in PERMS.get(u['role'], set())


def need(perm):
    """يوقف الطلب بـ 401/403 إذا لم يملك المستخدم الصلاحية."""
    if not getattr(g, 'user', None):
        abort(401)
    if not can(perm):
        abort(403)


def user_lines(u=None):
    """خطوط الإنتاج (مسارات) المسموحة للمستخدم؛ فارغ = كل الخطوط (المدير والمسؤول دائمًا كلها)."""
    u = u or getattr(g, 'user', None)
    if not u or u['role'] in ('manager', 'admin'):
        return []
    return [x for x in (u.get('lines') or '').split(',') if x]


def line_ok(route_code):
    """هل يستطيع المستخدم العمل على خط الإنتاج route_code؟"""
    lines = user_lines()
    return not lines or not route_code or route_code in lines


def need_line(route_code):
    if not line_ok(route_code):
        abort(403)


def require(perm='view'):
    """مُزخرِف للمسارات: يطلب تسجيل الدخول والصلاحية."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapped(*a, **k):
            if not getattr(g, 'user', None):
                return redirect(url_for('login', next=request.full_path))
            if not can(perm):
                abort(403)
            return fn(*a, **k)
        return wrapped
    return deco


# --------------------------------------------------------------- التوقيع الإلكتروني
def check_esign(password):
    """يتحقق أن المستخدم الحالي أعاد إدخال كلمة مروره بشكل صحيح."""
    return bool(getattr(g, 'user', None)) and verify_pw(g.user['pw_hash'], password)


def signature_row(meaning, table, key):
    """يعيد (sql, args) لإدراج توقيع إلكتروني — يُضاف لتعامل السجل نفسه."""
    now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
    return ("""INSERT INTO signatures(username, full_name, role, meaning, table_name, record_key, signed_at)
               VALUES(?,?,?,?,?,?,?)""",
            (g.user['username'], g.user['full_name'], g.user['role'], meaning, table, str(key), now))


def signatures_for(table, key):
    return db.q('SELECT * FROM signatures WHERE table_name=? AND record_key=? ORDER BY id',
                (table, str(key)))


# --------------------------------------------------------------- التهيئة
def ensure_admin():
    """ينشئ مستخدم admin افتراضيًا إن كان جدول المستخدمين فارغًا. يعيد بيانات الدخول أو None."""
    n = db.one('SELECT COUNT(*) c FROM users')['c']
    if n:
        return None
    db.run("""INSERT INTO users(username, full_name, pw_hash, role, active, must_change_pw)
              VALUES('admin', 'مدير النظام', ?, 'admin', 1, 1)""", (hash_pw('admin'),))
    db.log('create', 'users', 'admin', 'حساب المدير الافتراضي', actor='system')
    return ('admin', 'admin')
