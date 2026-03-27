"""
binder_checklist.py
===================
Per-item checklist engine for the FamilyBrain death binder / emergency file.

Public API
----------
compute_checklist(family_id)  → ChecklistResult
    Queries all data sources, evaluates every checklist item, writes the
    result to binder_progress, and returns a structured result object.

format_binder_status(result)  → str
    Renders the /binder WhatsApp reply from a ChecklistResult.

maybe_send_nudge(family_id, from_number, prev_pct, result)
    Sends a proactive nudge if the user has crossed a threshold or is ≥80%
    complete with specific items still missing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Checklist definition
# ---------------------------------------------------------------------------
# Each CheckItem defines one trackable piece of information.
#
# Fields
# ------
# key        Unique slug used in binder_progress.item_state JSON
# label      Human-readable name shown in /binder output
# cat        Category number (str "1"–"10")
# example    Example command the user can send to fill this item
# required   True = item counts toward category completion
#
# Detection is done in _detect_items() below, which maps each key to a
# query against death_binder_entries, memories, or structured tables.
# ---------------------------------------------------------------------------

@dataclass
class CheckItem:
    key: str
    label: str
    cat: str
    example: str
    required: bool = True


# Full checklist — 10 categories, 28 items total
CHECKLIST: list[CheckItem] = [
    # ── 1. Legal & Personal Documents ────────────────────────────────────────
    CheckItem("will",          "Will",                  "1", "will: Will held at Smiths Solicitors, ref W-2024"),
    CheckItem("lpa_health",    "LPA (health & welfare)", "1", "lpa: Health LPA signed 2023, stored at solicitors"),
    CheckItem("lpa_finance",   "LPA (property & finance)","1","lpa: Finance LPA signed 2023, ref LPA-FIN-001"),
    CheckItem("passport_id",   "Passport / photo ID",   "1", "legal: Passport no. 123456789, expires Jan 2030"),
    CheckItem("ni_number",     "National Insurance no.", "1", "ni: NI number AB 12 34 56 C"),
    CheckItem("nhs_number",    "NHS number",            "10", "nhs: NHS number 123 456 7890"),

    # ── 2. Financial Accounts & Access ───────────────────────────────────────
    CheckItem("bank_account",  "Bank account",          "2", "bank: Barclays current, sort 20-00-00, acc 12345678"),
    CheckItem("password_mgr",  "Password manager info", "2", "password: 1Password, vault key in sealed envelope in safe"),

    # ── 3. Insurance Policies ────────────────────────────────────────────────
    CheckItem("life_insurance","Life insurance",        "3", "insurance: Legal & General life, policy LG-001, renews Jan 2026"),
    CheckItem("home_insurance","Home / buildings insurance","3","insurance: Aviva home, policy AV-HOME-123, renews Mar 2026"),
    CheckItem("car_insurance", "Car insurance",         "3", "insurance: Admiral car, policy ADM-456, renews Aug 2025"),

    # ── 4. Pensions & Investments ────────────────────────────────────────────
    CheckItem("pension",       "At least one pension",  "4", "pension: Nest workplace pension, ref NEST-12345"),
    CheckItem("state_pension", "State pension info",    "4", "pension: State pension forecast at gov.uk/check-state-pension"),

    # ── 5. Bills, Debts & Regular Payments ───────────────────────────────────
    CheckItem("mortgage_rent", "Mortgage / rent",       "5", "mortgage: Halifax mortgage £1,200/month, DD 1st"),
    CheckItem("utility",       "At least one utility",  "5", "bills: British Gas energy, DD £120/month, auto-pay"),

    # ── 6. Assets & Possessions ──────────────────────────────────────────────
    CheckItem("property_vehicle","Property or vehicle details","6","assets: House deeds at solicitors; car V5 in filing cabinet"),

    # ── 7. Contacts & Professionals ──────────────────────────────────────────
    CheckItem("executor",      "Executor / solicitor",  "7", "executor: Brother David Jones, 07700 900000"),
    CheckItem("emergency_contact","Emergency contact",  "7", "contacts: Sister Jane Smith, 07700 911111"),

    # ── 8. Funeral & Final Wishes ────────────────────────────────────────────
    CheckItem("burial_pref",   "Burial / cremation preference","8","funeral: Cremation, no flowers, donate organs"),
    CheckItem("organ_donation","Organ donation wishes", "8", "organ: Yes — registered on NHS Organ Donor Register"),

    # ── 9. Digital Legacy ────────────────────────────────────────────────────
    CheckItem("social_media",  "Social media / email instructions","9","digital: Facebook — memorialise; email password in 1Password"),
    CheckItem("subscriptions", "Subscriptions to cancel","9","digital: Cancel Netflix, Spotify, Amazon Prime"),

    # ── 10. Family & Medical ─────────────────────────────────────────────────
    CheckItem("gp_details",    "GP / doctor details",   "10","gp: Dr Patel, Riverside Surgery, 01234 567890"),
    CheckItem("allergies",     "Allergies / conditions","10","allergy: Dan — penicillin allergy; Emma — nut allergy"),
    CheckItem("medications",   "Current medications",   "10","family: Dan takes 10mg Lisinopril daily for blood pressure"),
]

# Build a lookup by key
CHECKLIST_BY_KEY: dict[str, CheckItem] = {item.key: item for item in CHECKLIST}

# Items grouped by category
CHECKLIST_BY_CAT: dict[str, list[CheckItem]] = {}
for _item in CHECKLIST:
    CHECKLIST_BY_CAT.setdefault(_item.cat, []).append(_item)

# Category names (mirrors CATEGORIES in emergency_pdf.py)
CAT_NAMES: dict[str, str] = {
    "1":  "Legal & Personal Documents",
    "2":  "Financial Accounts & Access",
    "3":  "Insurance Policies",
    "4":  "Pensions & Investments",
    "5":  "Bills, Debts & Regular Payments",
    "6":  "Assets & Possessions",
    "7":  "Contacts & Professionals",
    "8":  "Funeral & Final Wishes",
    "9":  "Digital Legacy",
    "10": "Family & Medical",
}

# Short names for inline messaging
CAT_SHORT: dict[str, str] = {
    "1":  "Legal documents",
    "2":  "Financial accounts",
    "3":  "Insurance",
    "4":  "Pensions",
    "5":  "Bills & debts",
    "6":  "Assets",
    "7":  "Contacts",
    "8":  "Funeral wishes",
    "9":  "Digital legacy",
    "10": "Family & medical",
}

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ChecklistResult:
    """Full checklist evaluation for one family."""
    family_id: str
    item_state: dict[str, bool]          # key → True/False
    items_complete: int = 0
    items_total: int = 0
    cats_complete: int = 0
    cats_total: int = 10
    pct_complete: int = 0

    # Which categories have ALL required items ticked
    complete_cats: set[str] = field(default_factory=set)
    # Which categories have at least one item but not all
    partial_cats: set[str] = field(default_factory=set)
    # Which categories have zero items
    empty_cats: set[str] = field(default_factory=set)

    # Items that are still missing (for nudge messages)
    missing_items: list[CheckItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _subcats_for_family(db: Any, family_id: str) -> set[str]:
    """Return the set of subcategory strings stored in death_binder_entries."""
    try:
        result = db.table("death_binder_entries") \
            .select("subcategory, value") \
            .eq("family_id", family_id) \
            .execute()
        return {
            (row.get("subcategory") or "").lower()
            for row in (result.data or [])
        }
    except Exception as exc:
        logger.warning("_subcats_for_family: %s", exc)
        return set()


def _binder_values_for_cat(db: Any, family_id: str, cat: str) -> list[str]:
    """Return all stored value strings for a category (lowercased)."""
    try:
        result = db.table("death_binder_entries") \
            .select("value, subcategory") \
            .eq("family_id", family_id) \
            .eq("category", cat) \
            .execute()
        return [
            (row.get("value") or "").lower()
            for row in (result.data or [])
        ]
    except Exception as exc:
        logger.warning("_binder_values_for_cat: %s", exc)
        return []


def _memory_content_for_cat(db: Any, family_id: str, cat: str) -> list[str]:
    """Return memory content strings tagged to an emergency category."""
    try:
        result = db.table("memories") \
            .select("content, metadata") \
            .contains("metadata", {"family_id": family_id}) \
            .limit(500) \
            .execute()
        out = []
        for row in (result.data or []):
            meta = row.get("metadata") or {}
            if str(meta.get("emergency_category", "")) == cat:
                out.append((row.get("content") or "").lower())
        return out
    except Exception as exc:
        logger.warning("_memory_content_for_cat: %s", exc)
        return []


def _any_text_matches(texts: list[str], *keywords: str) -> bool:
    """Return True if any text in the list contains any of the keywords."""
    for text in texts:
        for kw in keywords:
            if kw in text:
                return True
    return False


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def _detect_items(db: Any, family_id: str) -> dict[str, bool]:
    """
    Query all data sources and return a dict of {item_key: bool}.
    Each item is True if we have evidence it has been filled in.
    """
    state: dict[str, bool] = {item.key: False for item in CHECKLIST}

    subcats = _subcats_for_family(db, family_id)

    # ── helpers ──────────────────────────────────────────────────────────────
    def has_subcat(*names: str) -> bool:
        return any(n in subcats for n in names)

    def binder_vals(cat: str) -> list[str]:
        return _binder_values_for_cat(db, family_id, cat)

    def mem_vals(cat: str) -> list[str]:
        return _memory_content_for_cat(db, family_id, cat)

    def any_match(cat: str, *kws: str) -> bool:
        return _any_text_matches(binder_vals(cat) + mem_vals(cat), *kws)

    # ── 1. Legal & Personal ──────────────────────────────────────────────────
    cat1_vals = binder_vals("1") + mem_vals("1")
    state["will"]        = has_subcat("will") or _any_text_matches(cat1_vals, "will")
    state["lpa_health"]  = has_subcat("lpa") or _any_text_matches(cat1_vals, "lpa", "lasting power", "health", "welfare")
    state["lpa_finance"] = has_subcat("lpa") or _any_text_matches(cat1_vals, "lpa", "lasting power", "finance", "property")
    state["passport_id"] = has_subcat("legal_document") or _any_text_matches(cat1_vals, "passport", "driving licence", "photo id")
    state["ni_number"]   = has_subcat("ni_number") or _any_text_matches(cat1_vals, "ni number", "national insurance", "ni:")

    # ── 2. Financial ─────────────────────────────────────────────────────────
    cat2_vals = binder_vals("2") + mem_vals("2")
    # Also check structured financial_accounts table
    try:
        fa = db.table("financial_accounts").select("id").eq("family_id", family_id).limit(1).execute()
        has_fa = bool(fa.data)
    except Exception:
        has_fa = False
    state["bank_account"] = has_fa or has_subcat("bank_account", "financial_account") or bool(cat2_vals)
    state["password_mgr"] = has_subcat("password_manager") or _any_text_matches(cat2_vals, "password", "1password", "bitwarden", "lastpass", "dashlane", "keychain")

    # ── 3. Insurance ─────────────────────────────────────────────────────────
    cat3_vals = binder_vals("3") + mem_vals("3")
    # Also check recurring_bills with category='insurance'
    try:
        rb_ins = db.table("recurring_bills").select("name, notes").eq("family_id", family_id).eq("category", "insurance").execute()
        ins_names = " ".join((r.get("name") or "") + " " + (r.get("notes") or "") for r in (rb_ins.data or [])).lower()
    except Exception:
        ins_names = ""
    all_ins = cat3_vals + ([ins_names] if ins_names else [])
    state["life_insurance"] = _any_text_matches(all_ins, "life", "life insurance", "term assurance", "whole of life")
    state["home_insurance"] = _any_text_matches(all_ins, "home", "buildings", "contents", "house insurance")
    state["car_insurance"]  = _any_text_matches(all_ins, "car", "vehicle", "motor", "auto")
    # Also check vehicles table for insurance_provider
    try:
        veh = db.table("vehicles").select("insurance_provider").eq("family_id", family_id).limit(5).execute()
        for v in (veh.data or []):
            if v.get("insurance_provider"):
                state["car_insurance"] = True
    except Exception:
        pass

    # ── 4. Pensions ───────────────────────────────────────────────────────────
    cat4_vals = binder_vals("4") + mem_vals("4")
    state["pension"]       = has_subcat("pension", "investment", "isa", "shares") or bool(cat4_vals)
    state["state_pension"] = _any_text_matches(cat4_vals, "state pension", "gov.uk", "hmrc", "national insurance record", "forecast")

    # ── 5. Bills ─────────────────────────────────────────────────────────────
    cat5_vals = binder_vals("5") + mem_vals("5")
    # Also check recurring_bills
    try:
        rb_all = db.table("recurring_bills").select("category, name").eq("family_id", family_id).execute()
        rb_cats = {(r.get("category") or "").lower() for r in (rb_all.data or [])}
        rb_names = " ".join((r.get("name") or "") for r in (rb_all.data or [])).lower()
    except Exception:
        rb_cats = set()
        rb_names = ""
    all_bills = cat5_vals + ([rb_names] if rb_names else [])
    state["mortgage_rent"] = (
        "mortgage" in rb_cats or "rent" in rb_cats
        or has_subcat("mortgage", "rent")
        or _any_text_matches(all_bills, "mortgage", "rent", "landlord")
    )
    state["utility"] = (
        "energy" in rb_cats or "gas" in rb_cats or "electric" in rb_cats
        or "water" in rb_cats or "broadband" in rb_cats or "council_tax" in rb_cats
        or has_subcat("bill")
        or _any_text_matches(all_bills, "gas", "electric", "energy", "water", "broadband", "council tax", "utility")
    )

    # ── 6. Assets ─────────────────────────────────────────────────────────────
    cat6_vals = binder_vals("6") + mem_vals("6")
    # Check vehicles table
    try:
        veh_count = db.table("vehicles").select("id").eq("family_id", family_id).limit(1).execute()
        has_vehicle = bool(veh_count.data)
    except Exception:
        has_vehicle = False
    state["property_vehicle"] = (
        has_vehicle
        or has_subcat("property", "vehicle", "asset", "safe_deposit_box", "valuables")
        or bool(cat6_vals)
    )

    # ── 7. Contacts ───────────────────────────────────────────────────────────
    cat7_vals = binder_vals("7") + mem_vals("7")
    # Check professional_contacts table
    try:
        pc = db.table("professional_contacts").select("relationship, role").eq("family_id", family_id).execute()
        pc_roles = " ".join(
            (r.get("relationship") or "") + " " + (r.get("role") or "")
            for r in (pc.data or [])
        ).lower()
    except Exception:
        pc_roles = ""
    all_contacts = cat7_vals + ([pc_roles] if pc_roles else [])
    state["executor"] = (
        has_subcat("executor", "solicitor")
        or _any_text_matches(all_contacts, "executor", "solicitor", "lawyer", "legal adviser")
    )
    state["emergency_contact"] = (
        has_subcat("professional_contact")
        or bool(all_contacts)
        or _any_text_matches(all_contacts, "contact", "next of kin", "emergency")
    )

    # ── 8. Funeral ────────────────────────────────────────────────────────────
    cat8_vals = binder_vals("8") + mem_vals("8")
    state["burial_pref"]   = has_subcat("burial_preference", "funeral_wishes") or bool(cat8_vals)
    state["organ_donation"] = has_subcat("organ_donation") or _any_text_matches(cat8_vals, "organ", "donation", "donor")

    # ── 9. Digital Legacy ─────────────────────────────────────────────────────
    cat9_vals = binder_vals("9") + mem_vals("9")
    state["social_media"]  = has_subcat("digital_legacy") or _any_text_matches(cat9_vals, "facebook", "instagram", "twitter", "email", "social media", "google", "apple id", "memorialise")
    state["subscriptions"] = has_subcat("digital_legacy", "crypto") or _any_text_matches(cat9_vals, "cancel", "netflix", "spotify", "amazon", "subscription", "crypto", "bitcoin")

    # ── 10. Family & Medical ──────────────────────────────────────────────────
    cat10_vals = binder_vals("10") + mem_vals("10")
    # Also check medications table
    try:
        meds = db.table("medications").select("id").eq("family_id", family_id).eq("active", True).limit(1).execute()
        has_meds = bool(meds.data)
    except Exception:
        has_meds = False
    state["nhs_number"]  = has_subcat("nhs_number") or _any_text_matches(cat10_vals, "nhs", "nhs number")
    state["gp_details"]  = has_subcat("gp_details") or _any_text_matches(cat10_vals, "gp", "doctor", "surgery", "dr ")
    state["allergies"]   = has_subcat("allergy") or _any_text_matches(cat10_vals, "allerg", "intoleran", "reaction")
    state["medications"] = has_meds or has_subcat("family_details") or _any_text_matches(cat10_vals, "medication", "tablet", "mg", "prescription", "takes ")

    return state


# ---------------------------------------------------------------------------
# Compute and cache
# ---------------------------------------------------------------------------

def compute_checklist(family_id: str) -> ChecklistResult:
    """
    Evaluate the full checklist for a family, write to binder_progress,
    and return a ChecklistResult.
    """
    # Import here to avoid circular import at module load time
    from . import brain  # type: ignore

    db = brain._supabase
    result = ChecklistResult(family_id=family_id)
    result.items_total = len(CHECKLIST)

    if not db:
        logger.warning("compute_checklist: no Supabase client")
        return result

    # Detect items
    try:
        item_state = _detect_items(db, family_id)
    except Exception as exc:
        logger.error("compute_checklist _detect_items failed: %s", exc)
        item_state = {item.key: False for item in CHECKLIST}

    result.item_state = item_state

    # Tally
    result.items_complete = sum(1 for v in item_state.values() if v)
    result.pct_complete = int(result.items_complete / result.items_total * 100) if result.items_total else 0

    # Per-category analysis
    for cat, items in CHECKLIST_BY_CAT.items():
        required = [i for i in items if i.required]
        done = [i for i in required if item_state.get(i.key)]
        if len(done) == len(required):
            result.complete_cats.add(cat)
        elif done:
            result.partial_cats.add(cat)
        else:
            result.empty_cats.add(cat)

    result.cats_complete = len(result.complete_cats)
    result.missing_items = [i for i in CHECKLIST if i.required and not item_state.get(i.key)]

    # Write to binder_progress cache
    try:
        row = {
            "family_id":     family_id,
            "item_state":    item_state,
            "items_complete": result.items_complete,
            "items_total":   result.items_total,
            "cats_complete": result.cats_complete,
            "cats_total":    10,
            "pct_complete":  result.pct_complete,
        }
        db.table("binder_progress").upsert(row, on_conflict="family_id").execute()
    except Exception as exc:
        logger.warning("compute_checklist: failed to write binder_progress: %s", exc)

    return result


def get_cached_pct(family_id: str) -> int:
    """Return the last cached pct_complete for a family (0 if not found)."""
    from . import brain  # type: ignore
    db = brain._supabase
    if not db:
        return 0
    try:
        result = db.table("binder_progress") \
            .select("pct_complete") \
            .eq("family_id", family_id) \
            .limit(1) \
            .execute()
        if result.data:
            return result.data[0].get("pct_complete", 0)
    except Exception as exc:
        logger.warning("get_cached_pct: %s", exc)
    return 0


def record_nudge_sent(family_id: str, pct: int) -> None:
    """Update last_nudge_at and last_nudge_pct in binder_progress."""
    from . import brain  # type: ignore
    db = brain._supabase
    if not db:
        return
    try:
        db.table("binder_progress").upsert(
            {
                "family_id":      family_id,
                "last_nudge_at":  datetime.now(timezone.utc).isoformat(),
                "last_nudge_pct": pct,
            },
            on_conflict="family_id",
        ).execute()
    except Exception as exc:
        logger.warning("record_nudge_sent: %s", exc)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_binder_status(result: ChecklistResult) -> str:
    """
    Render the /binder WhatsApp reply.

    Example output:
        📋 *Your emergency file: 7/10 sections complete*

        ✅ Legal & Personal Documents
        ✅ Financial Accounts & Access
        ...
        ❌ Funeral Wishes — send: funeral: cremation, no flowers
        ❌ Digital Legacy — send: digital: cancel Netflix; email in 1Password

        You're 72% done. Add your funeral wishes and you'll have a complete file.
        Send /sos to generate the PDF with what you have so far.
    """
    lines: list[str] = []
    pct = result.pct_complete
    n_cats = result.cats_complete

    lines.append(f"📋 *Your emergency file: {n_cats}/10 sections complete*\n")

    for i in range(1, 11):
        cat = str(i)
        cat_name = CAT_SHORT[cat]
        items_in_cat = CHECKLIST_BY_CAT.get(cat, [])
        required = [it for it in items_in_cat if it.required]
        done = [it for it in required if result.item_state.get(it.key)]
        missing = [it for it in required if not result.item_state.get(it.key)]

        if cat in result.complete_cats:
            lines.append(f"✅ {cat_name}")
        elif cat in result.partial_cats:
            # Show which specific items are still missing
            missing_labels = ", ".join(it.label for it in missing[:2])
            if len(missing) > 2:
                missing_labels += f" +{len(missing) - 2} more"
            lines.append(f"🔶 {cat_name} — still need: {missing_labels}")
        else:
            # Completely empty — show the first example command
            first_item = required[0] if required else None
            if first_item:
                lines.append(f"❌ {cat_name} — send: _{first_item.example}_")
            else:
                lines.append(f"❌ {cat_name}")

    lines.append("")

    # Closing summary line
    if pct == 100:
        lines.append("🎉 *Your emergency file is complete!* Send /sos to generate the PDF.")
    elif pct >= 80:
        # Specific nudge for the last few items
        first_missing = result.missing_items[0] if result.missing_items else None
        if first_missing:
            cat_name = CAT_SHORT[first_missing.cat]
            lines.append(
                f"You're {pct}% done — almost there! "
                f"Add your *{cat_name.lower()}* and you'll have a near-complete emergency file."
            )
        else:
            lines.append(f"You're {pct}% done. Send /sos to generate your PDF.")
    elif pct >= 50:
        n_missing_cats = 10 - n_cats
        lines.append(
            f"You're {pct}% done ({n_cats}/10 sections). "
            f"{n_missing_cats} section{'s' if n_missing_cats != 1 else ''} still to fill in."
        )
    else:
        lines.append(
            f"You're {pct}% done. Keep going — every section you add makes the file more useful."
        )

    lines.append("Send /sos to generate the PDF with what you have so far.")
    return "\n".join(lines)


def format_save_confirmation(
    result: ChecklistResult,
    cat_num: str,
    cat_name: str,
    prev_pct: int,
) -> str:
    """
    Build the confirmation message shown after a user saves a binder entry.

    Examples:
        ✅ Saved to your *Funeral Wishes* section.

        📋 8/10 sections covered. Just your digital legacy and assets left.
        Send /sos to generate your full 'If Anything Happens' PDF.

    or when a category just became complete:
        ✅ Saved to your *Insurance* section.

        🎉 That's your insurance sorted! You're now 7/10 complete.
        Still to add: funeral wishes, digital legacy.
        Send /sos to generate your full 'If Anything Happens' PDF.
    """
    pct = result.pct_complete
    n_cats = result.cats_complete
    just_completed = cat_num in result.complete_cats

    lines = [f"✅ Saved to your *{cat_name}* section.\n"]

    if pct == 100:
        lines.append("🎉 *Your emergency file is now complete across all 10 sections!*")
        lines.append("Send /sos to generate your full 'If Anything Happens' PDF.")
        return "\n".join(lines)

    if just_completed:
        lines.append(f"🎉 That's your *{CAT_SHORT[cat_num].lower()}* sorted!")

    lines.append(f"📋 {n_cats}/10 sections covered ({pct}% complete).")

    # Name the remaining missing categories (max 3)
    missing_cats = [
        CAT_SHORT[str(i)]
        for i in range(1, 11)
        if str(i) not in result.complete_cats
    ]
    if missing_cats:
        if len(missing_cats) <= 3:
            missing_str = ", ".join(f"*{c.lower()}*" for c in missing_cats)
            lines.append(f"Still to add: {missing_str}.")
        else:
            first_three = ", ".join(f"*{c.lower()}*" for c in missing_cats[:3])
            lines.append(f"Still to add: {first_three} and {len(missing_cats) - 3} more.")

    lines.append("Send /sos to generate your full 'If Anything Happens' PDF.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Proactive nudge
# ---------------------------------------------------------------------------

def maybe_send_nudge(
    family_id: str,
    from_number: str,
    prev_pct: int,
    result: ChecklistResult,
) -> None:
    """
    Send a proactive nudge if the user has crossed a meaningful threshold
    or is ≥80% complete with specific items still missing.

    Nudge conditions (any one triggers):
      - Crossed 50%, 70%, 80%, 90% since last nudge
      - Is ≥80% and has been ≥5% since last nudge
      - Just completed a category that was previously empty
    """
    from . import brain  # type: ignore

    pct = result.pct_complete

    # Read last nudge state from DB
    db = brain._supabase
    last_nudge_pct = 0
    if db:
        try:
            row = db.table("binder_progress") \
                .select("last_nudge_pct, last_nudge_at") \
                .eq("family_id", family_id) \
                .limit(1) \
                .execute()
            if row.data:
                last_nudge_pct = row.data[0].get("last_nudge_pct", 0)
        except Exception as exc:
            logger.warning("maybe_send_nudge: read last_nudge_pct: %s", exc)

    # Thresholds that trigger a nudge
    thresholds = [50, 70, 80, 90, 100]
    crossed = any(last_nudge_pct < t <= pct for t in thresholds)
    high_and_progressing = pct >= 80 and (pct - last_nudge_pct) >= 5

    if not (crossed or high_and_progressing):
        return

    # Build the nudge message
    try:
        from .whatsapp_capture import _send_proactive_message  # type: ignore
    except ImportError:
        logger.warning("maybe_send_nudge: could not import _send_proactive_message")
        return

    n_cats = result.cats_complete
    missing_items = result.missing_items

    if pct == 100:
        msg = (
            "🎉 *Your emergency file is 100% complete!*\n"
            "Send /sos to generate your full 'If Anything Happens' PDF."
        )
    elif pct >= 80:
        # Name the specific missing items (max 2)
        if missing_items:
            first = missing_items[0]
            cat_name = CAT_SHORT[first.cat]
            if len(missing_items) == 1:
                msg = (
                    f"📋 You're {pct}% done — just *{first.label.lower()}* left to add.\n"
                    f"Send: _{first.example}_"
                )
            else:
                second = missing_items[1]
                msg = (
                    f"📋 You're {pct}% done ({n_cats}/10 sections). "
                    f"Just your *{CAT_SHORT[first.cat].lower()}* and "
                    f"*{CAT_SHORT[second.cat].lower()}* left to add.\n"
                    f"Send /binder to see exactly what's missing."
                )
        else:
            msg = f"📋 You're {pct}% done. Send /sos to generate your emergency file."
    else:
        msg = (
            f"📋 Your emergency file is {pct}% complete ({n_cats}/10 sections). "
            f"Send /binder to see what's still missing."
        )

    try:
        _send_proactive_message(to=from_number, body=msg)
        record_nudge_sent(family_id, pct)
        logger.info("Binder nudge sent to %s at %d%%", from_number, pct)
    except Exception as exc:
        logger.warning("maybe_send_nudge: send failed: %s", exc)
