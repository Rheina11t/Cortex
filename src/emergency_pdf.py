import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

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
)

from . import brain

logger = logging.getLogger("open_brain.emergency_pdf")

# The 10 Categories
CATEGORIES = {
    "1": ("Legal & Personal Documents", "will, LPA, birth/marriage certs, passport, NI, NHS numbers"),
    "2": ("Financial Accounts & Access", "bank details, sort codes, account numbers, password manager"),
    "3": ("Insurance Policies", "life, home, car, pet, health — policy numbers, providers, renewal dates"),
    "4": ("Pensions & Investments", "state pension, workplace/private pensions, ISAs"),
    "5": ("Bills, Debts & Regular Payments", "mortgage, utilities, subscriptions, direct debits"),
    "6": ("Assets & Possessions", "property deeds, car V5/MOT, valuables inventory"),
    "7": ("Contacts & Professionals", "solicitor, accountant, GP, dentist, school, executor, guardians"),
    "8": ("Funeral & Final Wishes", "burial/cremation preference, organ donation, funeral plan"),
    "9": ("Digital Legacy", "social media instructions, crypto, subscriptions to cancel"),
    "10": ("Emergency Contacts & Family Details", "family members, NHS numbers, allergies, medications, blood types, school names"),
}

def _mask_sensitive_data(text: str) -> str:
    """Mask sensitive data like full account numbers or passwords."""
    if not text:
        return text
    
    # Simple masking: if we see what looks like an account number (8+ digits), mask all but last 4
    import re
    
    def mask_digits(match):
        digits = match.group(0)
        if len(digits) >= 8:
            return "*" * (len(digits) - 4) + digits[-4:]
        return digits
        
    # Mask 8+ consecutive digits
    masked = re.sub(r'\b\d{8,}\b', mask_digits, text)
    
    # Mask things that look like passwords (basic heuristic)
    # E.g., "password: mysecret" -> "password: ****"
    masked = re.sub(r'(?i)(password|pwd|passcode)[\s:=]+([^\s,;]+)', r'\1: ****', masked)
    
    return masked

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
    """Fetch all memories with emergency_category set."""
    db = brain._supabase
    if not db:
        return {str(i): [] for i in range(1, 11)}
        
    items_by_category = {str(i): [] for i in range(1, 11)}
    
    try:
        # We need to fetch all memories for the family and filter by metadata->emergency_category
        # Since we can't easily query by a specific JSON key existence in the python client,
        # we'll fetch recent memories and filter in memory, or use a contains query if possible.
        
        # Let's try to get all memories for the family (might need pagination if large, but fine for now)
        result = db.table("memories") \
            .select("id, content, metadata, created_at") \
            .contains("metadata", {"family_id": family_id}) \
            .order("created_at", desc=True) \
            .limit(1000) \
            .execute()
            
        for row in (result.data or []):
            metadata = row.get("metadata", {})
            cat = metadata.get("emergency_category")
            if cat:
                # Map string categories to numbers if needed
                cat_num = None
                if str(cat).isdigit() and 1 <= int(cat) <= 10:
                    cat_num = str(cat)
                else:
                    # Map string names to numbers
                    cat_lower = str(cat).lower()
                    if "legal" in cat_lower: cat_num = "1"
                    elif "finance" in cat_lower or "bank" in cat_lower: cat_num = "2"
                    elif "insurance" in cat_lower: cat_num = "3"
                    elif "pension" in cat_lower or "invest" in cat_lower: cat_num = "4"
                    elif "bill" in cat_lower or "debt" in cat_lower: cat_num = "5"
                    elif "asset" in cat_lower or "car" in cat_lower: cat_num = "6"
                    elif "contact" in cat_lower and "emergency" not in cat_lower: cat_num = "7"
                    elif "funeral" in cat_lower or "wish" in cat_lower: cat_num = "8"
                    elif "digital" in cat_lower or "legacy" in cat_lower: cat_num = "9"
                    elif "family" in cat_lower or "medical" in cat_lower or "emergency_contact" in cat_lower: cat_num = "10"
                
                if cat_num and cat_num in items_by_category:
                    items_by_category[cat_num].append(row)
                    
    except Exception as exc:
        logger.error("Failed to fetch emergency items: %s", exc)
        
    return items_by_category

def _create_footer(canvas, doc):
    """Add footer to every page."""
    canvas.saveState()
    canvas.setFont('Helvetica', 9)
    canvas.setFillColor(colors.gray)
    footer_text = f"Generated by FamilyBrain — Keep this document in a safe place | Page {doc.page}"
    canvas.drawCentredString(A4[0] / 2.0, 0.5 * inch, footer_text)
    canvas.restoreState()

def generate_emergency_pdf(family_id: str) -> bytes:
    """Generate the 'If Anything Happens' PDF for a family."""
    logger.info("Generating emergency PDF for family_id=%s", family_id)
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=72,
        leftMargin=72,
        topMargin=72,
        bottomMargin=72
    )
    
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'TitleStyle',
        parent=styles['Heading1'],
        fontSize=24,
        spaceAfter=30,
        alignment=1, # Center
        textColor=colors.darkblue
    )
    
    subtitle_style = ParagraphStyle(
        'SubtitleStyle',
        parent=styles['Normal'],
        fontSize=14,
        spaceAfter=20,
        alignment=1,
        textColor=colors.dimgray
    )
    
    watermark_style = ParagraphStyle(
        'WatermarkStyle',
        parent=styles['Normal'],
        fontSize=16,
        spaceAfter=40,
        alignment=1,
        textColor=colors.red,
        fontName='Helvetica-Bold'
    )
    
    section_header_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading2'],
        fontSize=16,
        spaceBefore=20,
        spaceAfter=10,
        textColor=colors.white,
        backColor=colors.darkblue,
        borderPadding=(5, 5, 5, 5)
    )
    
    item_title_style = ParagraphStyle(
        'ItemTitle',
        parent=styles['Heading3'],
        fontSize=12,
        spaceBefore=10,
        spaceAfter=5,
        textColor=colors.darkblue
    )
    
    item_body_style = ParagraphStyle(
        'ItemBody',
        parent=styles['Normal'],
        fontSize=10,
        spaceAfter=10,
        leading=14
    )
    
    item_meta_style = ParagraphStyle(
        'ItemMeta',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.gray,
        spaceAfter=15
    )
    
    toc_style = ParagraphStyle(
        'TOC',
        parent=styles['Normal'],
        fontSize=12,
        spaceAfter=5
    )
    
    elements = []
    
    # --- Cover Page ---
    elements.append(Spacer(1, 2 * inch))
    elements.append(Paragraph("Family Emergency File", title_style))
    elements.append(Paragraph("CONFIDENTIAL", watermark_style))
    
    # Get family members
    members = _get_family_members(family_id)
    family_name_str = " & ".join(members) if members else "Family"
    
    elements.append(Paragraph(f"For: {family_name_str}", subtitle_style))
    
    date_str = datetime.now().strftime("%d %B %Y")
    elements.append(Paragraph(f"Generated on: {date_str}", subtitle_style))
    
    elements.append(PageBreak())
    
    # --- Table of Contents ---
    elements.append(Paragraph("Table of Contents", styles['Heading1']))
    elements.append(Spacer(1, 0.2 * inch))
    
    for i in range(1, 11):
        cat_num = str(i)
        cat_name, _ = CATEGORIES[cat_num]
        elements.append(Paragraph(f"{i}. {cat_name}", toc_style))
        
    elements.append(Paragraph("11. Upcoming Events (Next 30 Days)", toc_style))
    
    elements.append(PageBreak())
    
    # --- Fetch Data ---
    items_by_category = _get_emergency_items(family_id)
    upcoming_events = _get_upcoming_events(family_id)
    
    # --- Sections ---
    for i in range(1, 11):
        cat_num = str(i)
        cat_name, cat_desc = CATEGORIES[cat_num]
        
        # Section Header
        elements.append(Paragraph(f"{i}. {cat_name}", section_header_style))
        elements.append(Paragraph(f"<i>Includes: {cat_desc}</i>", item_meta_style))
        elements.append(Spacer(1, 0.1 * inch))
        
        items = items_by_category.get(cat_num, [])
        
        if not items:
            elements.append(Paragraph("<i>No items stored in this category yet.</i>", item_body_style))
            elements.append(Spacer(1, 0.2 * inch))
            continue
            
        for item in items:
            metadata = item.get("metadata", {})
            content = item.get("content", "")
            created_at = item.get("created_at", "")
            
            # Format date
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                date_display = dt.strftime("%d %b %Y")
            except:
                date_display = created_at[:10] if created_at else "Unknown date"
                
            # Determine title
            doc_type = metadata.get("document_type", "Document").replace("_", " ").title()
            source_user = metadata.get("source_user", "Family Member").title()
            title = f"{doc_type} (Added by {source_user})"
            
            # Mask sensitive data
            safe_content = _mask_sensitive_data(content)
            
            # Add to elements
            elements.append(Paragraph(title, item_title_style))
            
            # Handle newlines in content
            for paragraph in safe_content.split('\n'):
                if paragraph.strip():
                    elements.append(Paragraph(paragraph.strip(), item_body_style))
                    
            elements.append(Paragraph(f"Stored on: {date_display}", item_meta_style))
            
        elements.append(Spacer(1, 0.3 * inch))
        
    # --- Upcoming Events Section ---
    elements.append(PageBreak())
    elements.append(Paragraph("11. Upcoming Events (Next 30 Days)", section_header_style))
    elements.append(Paragraph("<i>Important dates, renewals, and appointments coming up.</i>", item_meta_style))
    elements.append(Spacer(1, 0.1 * inch))
    
    if not upcoming_events:
        elements.append(Paragraph("<i>No upcoming events found.</i>", item_body_style))
    else:
        # Create a table for events
        data = [["Date", "Time", "Event", "Member"]]
        
        for event in upcoming_events:
            date_str = event.get("event_date", "")
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                date_display = dt.strftime("%d %b")
            except:
                date_display = date_str
                
            time_str = event.get("event_time") or "All day"
            name = event.get("event_name", "")
            member = event.get("family_member", "")
            
            data.append([date_display, time_str, name, member])
            
        table = Table(data, colWidths=[1 * inch, 1 * inch, 3 * inch, 1.5 * inch])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.white),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
        ]))
        
        elements.append(table)
        
    # Build PDF
    doc.build(elements, onFirstPage=_create_footer, onLaterPages=_create_footer)
    
    pdf_bytes = buffer.getvalue()
    buffer.close()
    
    logger.info("Successfully generated emergency PDF (%d bytes)", len(pdf_bytes))
    return pdf_bytes
