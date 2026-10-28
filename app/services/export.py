from __future__ import annotations

from io import BytesIO
from datetime import datetime

from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from sqlalchemy import select
import arabic_reshaper
from bidi.algorithm import get_display

from app.models import Product, Brand, ProductChannelPost, SystemSetting
from app.core.config import settings
from app.utils import stock_label


def rtl(text) -> str:
    text = str(text or "")
    return get_display(arabic_reshaper.reshape(text))


async def _rows(session):
    products = (await session.execute(
        select(Product).where(Product.is_active.is_(True)).order_by(Product.base_price, Product.name)
    )).scalars().all()
    out = []
    for p in products:
        brand = await session.get(Brand, p.brand_id) if p.brand_id else None
        post = await session.scalar(
            select(ProductChannelPost)
            .where(ProductChannelPost.product_id == p.id)
            .order_by(ProductChannelPost.id.desc())
        )
        out.append({
            "sku": p.internal_sku,
            "name": p.name,
            "brand": brand.name if brand else "",
            "price": int(p.base_price or 0),
            "wholesale_price": int(p.wholesale_price or 0),
            "unit": p.unit,
            "stock": stock_label(p.stock_status),
            "url": post.post_url if post else "",
        })
    return out


async def build_price_list(session, include_wholesale: bool = True) -> BytesIO:
    rows = await _rows(session)
    wb = Workbook()
    ws = wb.active
    ws.title = "Price List"
    ws.append(["کد", "نام محصول", "برند", "قیمت خرده (تومان)", "قیمت عمده (تومان)", "واحد", "وضعیت", "لینک کانال"])
    for r in rows:
        ws.append([r["sku"], r["name"], r["brand"], r["price"], r["wholesale_price"], r["unit"], r["stock"], r["url"]])
    if not include_wholesale:
        ws.delete_cols(5)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    widths = [16, 38, 22, 18, 18, 12, 16, 45]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[chr(64+i)].width = width
    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out


async def build_price_list_pdf(session, include_wholesale: bool = True) -> BytesIO:
    rows = await _rows(session)
    out = BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(
        out,
        pagesize=page,
        rightMargin=12*mm,
        leftMargin=12*mm,
        topMargin=12*mm,
        bottomMargin=12*mm,
    )

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    font_bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    pdfmetrics.registerFont(TTFont("DejaVu", font_path))
    pdfmetrics.registerFont(TTFont("DejaVuBold", font_bold_path))

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "rtl-title", parent=styles["Title"], fontName="DejaVuBold",
        fontSize=16, leading=22, alignment=TA_CENTER,
    )
    meta_style = ParagraphStyle(
        "rtl-meta", parent=styles["Normal"], fontName="DejaVu",
        fontSize=9, leading=14, alignment=TA_RIGHT,
    )
    cell_style = ParagraphStyle(
        "rtl-cell", parent=styles["Normal"], fontName="DejaVu",
        fontSize=8, leading=11, alignment=TA_RIGHT,
    )

    contact_row = await session.get(SystemSetting, "company_contact")
    contact = contact_row.value if contact_row and contact_row.value else ""
    story = [
        Paragraph(rtl(f"لیست قیمت {settings.company_name}"), title_style),
        Paragraph(rtl(f"آخرین بروزرسانی: {datetime.now():%Y-%m-%d %H:%M}"), meta_style),
        Paragraph(rtl(f"اطلاعات تماس: {contact}") if contact else rtl(""), meta_style),
        Spacer(1, 5*mm),
    ]

    data = [[
        Paragraph(rtl("کد"), cell_style),
        Paragraph(rtl("نام محصول"), cell_style),
        Paragraph(rtl("برند"), cell_style),
        Paragraph(rtl("خرده (تومان)"), cell_style),
        Paragraph(rtl("عمده (تومان)"), cell_style),
        Paragraph(rtl("واحد"), cell_style),
        Paragraph(rtl("وضعیت"), cell_style),
    ]]
    for r in rows:
        data.append([
            Paragraph(str(r["sku"]), cell_style),
            Paragraph(rtl(r["name"]), cell_style),
            Paragraph(rtl(r["brand"]), cell_style),
            Paragraph(f"{r['price']:,}", cell_style),
            Paragraph(f"{r['wholesale_price']:,}", cell_style),
            Paragraph(rtl(r["unit"]), cell_style),
            Paragraph(rtl(r["stock"]), cell_style),
        ])

    widths = [28*mm, 62*mm, 38*mm, 34*mm, 34*mm, 22*mm, 30*mm]
    if not include_wholesale:
        for row in data:
            del row[4]
        del widths[4]
    table = Table(data, repeatRows=1, colWidths=widths)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E9ECEF")),
        ("FONTNAME", (0,0), (-1,0), "DejaVuBold"),
        ("GRID", (0,0), (-1,-1), 0.4, colors.HexColor("#B9B9B9")),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("RIGHTPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING", (0,0), (-1,-1), 5),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    story.append(table)
    doc.build(story)
    out.seek(0)
    return out
