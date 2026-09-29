# -*- coding: utf-8 -*-
"""المهام المنتظرة للجودة: كل ما ينتظر قرارًا (5 إفراجات) + سجلات المتابعة المستحقة. المتابعة لا توقف سير العمل."""
import db, quality
from flask import render_template, url_for


def pending():
    t = []
    for w in db.q("SELECT batch_no, item_code FROM work_orders WHERE final_status='PENDING_QC' ORDER BY batch_no"):
        t.append(dict(kind='إفراج نهائي (غير معقم / معقم)', ref=w['batch_no'], sub=w['item_code'], url=url_for('quality_review', bn=w['batch_no'])))
    for r in db.q("SELECT DISTINCT batch_no FROM intermediates WHERE status='Pending SP Release'"):
        t.append(dict(kind='إفراج SP', ref=r['batch_no'], sub='كراتين SP بانتظار الإفراج', url=url_for('quality_sp_review', bn=r['batch_no'])))
    for r in db.q("SELECT rec_no, batch_no FROM ster_records WHERE status='Ready for Pre-Sterilization QC'"):
        t.append(dict(kind='إفراج قبل التعقيم', ref=r['rec_no'], sub=r['batch_no'], url=url_for('quality_pre_review', rec_no=r['rec_no'])))
    return t


def register(app):
    @app.route('/quality/tasks', endpoint='quality_tasks')
    def quality_tasks():
        due = [d for d in quality.due_items() if d['due']]
        return render_template('quality_tasks.html', nav='quality', tasks=pending(), due=due)
