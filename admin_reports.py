# admin_reports.py

import html
from datetime import date, datetime, timedelta, time
from decimal import Decimal
from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, case, desc
from sqlalchemy.orm import joinedload

from models import Order, OrderStatus, CashTransaction, Employee, OrderItem, Role, Settings
from templates import (
    ADMIN_HTML_TEMPLATE, ADMIN_REPORT_CASH_FLOW_BODY, 
    ADMIN_REPORT_WORKERS_BODY, ADMIN_REPORT_ANALYTICS_BODY
)
from dependencies import get_db_session, check_credentials

router = APIRouter()

async def get_date_range(date_from_str: str | None, date_to_str: str | None):
    today = date.today()
    d_to = datetime.strptime(date_to_str, "%Y-%m-%d").date() if date_to_str else today
    d_from = datetime.strptime(date_from_str, "%Y-%m-%d").date() if date_from_str else today - timedelta(days=0) # По умолчанию сегодня
    
    # Начало дня (00:00:00) и Конец дня (23:59:59)
    dt_from = datetime.combine(d_from, time.min)
    dt_to = datetime.combine(d_to, time.max)
    
    return d_from, d_to, dt_from, dt_to

@router.get("/admin/reports/cash_flow", response_class=HTMLResponse)
async def report_cash_flow(
    date_from: str = Query(None),
    date_to: str = Query(None),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    settings = await session.get(Settings, 1) or Settings()
    d_from, d_to, dt_from, dt_to = await get_date_range(date_from, date_to)

    # Получаем ID завершенных статусов
    completed_statuses = await session.execute(select(OrderStatus.id).where(OrderStatus.is_completed_status == True))
    completed_ids = completed_statuses.scalars().all()

    # 1. Анализ Продаж (Orders)
    sales_query = select(
        Order.payment_method,
        func.sum(Order.total_price)
    ).where(
        Order.created_at >= dt_from,
        Order.created_at <= dt_to,
        Order.status_id.in_(completed_ids)
    ).group_by(Order.payment_method)

    sales_res = await session.execute(sales_query)
    sales_data = sales_res.all()

    cash_revenue = Decimal('0.00')
    card_revenue = Decimal('0.00')

    for method, amount in sales_data:
        if method == 'cash': cash_revenue += amount
        elif method == 'card': card_revenue += amount

    # 2. Анализ Транзакций (CashTransaction) - Внесения и Изъятия
    trans_query = select(CashTransaction).options(joinedload(CashTransaction.shift).joinedload('employee')).where(
        CashTransaction.created_at >= dt_from,
        CashTransaction.created_at <= dt_to
    ).order_by(CashTransaction.created_at.desc())

    trans_res = await session.execute(trans_query)
    transactions = trans_res.scalars().all()

    total_expenses = Decimal('0.00')
    transaction_rows = ""

    for tx in transactions:
        tx_type_display = ""
        color = "black"
        if tx.transaction_type == 'in':
            tx_type_display = "📥 Внесение"
            color = "green"
        elif tx.transaction_type == 'out':
            tx_type_display = "📤 Расход/Изъятие"
            color = "red"
            total_expenses += tx.amount
        elif tx.transaction_type == 'handover':
            tx_type_display = "💸 Сдача выручки"
            color = "blue"

        emp_name = tx.shift.employee.full_name if tx.shift and tx.shift.employee else "Система"
        
        transaction_rows += f"""
        <tr>
            <td>{tx.created_at.strftime('%d.%m %H:%M')}</td>
            <td style="color:{color}">{tx_type_display}</td>
            <td>{tx.amount:.2f}</td>
            <td>{html.escape(emp_name)}</td>
            <td>{html.escape(tx.comment or '')}</td>
        </tr>
        """

    body = ADMIN_REPORT_CASH_FLOW_BODY.format(
        date_from=d_from,
        date_to=d_to,
        total_revenue=(cash_revenue + card_revenue).quantize(Decimal("0.01")),
        cash_revenue=cash_revenue.quantize(Decimal("0.01")),
        card_revenue=card_revenue.quantize(Decimal("0.01")),
        total_expenses=total_expenses.quantize(Decimal("0.01")),
        transaction_rows=transaction_rows or "<tr><td colspan='5'>Транзакций за период не найдено</td></tr>"
    )

    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Отчет: Движение средств",
        body=body,
        site_title=settings.site_title,
        reports_active="active",
        **{k: "" for k in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "settings_active", "design_active"]}
    ))


@router.get("/admin/reports/workers", response_class=HTMLResponse)
async def report_workers(
    date_from: str = Query(None),
    date_to: str = Query(None),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    settings = await session.get(Settings, 1) or Settings()
    d_from, d_to, dt_from, dt_to = await get_date_range(date_from, date_to)
    
    completed_statuses = await session.execute(select(OrderStatus.id).where(OrderStatus.is_completed_status == True))
    completed_ids = completed_statuses.scalars().all()

    # Запрос для курьеров и официантов (объединяем логику)
    # Мы считаем заказы, которые были выполнены определенным сотрудником
    
    # 1. Курьеры (completed_by_courier_id)
    courier_stats = await session.execute(
        select(
            Employee.full_name,
            Role.name.label("role_name"),
            func.count(Order.id).label("count"),
            func.sum(Order.total_price).label("total")
        )
        .join(Employee, Order.completed_by_courier_id == Employee.id)
        .join(Role, Employee.role_id == Role.id)
        .where(
            Order.created_at >= dt_from,
            Order.created_at <= dt_to,
            Order.status_id.in_(completed_ids)
        )
        .group_by(Employee.id, Employee.full_name, Role.name)
    )
    
    # 2. Официанты (accepted_by_waiter_id) - только для in_house заказов
    waiter_stats = await session.execute(
        select(
            Employee.full_name,
            Role.name.label("role_name"),
            func.count(Order.id).label("count"),
            func.sum(Order.total_price).label("total")
        )
        .join(Employee, Order.accepted_by_waiter_id == Employee.id)
        .join(Role, Employee.role_id == Role.id)
        .where(
            Order.created_at >= dt_from,
            Order.created_at <= dt_to,
            Order.status_id.in_(completed_ids),
            Order.order_type == 'in_house'
        )
        .group_by(Employee.id, Employee.full_name, Role.name)
    )

    all_stats = list(courier_stats.all()) + list(waiter_stats.all())
    
    # Сортируем по сумме продаж
    all_stats.sort(key=lambda x: x.total or 0, reverse=True)

    rows = ""
    for row in all_stats:
        total = row.total or Decimal(0)
        count = row.count or 0
        avg_check = (total / count) if count > 0 else 0
        
        rows += f"""
        <tr>
            <td>{html.escape(row.full_name)}</td>
            <td>{html.escape(row.role_name)}</td>
            <td>{count}</td>
            <td>{total:.2f} грн</td>
            <td>{avg_check:.2f} грн</td>
        </tr>
        """

    body = ADMIN_REPORT_WORKERS_BODY.format(
        date_from=d_from,
        date_to=d_to,
        rows=rows or "<tr><td colspan='5'>Нет данных за выбранный период</td></tr>"
    )

    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Отчет: Персонал",
        body=body,
        site_title=settings.site_title,
        reports_active="active",
        **{k: "" for k in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "settings_active", "design_active"]}
    ))


@router.get("/admin/reports/analytics", response_class=HTMLResponse)
async def report_analytics(
    date_from: str = Query(None),
    date_to: str = Query(None),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    settings = await session.get(Settings, 1) or Settings()
    d_from, d_to, dt_from, dt_to = await get_date_range(date_from, date_to)
    
    completed_statuses = await session.execute(select(OrderStatus.id).where(OrderStatus.is_completed_status == True))
    completed_ids = completed_statuses.scalars().all()

    # Агрегация по товарам (OrderItems)
    # Учитываем цену на момент заказа (price_at_moment)
    query = select(
        OrderItem.product_name,
        func.sum(OrderItem.quantity).label("total_qty"),
        func.sum(OrderItem.quantity * OrderItem.price_at_moment).label("total_revenue")
    ).join(Order, OrderItem.order_id == Order.id).where(
        Order.created_at >= dt_from,
        Order.created_at <= dt_to,
        Order.status_id.in_(completed_ids)
    ).group_by(OrderItem.product_name).order_by(desc("total_revenue"))

    res = await session.execute(query)
    data = res.all()

    total_period_revenue = sum(row.total_revenue for row in data) if data else Decimal(1)
    if total_period_revenue == 0: total_period_revenue = Decimal(1)

    rows = ""
    for idx, row in enumerate(data, 1):
        revenue = row.total_revenue
        share = (revenue / total_period_revenue) * 100
        
        rows += f"""
        <tr>
            <td>{idx}</td>
            <td>{html.escape(row.product_name)}</td>
            <td>{row.total_qty}</td>
            <td>{revenue:.2f} грн</td>
            <td>
                <div style="display:flex; align-items:center; gap:10px;">
                    <div style="background:#e0e0e0; width:100px; height:10px; border-radius:5px; overflow:hidden;">
                        <div style="background:#4caf50; width:{share}%; height:100%;"></div>
                    </div>
                    <small>{share:.1f}%</small>
                </div>
            </td>
        </tr>
        """

    body = ADMIN_REPORT_ANALYTICS_BODY.format(
        date_from=d_from,
        date_to=d_to,
        rows=rows or "<tr><td colspan='5'>Нет продаж за выбранный период</td></tr>"
    )

    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Отчет: Аналитика",
        body=body,
        site_title=settings.site_title,
        reports_active="active",
        **{k: "" for k in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "settings_active", "design_active"]}
    ))