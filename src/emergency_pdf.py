"""
emergency_pdf.py — Generate the 'If Anything Happens' PDF for FamilyBrain.

Pulls data from:
  - memories table (metadata.emergency_category)
  - death_binder_entries table (structured free-text entries per category)
  - recurring_bills table (bills, debts, subscriptions)
  - financial_accounts table (bank accounts, investments)
  - medications + medical_appointments tables (cat 10)
  - vehicles table (cat 6)
  - professional_contacts table (cat 7)
"""

import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    HRFlowable,
)

from . import brain

logger = logging.getLogger("open_brain.emergency_pdf")

# ---------------------------------------------------------------------------
# Category definitions — 10 death-binder categories
# ---------------------------------------------------------------------------
CATEGORIES = {
    "1":  ("Legal & Personal Documents",
           "will, LPA, birth/marriage/divorce certs, passport, NI number, NHS number"),
    "2":  ("Financial Accounts & Access",
           "bank accounts, sort codes, account numbers, password manager location"),
    "3":  ("Insurance Policies",
           "life, home, contents, car, pet, health — policy numbers, providers, renewal dates"),
    "4":  ("Pensions & Investments",
           "state pension, workplace/private pensions, ISAs, shares, investment accounts"),
    "5":  ("Bills, Debts & Regular Payments",
           "mortgage/rent, utilities, council tax, subscriptions, loans, direct debits"),
    "6":  ("Assets & Possessions",
           "property deeds, car V5/MOT, valuables inventory, safe deposit box"),
    "7":  ("Contacts & Professionals",
           "solicitor, accountant, financial adviser, executor, guardians for children"),
    "8":  ("Funeral & Final Wishes",
           "burial/cremation preference, funeral plan, organ donation, letters to loved ones"),
    "9":  ("Digital Legacy",
           "social media instructions, email access, crypto wallets, subscription cancellations"),
    "10": ("Emergency Contacts & Family Details",
           "family members, NHS numbers, allergies, medications, blood types, GP, school names"),
}

# ---------------------------------------------------------------------------
# Sensitive data masking
# ---------------------------------------------------------------------------
def _mask_sensitive_data(text: str) -> str:
    """Mask account numbers and passwords in displayed text."""
    if not text:
        return text
    def mask_digits(match):
        digits = match.group(0)
        if len(digits) >= 8:
            return "*" * (len(digits) - 4) + digits[-4:]
        return digits
    masked = re.sub(r'\b\d{8,}\b', mask_digits, text)
    masked = re.sub(r'(?i)(password|pwd|passcode)[\s:=]+([^\s,;]+)', r'\1: ****', masked)
    return masked


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _get_family_members(family_id: str) -> List[str]:
    """Fetch family member names from whatsapp_members."""
    db = brain._supabase
    if not db:
        return []
    try:
        result = db.table("whatsapp_members").select("name").eq("family_id", family_id).execute()
        return [row.get("name", "Family Member") for row in (result.data or [])]
    except Exception as exc:
        logger.warning("Failed to fetch family members: %s", exc)
        return []


def _get_upcoming_events(family_id: str) -> List[Dict[str, Any]]:
    """Fetch upcoming events for the next 30 days."""
    db = brain._supabase
    if not db:
        return []
    try:
        now = datetime.now(timezone.utc)
        thirty_days = now + timedelta(days=30)
        result = db.table("family_events") \
            .select("*") \
            .eq("family_id", family_id) \
            .gte("event_date", now.strftime("%Y-%m-%d")) \
            .lte("event_date", thirty_days.strftime("%Y-%m-%d")) \
            .order("event_date") \
            .execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Failed to fetch upcoming events: %s", exc)
        return []


def _get_emergency_items(family_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch all memories with emergency_category set, grouped by category."""
    db = brain._supabase
    if not db:
        return {str(i): [] for i in range(1, 11)}

    items_by_category: Dict[str, List[Dict[str, Any]]] = {str(i): [] for i in range(1, 11)}

    try:
        result = db.table("memories") \
            .select("id, content, metadata, created_at") \
            .contains("metadata", {"family_id": family_id}) \
            .order("created_at", desc=True) \
            .limit(1000) \
            .execute()

        for row in (result.data or []):
            metadata = row.get("metadata", {})
            cat = metadata.get("emergency_category")
            if not cat:
                continue
            cat_num = _resolve_category_num(str(cat))
            if cat_num and cat_num in items_by_category:
                items_by_category[cat_num].append(row)
    except Exception as exc:
        logger.error("Failed to fetch emergency items from memories: %s", exc)

    return items_by_category


def _resolve_category_num(cat: str) -> Optional[str]:
    """Resolve a category string (number or name) to a 1-10 string key."""
    if str(cat).isdigit() and 1 <= int(cat) <= 10:
        return str(cat)
    cat_lower = cat.lower()
    if "legal" in cat_lower:                                          return "1"
    if "finance" in cat_lower or "bank" in cat_lower:                 return "2"
    if "insurance" in cat_lower:                                      return "3"
    if "pension" in cat_lower or "invest" in cat_lower:               return "4"
    if "bill" in cat_lower or "debt" in cat_lower:                    return "5"
    if "asset" in cat_lower or "car" in cat_lower:                    return "6"
    if "contact" in cat_lower and "emergency" not in cat_lower:       return "7"
    if "funeral" in cat_lower or "wish" in cat_lower:                 return "8"
    if "digital" in cat_lower or "legacy" in cat_lower:               return "9"
    if "family" in cat_lower or "medical" in cat_lower:               return "10"
    return None


def _get_death_binder_entries(family_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch structured death_binder_entries grouped by category."""
    db = brain._supabase
    if not db:
        return {str(i): [] for i in range(1, 11)}

    entries_by_category: Dict[str, List[Dict[str, Any]]] = {str(i): [] for i in range(1, 11)}

    try:
        result = db.table("death_binder_entries") \
            .select("*") \
            .eq("family_id", family_id) \
            .order("created_at", desc=False) \
            .execute()

        for row in (result.data or []):
            cat_num = _resolve_category_num(str(row.get("category", "")))
            if cat_num and cat_num in entries_by_category:
                entries_by_category[cat_num].append(row)
    except Exception as exc:
        logger.warning("Failed to fetch death_binder_entries: %s", exc)

    return entries_by_category


def _get_recurring_bills(family_id: str) -> List[Dict[str, Any]]:
    """Fetch active recurring bills/debts for category 5."""
    db = brain._supabase
    if not db:
        return []
    try:
        result = db.table("recurring_bills") \
            .select("*") \
            .eq("family_id", family_id) \
            .eq("active", True) \
            .order("category") \
            .execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Failed to fetch recurring_bills: %s", exc)
        return []


def _get_financial_accounts(family_id: str) -> List[Dict[str, Any]]:
    """Fetch financial accounts for category 2."""
    db = brain._supabase
    if not db:
        return []
    try:
        result = db.table("financial_accounts") \
            .select("*") \
            .eq("family_id", family_id) \
            .eq("active", True) \
            .order("account_type") \
            .execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Failed to fetch financial_accounts: %s", exc)
        return []


def _get_vehicles(family_id: str) -> List[Dict[str, Any]]:
    """Fetch vehicles for category 6."""
    db = brain._supabase
    if not db:
        return []
    try:
        result = db.table("vehicles") \
            .select("*") \
            .eq("family_id", family_id) \
            .execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Failed to fetch vehicles: %s", exc)
        return []


def _get_medications(family_id: str) -> List[Dict[str, Any]]:
    """Fetch active medications for category 10."""
    db = brain._supabase
    if not db:
        return []
    try:
        result = db.table("medications") \
            .select("*") \
            .eq("family_id", family_id) \
            .eq("active", True) \
            .execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Failed to fetch medications: %s", exc)
        return []


def _get_professional_contacts(family_id: str) -> List[Dict[str, Any]]:
    """Fetch professional contacts for category 7."""
    db = brain._supabase
    if not db:
        return []
    try:
        result = db.table("professional_contacts") \
            .select("*") \
            .eq("family_id", family_id) \
            .execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Failed to fetch professional_contacts: %s", exc)
        return []


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def _create_footer(canvas, doc):
    """Add footer to every page."""
    canvas.saveState()
    canvas.setFont('Helvetica', 9)
    canvas.setFillColor(colors.grey)
    footer_text = (
        f"FamilyBrain — If Anything Happens File  |  CONFIDENTIAL  |  "
        f"Generated {datetime.now().strftime('%d %B %Y')}  |  Page {doc.page}"
    )
    canvas.drawCentredString(A4[0] / 2.0, 0.45 * inch, footer_text)
    canvas.restoreState()


def _section_header(text: str, num: int, style: ParagraphStyle) -> Paragraph:
    return Paragraph(f"{num}. {text}", style)


def _empty_section(style: ParagraphStyle) -> Paragraph:
    return Paragraph("<i>No items stored in this category yet.</i>", style)


def _format_date(date_str: str, fmt: str = "%d %b %Y") -> str:
    if not date_str:
        return "—"
    for pattern in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                    "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str[:19], pattern[:len(date_str[:19])]).strftime(fmt)
        except Exception:
            pass
    return date_str[:10] if date_str else "—"


def _kv_table(rows: List[tuple], col_widths=None) -> Table:
    """Build a simple two-column key-value table."""
    if col_widths is None:
        col_widths = [2.2 * inch, 4.3 * inch]
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('FONTNAME',  (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME',  (1, 0), (1, -1), 'Helvetica'),
        ('FONTSIZE',  (0, 0), (-1, -1), 9),
        ('VALIGN',    (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 0), (-1, -1), [colors.whitesmoke, colors.white]),
        ('GRID',      (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ('TOPPADDING',  (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
    ]))
    return t


# ---------------------------------------------------------------------------
# Main PDF generator
# ---------------------------------------------------------------------------

def generate_emergency_pdf(family_id: str) -> bytes:
    """Generate the 'If Anything Happens' PDF for a family."""
    logger.info("Generating emergency PDF for family_id=%s", family_id)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=60,
        leftMargin=60,
        topMargin=60,
        bottomMargin=60,
    )

    styles = getSampleStyleSheet()

    # --- Custom styles ---
    title_style = ParagraphStyle(
        'TitleStyle', parent=styles['Heading1'],
        fontSize=26, spaceAfter=12, alignment=1, textColor=colors.HexColor('#1a2e44'),
    )
    subtitle_style = ParagraphStyle(
        'SubtitleStyle', parent=styles['Normal'],
        fontSize=13, spaceAfter=8, alignment=1, textColor=colors.HexColor('#4a5568'),
    )
    watermark_style = ParagraphStyle(
        'WatermarkStyle', parent=styles['Normal'],
        fontSize=14, spaceAfter=30, alignment=1,
        textColor=colors.HexColor('#c53030'), fontName='Helvetica-Bold',
    )
    section_header_style = ParagraphStyle(
        'SectionHeader', parent=styles['Heading2'],
        fontSize=14, spaceBefore=16, spaceAfter=8,
        textColor=colors.white,
        backColor=colors.HexColor('#1a2e44'),
        borderPadding=(6, 6, 6, 8),
    )
    subsection_style = ParagraphStyle(
        'SubSection', parent=styles['Heading3'],
        fontSize=11, spaceBefore=10, spaceAfter=4,
        textColor=colors.HexColor('#2b6cb0'),
    )
    item_title_style = ParagraphStyle(
        'ItemTitle', parent=styles['Heading3'],
        fontSize=11, spaceBefore=8, spaceAfter=3,
        textColor=colors.HexColor('#1a2e44'),
    )
    item_body_style = ParagraphStyle(
        'ItemBody', parent=styles['Normal'],
        fontSize=9.5, spaceAfter=6, leading=14,
    )
    item_meta_style = ParagraphStyle(
        'ItemMeta', parent=styles['Normal'],
        fontSize=8, textColor=colors.grey, spaceAfter=10,
    )
    toc_style = ParagraphStyle(
        'TOC', parent=styles['Normal'],
        fontSize=11, spaceAfter=4,
    )
    note_style = ParagraphStyle(
        'Note', parent=styles['Normal'],
        fontSize=9, textColor=colors.HexColor('#744210'),
        backColor=colors.HexColor('#fefcbf'),
        borderPadding=(4, 4, 4, 6), spaceAfter=10,
    )

    elements = []

    # -----------------------------------------------------------------------
    # Cover Page
    # -----------------------------------------------------------------------
    elements.append(Spacer(1, 1.8 * inch))
    elements.append(Paragraph("If Anything Happens…", title_style))
    elements.append(Paragraph("Family Emergency File", subtitle_style))
    elements.append(Spacer(1, 0.2 * inch))
    elements.append(Paragraph("⚠  CONFIDENTIAL — Keep in a safe place", watermark_style))

    members = _get_family_members(family_id)
    family_name_str = " & ".join(members) if members else "Family"
    elements.append(Paragraph(f"For: <b>{family_name_str}</b>", subtitle_style))
    elements.append(Paragraph(
        f"Generated: {datetime.now().strftime('%d %B %Y at %H:%M')}",
        subtitle_style,
    ))
    elements.append(Spacer(1, 0.4 * inch))
    elements.append(Paragraph(
        "This document contains everything your loved ones need to manage your affairs. "
        "Store it securely and tell a trusted person where to find it. "
        "Update it whenever circumstances change.",
        item_body_style,
    ))
    elements.append(PageBreak())

    # -----------------------------------------------------------------------
    # Table of Contents
    # -----------------------------------------------------------------------
    elements.append(Paragraph("Table of Contents", styles['Heading1']))
    elements.append(Spacer(1, 0.15 * inch))
    for i in range(1, 11):
        cat_name, cat_desc = CATEGORIES[str(i)]
        elements.append(Paragraph(f"<b>{i}.</b>  {cat_name}", toc_style))
        elements.append(Paragraph(f"    <i>{cat_desc}</i>", item_meta_style))
    elements.append(Paragraph("<b>11.</b>  Upcoming Events (Next 30 Days)", toc_style))
    elements.append(PageBreak())

    # -----------------------------------------------------------------------
    # Fetch all data
    # -----------------------------------------------------------------------
    items_by_category   = _get_emergency_items(family_id)
    binder_by_category  = _get_death_binder_entries(family_id)
    recurring_bills     = _get_recurring_bills(family_id)
    financial_accounts  = _get_financial_accounts(family_id)
    vehicles            = _get_vehicles(family_id)
    medications         = _get_medications(family_id)
    professional_contacts = _get_professional_contacts(family_id)
    upcoming_events     = _get_upcoming_events(family_id)

    # -----------------------------------------------------------------------
    # Helper: render memory items (generic)
    # -----------------------------------------------------------------------
    def _render_memory_items(cat_num: str) -> bool:
        """Render items from the memories table for a category. Returns True if any rendered."""
        items = items_by_category.get(cat_num, [])
        if not items:
            return False
        elements.append(Paragraph("From your stored documents & notes:", subsection_style))
        for item in items:
            metadata = item.get("metadata", {})
            content  = item.get("content", "")
            created_at = item.get("created_at", "")
            doc_type   = metadata.get("document_type", "Document").replace("_", " ").title()
            source_user = metadata.get("source_user", "Family Member").title()
            date_display = _format_date(created_at)
            title = f"{doc_type} — added by {source_user}"
            safe_content = _mask_sensitive_data(content)
            elements.append(Paragraph(title, item_title_style))
            for para in safe_content.split('\n'):
                if para.strip():
                    elements.append(Paragraph(para.strip(), item_body_style))
            # Show key_fields if present
            kf = metadata.get("key_fields", {})
            if kf:
                kv_rows = [
                    (k.replace("_", " ").title(), _mask_sensitive_data(str(v)))
                    for k, v in kf.items() if v
                ]
                if kv_rows:
                    elements.append(_kv_table(kv_rows))
            elements.append(Paragraph(f"Stored: {date_display}", item_meta_style))
        return True

    def _render_binder_entries(cat_num: str) -> bool:
        """Render structured death_binder_entries for a category. Returns True if any rendered."""
        entries = binder_by_category.get(cat_num, [])
        if not entries:
            return False
        elements.append(Paragraph("From your binder entries:", subsection_style))
        for entry in entries:
            label = entry.get("label") or entry.get("subcategory") or "Entry"
            value = entry.get("value", "")
            notes = entry.get("notes", "")
            date_display = _format_date(entry.get("created_at", ""))
            elements.append(Paragraph(label.title(), item_title_style))
            if value:
                safe_val = _mask_sensitive_data(value)
                for line in safe_val.split('\n'):
                    if line.strip():
                        elements.append(Paragraph(line.strip(), item_body_style))
            if notes:
                elements.append(Paragraph(f"<i>Note: {_mask_sensitive_data(notes)}</i>", item_meta_style))
            elements.append(Paragraph(f"Stored: {date_display}", item_meta_style))
        return True

    # -----------------------------------------------------------------------
    # Section 1 — Legal & Personal Documents
    # -----------------------------------------------------------------------
    elements.append(_section_header("Legal & Personal Documents", 1, section_header_style))
    elements.append(Paragraph(
        f"<i>Includes: {CATEGORIES['1'][1]}</i>", item_meta_style,
    ))
    elements.append(Spacer(1, 0.08 * inch))
    has_content = _render_binder_entries("1") | _render_memory_items("1")
    if not has_content:
        elements.append(_empty_section(item_body_style))
        elements.append(Paragraph(
            "💡 Tip: Send FamilyBrain a message like:\n"
            "  legal: Will stored at Smiths Solicitors, ref W-2024-001\n"
            "  legal: LPA registered, copy with Mum\n"
            "  legal: NI number AB123456C",
            note_style,
        ))
    elements.append(Spacer(1, 0.2 * inch))

    # -----------------------------------------------------------------------
    # Section 2 — Financial Accounts & Access
    # -----------------------------------------------------------------------
    elements.append(PageBreak())
    elements.append(_section_header("Financial Accounts & Access", 2, section_header_style))
    elements.append(Paragraph(f"<i>Includes: {CATEGORIES['2'][1]}</i>", item_meta_style))
    elements.append(Spacer(1, 0.08 * inch))

    # Structured financial_accounts table
    if financial_accounts:
        elements.append(Paragraph("Bank & Investment Accounts:", subsection_style))
        header = [["Account", "Type", "Institution", "Sort Code", "Account No.", "Owner"]]
        rows = header
        for acc in financial_accounts:
            rows.append([
                acc.get("name", ""),
                acc.get("account_type", "").replace("_", " ").title(),
                acc.get("institution", ""),
                acc.get("sort_code", "") or "—",
                _mask_sensitive_data(acc.get("account_number", "") or "—"),
                acc.get("owner", "family").title(),
            ])
        t = Table(rows, colWidths=[1.2*inch, 0.9*inch, 1.3*inch, 0.9*inch, 1.1*inch, 0.9*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a2e44')),
            ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
            ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0, 0), (-1, -1), 8),
            ('ALIGN',      (0, 0), (-1, -1), 'LEFT'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ('GRID',       (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ('TOPPADDING',    (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 0.1 * inch))

    has_content = bool(financial_accounts) | _render_binder_entries("2") | _render_memory_items("2")
    if not has_content:
        elements.append(_empty_section(item_body_style))
        elements.append(Paragraph(
            "💡 Tip: Send FamilyBrain a message like:\n"
            "  bank: Barclays current account, sort code 20-00-00, acc 12345678\n"
            "  bank: Password manager is 1Password, vault shared with spouse",
            note_style,
        ))
    elements.append(Spacer(1, 0.2 * inch))

    # -----------------------------------------------------------------------
    # Section 3 — Insurance Policies
    # -----------------------------------------------------------------------
    elements.append(PageBreak())
    elements.append(_section_header("Insurance Policies", 3, section_header_style))
    elements.append(Paragraph(f"<i>Includes: {CATEGORIES['3'][1]}</i>", item_meta_style))
    elements.append(Spacer(1, 0.08 * inch))

    # Insurance bills from recurring_bills
    insurance_bills = [b for b in recurring_bills if b.get("category") == "insurance"]
    if insurance_bills:
        elements.append(Paragraph("Insurance Policies (from bills tracker):", subsection_style))
        header = [["Policy / Name", "Provider", "Ref/Policy No.", "Amount", "Renewal"]]
        rows = header
        for bill in insurance_bills:
            renewal = _format_date(str(bill.get("renewal_date", "") or ""), "%d %b %Y")
            rows.append([
                bill.get("name", ""),
                bill.get("provider", "") or "—",
                bill.get("account_ref", "") or "—",
                f"£{bill.get('amount_gbp', '')}/{bill.get('frequency', 'yr')}" if bill.get("amount_gbp") else "—",
                renewal,
            ])
        t = Table(rows, colWidths=[1.6*inch, 1.4*inch, 1.3*inch, 0.9*inch, 1.1*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a2e44')),
            ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
            ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0, 0), (-1, -1), 8),
            ('ALIGN',      (0, 0), (-1, -1), 'LEFT'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ('GRID',       (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ('TOPPADDING',    (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 0.1 * inch))

    has_content = bool(insurance_bills) | _render_binder_entries("3") | _render_memory_items("3")
    if not has_content:
        elements.append(_empty_section(item_body_style))
        elements.append(Paragraph(
            "💡 Tip: Send FamilyBrain a message like:\n"
            "  insurance: Life insurance, Legal & General, policy LG-2024-001, £500k cover, renews Jan 2025\n"
            "  insurance: Home & contents, Aviva, policy AV-12345, renews March 2025",
            note_style,
        ))
    elements.append(Spacer(1, 0.2 * inch))

    # -----------------------------------------------------------------------
    # Section 4 — Pensions & Investments
    # -----------------------------------------------------------------------
    elements.append(PageBreak())
    elements.append(_section_header("Pensions & Investments", 4, section_header_style))
    elements.append(Paragraph(f"<i>Includes: {CATEGORIES['4'][1]}</i>", item_meta_style))
    elements.append(Spacer(1, 0.08 * inch))

    # Pension/investment bills from recurring_bills
    pension_bills = [b for b in recurring_bills if b.get("category") in ("pension", "investment")]
    if pension_bills:
        elements.append(Paragraph("Pension & Investment Contributions:", subsection_style))
        for bill in pension_bills:
            kv = [
                ("Name", bill.get("name", "")),
                ("Provider", bill.get("provider", "") or "—"),
                ("Reference", bill.get("account_ref", "") or "—"),
                ("Amount", f"£{bill.get('amount_gbp', '')} {bill.get('frequency', '')}" if bill.get("amount_gbp") else "—"),
                ("Notes", bill.get("notes", "") or "—"),
            ]
            elements.append(_kv_table(kv))
            elements.append(Spacer(1, 0.05 * inch))

    has_content = bool(pension_bills) | _render_binder_entries("4") | _render_memory_items("4")
    if not has_content:
        elements.append(_empty_section(item_body_style))
        elements.append(Paragraph(
            "💡 Tip: Send FamilyBrain a message like:\n"
            "  pension: State pension, NI contributions up to date, forecast £9,800/yr\n"
            "  pension: Workplace pension with Nest, employer ref NEST-12345\n"
            "  pension: Stocks & Shares ISA with Vanguard, acc ref VS-001",
            note_style,
        ))
    elements.append(Spacer(1, 0.2 * inch))

    # -----------------------------------------------------------------------
    # Section 5 — Bills, Debts & Regular Payments
    # -----------------------------------------------------------------------
    elements.append(PageBreak())
    elements.append(_section_header("Bills, Debts & Regular Payments", 5, section_header_style))
    elements.append(Paragraph(f"<i>Includes: {CATEGORIES['5'][1]}</i>", item_meta_style))
    elements.append(Spacer(1, 0.08 * inch))

    non_insurance_bills = [
        b for b in recurring_bills
        if b.get("category") not in ("insurance", "pension", "investment")
    ]
    if non_insurance_bills:
        elements.append(Paragraph("Regular Bills & Commitments:", subsection_style))
        header = [["Name", "Category", "Provider", "Amount", "Frequency", "Payment"]]
        rows = header
        for bill in non_insurance_bills:
            rows.append([
                bill.get("name", ""),
                bill.get("category", "").replace("_", " ").title(),
                bill.get("provider", "") or "—",
                f"£{bill.get('amount_gbp', '')}" if bill.get("amount_gbp") else "—",
                bill.get("frequency", "").title(),
                bill.get("payment_method", "") or "—",
            ])
        t = Table(rows, colWidths=[1.4*inch, 1.0*inch, 1.2*inch, 0.8*inch, 0.9*inch, 1.0*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a2e44')),
            ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
            ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0, 0), (-1, -1), 8),
            ('ALIGN',      (0, 0), (-1, -1), 'LEFT'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ('GRID',       (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ('TOPPADDING',    (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 0.1 * inch))

    has_content = bool(non_insurance_bills) | _render_binder_entries("5") | _render_memory_items("5")
    if not has_content:
        elements.append(_empty_section(item_body_style))
        elements.append(Paragraph(
            "💡 Tip: Send FamilyBrain a message like:\n"
            "  bills: Mortgage with Halifax, £1,200/month, direct debit 1st of month\n"
            "  bills: Council tax £180/month, auto-pay\n"
            "  bills: Netflix £15.99/month, cancel if needed",
            note_style,
        ))
    elements.append(Spacer(1, 0.2 * inch))

    # -----------------------------------------------------------------------
    # Section 6 — Assets & Possessions
    # -----------------------------------------------------------------------
    elements.append(PageBreak())
    elements.append(_section_header("Assets & Possessions", 6, section_header_style))
    elements.append(Paragraph(f"<i>Includes: {CATEGORIES['6'][1]}</i>", item_meta_style))
    elements.append(Spacer(1, 0.08 * inch))

    if vehicles:
        elements.append(Paragraph("Vehicles:", subsection_style))
        for v in vehicles:
            kv = [
                ("Make & Model",   f"{v.get('make', '')} {v.get('model', '')} ({v.get('year', '')})"),
                ("Registration",   v.get("registration", "") or "—"),
                ("MOT Due",        _format_date(str(v.get("mot_due_date", "") or ""), "%d %b %Y")),
                ("Road Tax Due",   _format_date(str(v.get("tax_due_date", "") or ""), "%d %b %Y")),
                ("Insurance",      f"{v.get('insurance_provider', '')} — {v.get('insurance_policy_number', '')}" or "—"),
                ("Insurance Due",  _format_date(str(v.get("insurance_due_date", "") or ""), "%d %b %Y")),
                ("Notes",          v.get("notes", "") or "—"),
            ]
            elements.append(_kv_table(kv))
            elements.append(Spacer(1, 0.05 * inch))

    has_content = bool(vehicles) | _render_binder_entries("6") | _render_memory_items("6")
    if not has_content:
        elements.append(_empty_section(item_body_style))
        elements.append(Paragraph(
            "💡 Tip: Send FamilyBrain a message like:\n"
            "  assets: Property at 12 Oak Lane, title deeds with Smiths Solicitors\n"
            "  assets: Safe deposit box at Barclays, key in top drawer\n"
            "  assets: Jewellery collection, valued at £8,000, insured under home policy",
            note_style,
        ))
    elements.append(Spacer(1, 0.2 * inch))

    # -----------------------------------------------------------------------
    # Section 7 — Contacts & Professionals
    # -----------------------------------------------------------------------
    elements.append(PageBreak())
    elements.append(_section_header("Contacts & Professionals", 7, section_header_style))
    elements.append(Paragraph(f"<i>Includes: {CATEGORIES['7'][1]}</i>", item_meta_style))
    elements.append(Spacer(1, 0.08 * inch))

    if professional_contacts:
        elements.append(Paragraph("Professional Contacts:", subsection_style))
        header = [["Role", "Name", "Company", "Phone", "Email"]]
        rows = header
        for c in professional_contacts:
            rows.append([
                c.get("role", "") or c.get("contact_type", "") or "—",
                c.get("name", "") or "—",
                c.get("company", "") or "—",
                c.get("phone", "") or "—",
                c.get("email", "") or "—",
            ])
        t = Table(rows, colWidths=[1.1*inch, 1.2*inch, 1.4*inch, 1.1*inch, 1.5*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a2e44')),
            ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
            ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0, 0), (-1, -1), 8),
            ('ALIGN',      (0, 0), (-1, -1), 'LEFT'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ('GRID',       (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ('TOPPADDING',    (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 0.1 * inch))

    has_content = bool(professional_contacts) | _render_binder_entries("7") | _render_memory_items("7")
    if not has_content:
        elements.append(_empty_section(item_body_style))
        elements.append(Paragraph(
            "💡 Tip: Send FamilyBrain a message like:\n"
            "  contacts: Solicitor — Jane Smith, Smiths Law, 01234 567890\n"
            "  contacts: Executor — my brother David Jones, 07700 900000\n"
            "  contacts: Guardian for children — Sarah & Mike Brown if both parents die",
            note_style,
        ))
    elements.append(Spacer(1, 0.2 * inch))

    # -----------------------------------------------------------------------
    # Section 8 — Funeral & Final Wishes
    # -----------------------------------------------------------------------
    elements.append(PageBreak())
    elements.append(_section_header("Funeral & Final Wishes", 8, section_header_style))
    elements.append(Paragraph(f"<i>Includes: {CATEGORIES['8'][1]}</i>", item_meta_style))
    elements.append(Spacer(1, 0.08 * inch))

    has_content = _render_binder_entries("8") | _render_memory_items("8")
    if not has_content:
        elements.append(_empty_section(item_body_style))
        elements.append(Paragraph(
            "💡 Tip: Send FamilyBrain a message like:\n"
            "  funeral: Cremation preferred, no flowers, donate to Cancer Research UK\n"
            "  funeral: Funeral plan with Co-op Funeralcare, plan ref FP-2024-001\n"
            "  funeral: Organ donor — registered on NHS Organ Donor Register\n"
            "  funeral: Letter to loved ones stored in my desk, top left drawer",
            note_style,
        ))
    elements.append(Spacer(1, 0.2 * inch))

    # -----------------------------------------------------------------------
    # Section 9 — Digital Legacy
    # -----------------------------------------------------------------------
    elements.append(PageBreak())
    elements.append(_section_header("Digital Legacy", 9, section_header_style))
    elements.append(Paragraph(f"<i>Includes: {CATEGORIES['9'][1]}</i>", item_meta_style))
    elements.append(Spacer(1, 0.08 * inch))

    has_content = _render_binder_entries("9") | _render_memory_items("9")
    if not has_content:
        elements.append(_empty_section(item_body_style))
        elements.append(Paragraph(
            "💡 Tip: Send FamilyBrain a message like:\n"
            "  digital: Password manager is 1Password, master password in sealed envelope with solicitor\n"
            "  digital: Facebook — memorialise account, contact Sarah Jones\n"
            "  digital: Crypto — hardware wallet in safe, seed phrase with solicitor\n"
            "  digital: Cancel: Netflix, Spotify, Amazon Prime, gym membership",
            note_style,
        ))
    elements.append(Spacer(1, 0.2 * inch))

    # -----------------------------------------------------------------------
    # Section 10 — Emergency Contacts & Family Details
    # -----------------------------------------------------------------------
    elements.append(PageBreak())
    elements.append(_section_header("Emergency Contacts & Family Details", 10, section_header_style))
    elements.append(Paragraph(f"<i>Includes: {CATEGORIES['10'][1]}</i>", item_meta_style))
    elements.append(Spacer(1, 0.08 * inch))

    if medications:
        elements.append(Paragraph("Current Medications:", subsection_style))
        header = [["Person", "Medication", "Dosage", "Frequency", "Prescriber", "Pharmacy"]]
        rows = header
        for med in medications:
            rows.append([
                med.get("member_name", "") or "—",
                med.get("name", "") or "—",
                med.get("dosage", "") or "—",
                med.get("frequency", "") or "—",
                med.get("prescriber", "") or "—",
                med.get("pharmacy", "") or "—",
            ])
        t = Table(rows, colWidths=[0.9*inch, 1.2*inch, 0.8*inch, 0.9*inch, 1.1*inch, 1.4*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a2e44')),
            ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
            ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0, 0), (-1, -1), 8),
            ('ALIGN',      (0, 0), (-1, -1), 'LEFT'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ('GRID',       (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ('TOPPADDING',    (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 0.1 * inch))

    has_content = bool(medications) | _render_binder_entries("10") | _render_memory_items("10")
    if not has_content:
        elements.append(_empty_section(item_body_style))
        elements.append(Paragraph(
            "💡 Tip: Send FamilyBrain a message like:\n"
            "  family: Dan — NHS number 123 456 7890, blood type O+, allergic to penicillin\n"
            "  family: GP — Dr Ahmed, The Surgery, 01234 567890\n"
            "  family: School — St Mary's Primary, headteacher Mrs Jones, 01234 567891",
            note_style,
        ))
    elements.append(Spacer(1, 0.2 * inch))

    # -----------------------------------------------------------------------
    # Section 11 — Upcoming Events
    # -----------------------------------------------------------------------
    elements.append(PageBreak())
    elements.append(Paragraph("11. Upcoming Events (Next 30 Days)", section_header_style))
    elements.append(Paragraph(
        "<i>Important dates, renewals, and appointments coming up.</i>", item_meta_style,
    ))
    elements.append(Spacer(1, 0.1 * inch))

    if not upcoming_events:
        elements.append(Paragraph("<i>No upcoming events found.</i>", item_body_style))
    else:
        header = [["Date", "Time", "Event", "Member"]]
        rows = header
        for event in upcoming_events:
            date_str = event.get("event_date", "")
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                date_display = dt.strftime("%d %b")
            except Exception:
                date_display = date_str
            rows.append([
                date_display,
                event.get("event_time") or "All day",
                event.get("event_name", ""),
                event.get("family_member", ""),
            ])
        t = Table(rows, colWidths=[0.9*inch, 0.9*inch, 3.5*inch, 1.0*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a2e44')),
            ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
            ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0, 0), (-1, -1), 9),
            ('ALIGN',      (0, 0), (-1, -1), 'LEFT'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ('GRID',       (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ('TOPPADDING',    (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING',   (0, 0), (-1, -1), 5),
        ]))
        elements.append(t)

    # -----------------------------------------------------------------------
    # Build
    # -----------------------------------------------------------------------
    doc.build(elements, onFirstPage=_create_footer, onLaterPages=_create_footer)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    logger.info("Successfully generated emergency PDF (%d bytes)", len(pdf_bytes))
    return pdf_bytes
