# -*- coding: utf-8 -*-
"""الإشعارات الديناميكية — نظام واحد لكل مسارات التصنيع.

الإشعار يُنشأ عند حدث (استلام خام، إفراج، اكتمال مرحلة…) ويستهدف **صلاحية** لا شخصًا:
كل من يملك إحدى الصلاحيات المستهدفة (وخط الإنتاج مسموح له) يراه. حالة القراءة لكل مستخدم.
"""
import json

import db
from constants import PERMS


def _row(kind, title, body, link, ref, route, perms, actor):
    return (kind, title, body, link, ref, route, json.dumps(sorted(set(perms))), actor)


def push(con, kind, title, body='', link=None, ref=None, route=None, perms=('view',), actor=None):
    """يُنشئ إشعارًا داخل تعامل قائم. perms: الصلاحيات المستهدفة (يكفي واحدة)."""
    from flask import g, has_request_context
    who = actor or (g.user['username'] if has_request_context() and getattr(g, 'user', None) else 'system')
    con.execute("""INSERT INTO notifications(kind,title,body,link,ref,route_code,target_perms,created_by)
                   VALUES(?,?,?,?,?,?,?,?)""", _row(kind, title, body, link, ref, route, perms, who))


def _user_lines(u):
    return [x for x in (u.get('lines') or '').split(',') if x]


def visible(u, notif):
    """هل يرى المستخدم u هذا الإشعار؟"""
    try:
        want = set(json.loads(notif.get('target_perms') or '[]'))
    except ValueError:
        want = set()
    if want and not (want & PERMS.get(u['role'], set())):
        return False
    lines = _user_lines(u)
    return not (notif.get('route_code') and lines and notif['route_code'] not in lines)


def for_user(u, limit=60, only_unread=False):
    rows = db.q('SELECT * FROM notifications ORDER BY id DESC LIMIT 400')
    read = {r['notif_id'] for r in db.q('SELECT notif_id FROM notif_reads WHERE username=?', (u['username'],))}
    out = []
    for r in rows:
        if not visible(u, r):
            continue
        r['read'] = r['id'] in read
        if only_unread and r['read']:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def unread_count(u):
    return len(for_user(u, limit=99, only_unread=True))


def mark_read(u, ids=None):
    ids = ids or [r['id'] for r in for_user(u, limit=200, only_unread=True)]
    if ids:
        db.run_many([('INSERT OR IGNORE INTO notif_reads(notif_id,username) VALUES(?,?)', (i, u['username']))
                     for i in ids])
