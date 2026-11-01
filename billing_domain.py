"""Billing rules shared by quotes and tests. Amounts are ARS decimal pesos."""
import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP


def add_months(day, count):
    month = day.month - 1 + count
    year, month = day.year + month // 12, month % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year,month)[1]))


def cycle_bounds(nutri, today):
    start = date.fromisoformat(str(nutri['subscription_start'])[:10])
    end = date.fromisoformat(str(nutri['subscription_end'])[:10])
    if nutri.get('billing_current_plan') and nutri['subscription_type']=='monthly':
        cycle_start = start + timedelta(days=max(0, (today-start).days // 30) * 30)
        return cycle_start, min(cycle_start + timedelta(days=30), end)
    n = max(0,(today.year-start.year)*12 + today.month-start.month)
    if add_months(start,n)>today:
        n=max(0,n-1)
    return add_months(start,n), min(add_months(start,n+1),end)


def pack_price(paid_amount, months, reports_per_month, quantity):
    if quantity != 10 or months not in (1,6) or reports_per_month <= 0:
        raise ValueError('Invalid package')
    paid = Decimal(str(paid_amount))
    if not paid.is_finite() or paid <= 0:
        raise ValueError('A verified paid price is required')
    return (paid * quantity / (months * reports_per_month)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def change_mode(current, target):
    if current['id']==target['id']:
        raise ValueError('Ya tenés este plan')
    return 'exhaustion' if target['max_reports_month']>current['max_reports_month'] else 'expiry'


def new_end(start, current_end, months, policy='extend_expiry'):
    if policy not in ('activation','extend_expiry'):
        raise ValueError('Unknown expiry policy')
    base = max(start,current_end) if policy=='extend_expiry' else start
    return base + timedelta(days=30) if months==1 else add_months(base,6)
