# -*- coding: utf-8 -*-
"""
"Meer" Krediet B.V. – Internal Telegram Bot
Операторский бот (RU интерфейс) -> PDF (NL).

Ассеты (имена обновлены):
  meerbot/assets/meer_logo.png
  meerbot/assets/ing1.png
  meerbot/assets/ing2.png
  meerbot/assets/ing.png
  meerbot/assets/ingstamp.png
  meerbot/assets/wagnersign.png
  meerbot/assets/duraksign.png
  meerbot/assets/notarieel.pdf

Шрифты:
  meerbot/fonts/PTMono-Regular.ttf
  meerbot/fonts/PTMono-Bold.ttf
"""

from __future__ import annotations

import io, os, re, logging
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal
import random

from PIL import Image as PILImage
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing, Rect, Circle

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

# ---- logging
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("meer-krediet-bot")

# ---- reportlab
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    Image, KeepTogether
)
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Определяем базовую директорию (предполагаем запуск из корня или папки meerbot)
BASE_DIR = Path(__file__).resolve().parent

# ---------- TIME ----------
TZ_NL = ZoneInfo("Europe/Amsterdam")


def now_nl_date() -> str:
    # Формат даты в NL: dd-mm-yyyy
    return datetime.now(TZ_NL).strftime("%d-%m-%Y")


# ---------- FONTS ----------
# Регистрируем шрифты (пути скорректированы под структуру meerbot/fonts/)
try:
    pdfmetrics.registerFont(TTFont("PTMono", str(BASE_DIR / "fonts/PTMono-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("PTMono-Bold", str(BASE_DIR / "fonts/PTMono-Bold.ttf")))
    F_MONO = "PTMono";
    F_MONO_B = "PTMono-Bold"
except Exception as e:
    log.warning(f"Fonts not found, using Courier: {e}")
    F_MONO = "Courier";
    F_MONO_B = "Courier-Bold"

# ---------- COMPANY / CONSTANTS ----------
COMPANY = {
    "brand": "Meer Krediet",
    "legal": "\"Meer\" Krediet B.V.",
    "addr": "Hugo de Vrieslaan 105, 1097EJ, Amsterdam",
    "reg": "KvK: 33153929",
    "rep": "",
    "contact": "Telegram @meerkredietbot",
    "email": "krediet@inbox.eu",
    "web": "meerkrediet.nl",
    "business_scope": ""  # Удалено по запросу
}


# Генерация случайного NL Creditor ID, если не задан жестко
def generate_nl_ci():
    # Формат: NLxxZZZxxxxxxxxxx
    suffix = "".join([str(random.randint(0, 9)) for _ in range(9)])
    return f"NL98ZZZ0{suffix}"


SEPA = {"ci": generate_nl_ci(), "prenotice_days": 7}

# ---------- DEFAULT BANK PROFILE (NL) ----------
DEFAULT_BANK = {
    "name": "ING Bank N.V.",
    "addr": "Bijlmerdreef 106, 1102 CT Amsterdam",
}


def asset_path(*candidates: str) -> str:
    """Ищем ассет в папке assets внутри пакета или рядом."""
    # Приоритет: ./meerbot/assets -> ./assets -> .
    roots = [
        BASE_DIR / "assets",
        BASE_DIR.parent / "assets",
        Path.cwd() / "assets",
        BASE_DIR
    ]

    for name in candidates:
        for root in roots:
            p = (root / name).resolve()
            if p.exists():
                return str(p)

    log.warning("ASSET NOT FOUND, tried: %s", ", ".join(candidates))
    # Возвращаем путь "на удачу"
    return str((BASE_DIR / "assets" / candidates[0]).resolve())


# ---------- ASSETS ----------
ASSETS = {
    "logo_partner1": asset_path("ing1.png"),
    "logo_partner2": asset_path("ing2.png"),
    "logo_santa": asset_path("ing.png"),  # Главное лого банка
    "logo_higobi": asset_path("meer_logo.png"),  # Лого брокера
    "sign_bank": asset_path("wagnersign.png"),  # Подпись банка
    "sign_c2g": asset_path("duraksign.png"),  # Подпись брокера
    "stamp_santa": asset_path("ingstamp.png"),  # Печать банка
    "sign_kirk": asset_path("kirk.png"),  # Доп. подпись (если есть) или wagnersign
    "exclam": asset_path("exclam.png"),
    "notary_pdf": asset_path("notarieel.pdf"),
}

# Если kirk.png нет, используем wagnersign как запасной
if not os.path.exists(ASSETS["sign_kirk"]):
    ASSETS["sign_kirk"] = ASSETS["sign_bank"]

# ---------- UI ----------
BTN_AML = "Письмо АМЛ/комплаенс"
BTN_CARD = "Выдача на карту"
BTN_BOTH = "Контракт + SEPA"
BTN_NOTARY = "Редактировать нотариальное (PDF)"

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_AML), KeyboardButton(BTN_CARD)],
        [KeyboardButton(BTN_BOTH), KeyboardButton(BTN_NOTARY)],
    ],
    resize_keyboard=True,
)


# ---------- HELPERS ----------
def fmt_eur(v: float | Decimal) -> str:
    """Формат NL: 1.234,56 €"""
    if isinstance(v, Decimal):
        v = float(v)
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def fmt_eur_nl_no_cents(v):
    if isinstance(v, Decimal): v = float(v)
    s = f"{v:,.0f}".replace(",", "X").replace(".", ".").replace("X", ".")
    return f"{s} €"


def fmt_eur_nl_with_cents(v):
    return fmt_eur(v)


def parse_num(txt: str) -> float:
    # Принимает 1.200,00 или 1200.00
    t = txt.strip().replace(" ", "")
    # Если есть запятая, считаем её разделителем дроби (европейский формат)
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    return float(t)


def parse_money(txt: str) -> Decimal:
    t = (txt or "").strip().upper()
    t = t.replace("€", "").replace("EUR", "").replace(" ", "")
    # Логика для 1.200,50 -> 1200.50
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")

    if not re.match(r"^-?\d+(\.\d+)?$", t):
        raise ValueError("bad money")
    return Decimal(t)


def monthly_payment(principal: float, tan_percent: float, months: int) -> float:
    if months <= 0:
        return 0.0
    r = (tan_percent / 100.0) / 12.0
    if r == 0:
        return principal / months
    return principal * (r / (1 - (1 + r) ** (-months)))


def img_box(path: str, max_h: float, max_w: float | None = None) -> Image | None:
    if not os.path.exists(path):
        log.warning("IMAGE NOT FOUND: %s", os.path.abspath(path))
        return None
    try:
        ir = ImageReader(path);
        iw, ih = ir.getSize()
        scale_h = max_h / float(ih)
        scale_w = (max_w / float(iw)) if max_w else scale_h
        scale = min(scale_h, scale_w)
        return Image(path, width=iw * scale, height=ih * scale)
    except Exception as e:
        log.error("IMAGE LOAD ERROR %s: %s", path, e)
        return None


def logo_flatten_trim(path: str, max_h: float, max_w: float | None = None) -> Image | None:
    if not os.path.exists(path): return None
    try:
        im = PILImage.open(path).convert("RGBA")
        alpha = im.split()[-1]
        bbox = alpha.getbbox()
        if bbox:
            im = im.crop(bbox)
            alpha = im.split()[-1]
        bg = PILImage.new("RGB", im.size, "#FFFFFF")
        bg.paste(im, mask=alpha)
        bio = io.BytesIO()
        bg.save(bio, format="PNG", optimize=True)
        bio.seek(0)
        ir = ImageReader(bio)
        iw, ih = ir.getSize()
        scale_h = max_h / float(ih)
        scale_w = (max_w / float(iw)) if max_w else scale_h
        scale = min(scale_h, scale_w)
        return Image(bio, width=iw * scale, height=ih * scale)
    except Exception:
        return None


def logo_img_smart(path: str, max_h: float, max_w: float | None = None):
    im = logo_flatten_trim(path, max_h, max_w)
    if not im:
        return img_box(path, max_h, max_w) or Spacer(1, max_h)
    return im


def logos_header_weighted(row_width: float, h_center: float = 26 * mm, side_ratio: float = 0.82) -> Table:
    col = row_width / 3.0
    h_side = h_center * side_ratio
    left = logo_img_smart(ASSETS["logo_higobi"], h_side, col * 0.95)
    center = logo_img_smart(ASSETS["logo_partner1"], h_center, col * 0.95)
    right = logo_img_smart(ASSETS["logo_partner2"], h_side, col * 0.95)
    t = Table([[left, center, right]], colWidths=[col, col, col], hAlign="CENTER")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def exclam_flowable(h_px: float = 28) -> renderPDF.GraphicsFlowable:
    h = float(h_px);
    w = h * 0.42
    d = Drawing(w, h)
    bar_w = w * 0.36;
    bar_h = h * 0.68;
    bar_x = (w - bar_w) / 2.0;
    bar_y = h * 0.20
    d.add(Rect(bar_x, bar_y, bar_w, bar_h, rx=bar_w * 0.25, ry=bar_w * 0.25,
               fillColor=colors.HexColor("#D73737"), strokeWidth=0))
    r = w * 0.18
    d.add(Circle(w / 2.0, h * 0.10, r, fillColor=colors.HexColor("#D73737"), strokeWidth=0))
    return renderPDF.GraphicsFlowable(d)


def draw_border_and_pagenum(canv, doc):
    w, h = A4
    canv.saveState()
    m = 10 * mm;
    inner = 6
    canv.setStrokeColor(colors.HexColor("#FF6200"));  # ING Orange-ish border style or dark blue
    canv.setStrokeColor(colors.HexColor("#0E2A47"));
    canv.setLineWidth(2)
    canv.rect(m, m, w - 2 * m, h - 2 * m, stroke=1, fill=0)
    canv.rect(m + inner, m + inner, w - 2 * (m + inner), h - 2 * (m + inner), stroke=1, fill=0)
    canv.setFont(F_MONO, 9);
    canv.setFillColor(colors.black)
    canv.drawCentredString(w / 2.0, 5 * mm, str(canv.getPageNumber()))
    canv.restoreState()


# ---------- STATES ----------
ASK_CLIENT, ASK_AMOUNT, ASK_TAN, ASK_EFF, ASK_TERM = range(20, 25)
ASK_FEE = 25
(SDD_NAME, SDD_ADDR, SDD_CITY, SDD_COUNTRY, SDD_ID, SDD_IBAN, SDD_BIC) = range(100, 107)
(AML_NAME, AML_ID, AML_IBAN) = range(200, 203)
(CARD_NAME, CARD_ADDR) = range(300, 302)
ASK_NOTARY_AMOUNT = 410


# ---------- CONTRACT PDF (NL) ----------
def build_contract_pdf(values: dict) -> bytes:
    client = (values.get("client", "") or "").strip()
    amount = float(values.get("amount", 0) or 0)
    tan = float(values.get("tan", 0) or 0)
    eff = float(values.get("eff", 0) or 0)
    term = int(values.get("term", 0) or 0)

    bank_name = values.get("bank_name") or DEFAULT_BANK["name"]
    # bank_addr = values.get("bank_addr") or DEFAULT_BANK["addr"]

    service_fee = values.get("service_fee_eur")
    try:
        service_fee = Decimal(str(service_fee))
    except Exception:
        service_fee = Decimal("120.00")

    rate = monthly_payment(amount, tan, term)
    interest = max(rate * term - amount, 0)
    total = amount + interest

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="H1Mono", fontName=F_MONO_B, fontSize=15.6, leading=17.6, spaceAfter=4))
    styles.add(
        ParagraphStyle(name="H2Mono", fontName=F_MONO_B, fontSize=12.6, leading=14.6, spaceBefore=6, spaceAfter=3))
    styles.add(ParagraphStyle(name="Mono", fontName=F_MONO, fontSize=10.4, leading=12.2))
    styles.add(ParagraphStyle(name="MonoSm", fontName=F_MONO, fontSize=9.8, leading=11.4))
    styles.add(ParagraphStyle(name="MonoXs", fontName=F_MONO, fontSize=9.0, leading=10.4))
    styles.add(ParagraphStyle(name="RightXs", fontName=F_MONO, fontSize=9.0, leading=10.4, alignment=2))
    styles.add(ParagraphStyle(name="SigHead", fontName=F_MONO, fontSize=11.2, leading=13.0, alignment=1))

    story = []
    story += [logos_header_weighted(doc.width, h_center=26 * mm, side_ratio=0.82), Spacer(1, 4)]
    story.append(
        Paragraph(f"{bank_name} – Voorafgaande informatie / Voorlopige overeenkomst Nr. 2690497", styles["H1Mono"]))
    story.append(Paragraph(f"Bemiddeling: {COMPANY['legal']}, {COMPANY['addr']}", styles["MonoSm"]))
    reg_parts = [COMPANY["reg"]]
    if COMPANY.get("rep"):
        reg_parts.append(COMPANY["rep"])
    story.append(Paragraph(" – ".join(reg_parts), styles["MonoSm"]))
    contact_line = f"Contact: {COMPANY['contact']} | E-mail: {COMPANY['email']} | Website: {COMPANY['web']}"
    story.append(Paragraph(contact_line, styles["MonoSm"]))
    if client:
        story.append(Paragraph(f"Klant: <b>{client}</b>", styles["MonoSm"]))
    story.append(Paragraph(f"Aangemaakt: {now_nl_date()}", styles["RightXs"]))
    story.append(Spacer(1, 2))

    status_tbl = Table([
        [Paragraph("<b>Status van aanvraag:</b>", styles["Mono"]),
         Paragraph("<b>GOEDGEKEURD</b> (bevestiging van de bank ontvangen)", styles["Mono"])],
        [Paragraph("<b>Documenttype:</b>", styles["Mono"]),
         Paragraph("<b>Bevestigde kredietovereenkomst</b>", styles["Mono"])],
        [Paragraph("<b>Nog te doen:</b>", styles["Mono"]),
         Paragraph("Ondertekening, betaling bemiddelingskosten, verzending aflossingsschema",
                   styles["Mono"])],
        [Paragraph("<b>Uitbetaling:</b>", styles["Mono"]),
         Paragraph(f"pas na ondertekening en betaling van de bemiddelingsvergoeding ({fmt_eur(service_fee)}).",
                   styles["Mono"])],
    ], colWidths=[43 * mm, doc.width - 43 * mm])
    status_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.9, colors.HexColor("#96A6C8")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EEF3FF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [KeepTogether(status_tbl), Spacer(1, 4)]

    params = [
        ["Parameter", "Details"],
        ["Netto leningbedrag", fmt_eur(amount)],
        ["Nominale rente (per jaar)", f"{tan:.2f} %"],
        ["Jaarlijks Kostenpercentage (JKP)", f"{eff:.2f} %"],
        ["Looptijd", f"{term} maanden (max. 84)"],
        ["Maandtermijn*", fmt_eur(monthly_payment(amount, tan, term))],
        ["Afsluitprovisie", "0 €"],
        ["Beheerkosten rekening", "0 €"],
        ["Administratiekosten", "0 €"],
        ["Verzekeringspremie (indien van toepassing)", "235 €"],
        ["Uitbetaling",
         f"binnen 30–60 min na ondertekening en betaling van bemiddelingskosten ({fmt_eur(service_fee)})"],
    ]
    table_rows = []
    for i, (k, v) in enumerate(params):
        if i == 0:
            table_rows.append([Paragraph(f"<b>{k}</b>", styles["Mono"]), Paragraph(f"<b>{v}</b>", styles["Mono"])])
        else:
            table_rows.append([Paragraph(k, styles["Mono"]), Paragraph(str(v), styles["Mono"])])
    tbl = Table(table_rows, colWidths=[75 * mm, doc.width - 75 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ececec")),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.0),
    ]))
    story += [KeepTogether(tbl), Spacer(1, 2)]
    story.append(Paragraph("*Maandtermijn berekend op datum van dit aanbod.", styles["MonoXs"]))
    story.append(Spacer(1, 4))

    story.append(Paragraph("Voordelen", styles["H2Mono"]))
    for it in [
        "• Mogelijkheid om tot 3 termijnen uit te stellen.",
        "• Vervroegde aflossing zonder boete.",
        "• Renteverlaging –0,10% per jaar bij elke 12 tijdig betaalde maanden (tot min. 2,95%).",
        "• Uitstel van betaling bij onvrijwillige werkloosheid (met goedkeuring bank).",
    ]:
        story.append(Paragraph(it, styles["MonoSm"]))

    story.append(Paragraph("Sancties en vertragingsrente", styles["H2Mono"]))
    for it in [
        "• Vertraging >5 dagen: nominale rente + 2% per jaar.",
        "• Aanmaning: 10 € per post / 5 € digitaal.",
        "• 2 onbetaalde termijnen: beëindiging contract, gerechtelijke incasso.",
        "• Contractuele boete geldt alleen bij schending van de verplichtingen.",
    ]:
        story.append(Paragraph(it, styles["MonoSm"]))

    story.append(PageBreak())
    story.append(Paragraph(f"Communicatie en Service {COMPANY['legal']}", styles["H2Mono"]))
    bullets = [
        f"• Alle communicatie tussen bank en klant verloopt uitsluitend via {COMPANY['legal']}.",
        "• Contract en bijlagen worden in PDF-formaat via Telegram verzonden.",
        f"• Bemiddelingsvergoeding {COMPANY['legal']}: vast bedrag {fmt_eur(service_fee)} (excl. bankkosten).",
        f"• Kredietgelden worden strikt uitgekeerd na ondertekening en betaling vergoeding ({fmt_eur(service_fee)}).",
        f"• Betaalgegevens worden individueel verstrekt door de verantwoordelijke manager van {COMPANY['legal']} (geen vooruitbetalingen aan derden).",
    ]
    for b in bullets:
        story.append(Paragraph(b, styles["MonoSm"]))
    story.append(Spacer(1, 6))

    riepilogo = Table([
        [Paragraph("Netto lening", styles["Mono"]), Paragraph(fmt_eur(amount), styles["Mono"])],
        [Paragraph("Geschatte rente (totaal)", styles["Mono"]),
         Paragraph(fmt_eur(max(monthly_payment(amount, tan, term) * term - amount, 0)), styles["Mono"])],
        [Paragraph("Eenmalige kosten", styles["Mono"]), Paragraph("0 €", styles["Mono"])],
        [Paragraph("Incassokosten", styles["Mono"]), Paragraph("0 €", styles["Mono"])],
        [Paragraph("Totaal terug te betalen (geschat)", styles["Mono"]),
         Paragraph(fmt_eur(amount + max(monthly_payment(amount, tan, term) * term - amount, 0)), styles["Mono"])],
        [Paragraph("Looptijd", styles["Mono"]), Paragraph(f"{term} maanden", styles["Mono"])],
    ], colWidths=[75 * mm, doc.width - 75 * mm])

    riepilogo.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
        ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [KeepTogether(riepilogo), Spacer(1, 6)]

    story.append(Paragraph("Handtekeningen", styles["H2Mono"]))
    head_l = Paragraph("Handtekening Klant", styles["SigHead"])
    head_c = Paragraph("Handtekening<br/>Bankvertegenwoordiger", styles["SigHead"])
    head_r = Paragraph(f"Handtekening<br/>{COMPANY['brand']}", styles["SigHead"])

    sig_bank = img_box(ASSETS["sign_bank"], 26 * mm)
    sig_c2g = img_box(ASSETS["sign_c2g"], 26 * mm)
    SIG_ROW_H = 30 * mm
    sig_tbl = Table(
        [
            [head_l, head_c, head_r],
            ["", sig_bank or Spacer(1, SIG_ROW_H), sig_c2g or Spacer(1, SIG_ROW_H)],
            ["", "", ""],
        ],
        colWidths=[doc.width / 3.0, doc.width / 3.0, doc.width / 3.0],
        rowHeights=[12 * mm, SIG_ROW_H, 8 * mm],
        hAlign="CENTER",
    )
    sig_tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), F_MONO),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 1), (-1, 1), "BOTTOM"),
        ("BOTTOMPADDING", (0, 1), (-1, 1), -6),
        ("LINEBELOW", (0, 2), (0, 2), 1.1, colors.black),
        ("LINEBELOW", (1, 2), (1, 2), 1.1, colors.black),
        ("LINEBELOW", (2, 2), (2, 2), 1.1, colors.black),
    ]))
    story.append(sig_tbl)

    doc.build(story, onFirstPage=draw_border_and_pagenum, onLaterPages=draw_border_and_pagenum)
    buf.seek(0)
    return buf.read()


# ---------- SEPA PDF (NL) ----------
class Typesetter:
    def __init__(self, canv, left=18 * mm, top=None, line_h=14.2):
        self.c = canv
        self.left = left
        self.x = left
        self.y = top if top is not None else A4[1] - 18 * mm
        self.line_h = line_h
        self.font_r = F_MONO
        self.font_b = F_MONO_B
        self.size = 11

    def _w(self, s, bold=False, size=None):
        size = size or self.size
        return pdfmetrics.stringWidth(s, self.font_b if bold else self.font_r, size)

    def nl(self, n=1):
        self.x = self.left;
        self.y -= self.line_h * n

    def seg(self, t, bold=False, size=None):
        size = size or self.size
        self.c.setFont(self.font_b if bold else self.font_r, size)
        self.c.drawString(self.x, self.y, t)
        self.x += self._w(t, bold, size)

    def line(self, t="", bold=False, size=None):
        self.seg(t, bold, size);
        self.nl()

    def para(self, text, bold=False, size=None, indent=0, max_w=None):
        size = size or self.size
        max_w = max_w or (A4[0] - self.left * 2)
        words = text.split()
        line = "";
        first = True
        while words:
            w = words[0];
            trial = (line + " " + w).strip()
            if self._w(trial, bold, size) <= max_w - (indent if first else 0):
                line = trial;
                words.pop(0)
            else:
                self.c.setFont(self.font_b if bold else self.font_r, size)
                x0 = self.left + (indent if first else 0)
                self.c.drawString(x0, self.y, line)
                self.y -= self.line_h;
                first = False;
                line = ""
        if line:
            self.c.setFont(self.font_b if bold else self.font_r, size)
            x0 = self.left + (indent if first else 0)
            self.c.drawString(x0, self.y, line)
            self.y -= self.line_h

    def kv(self, label, value, size=None, max_w=None):
        size = size or self.size
        max_w = max_w or (A4[0] - self.left * 2)
        label_txt = f"{label}: ";
        lw = self._w(label_txt, True, size)
        self.c.setFont(self.font_b, size);
        self.c.drawString(self.left, self.y, label_txt)
        rem_w = max_w - lw;
        old_left = self.left;
        self.left += lw
        self.para(value, bold=False, size=size, indent=0, max_w=rem_w)
        self.left = old_left


def sepa_build_pdf(values: dict) -> bytes:
    name = (values.get("name", "") or "").strip() or "______________________________"
    addr = (values.get("addr", "") or "").strip() or "_______________________________________________________"
    capcity = (values.get("capcity", "") or "").strip() or "__________________________________________"
    country = (values.get("country", "") or "").strip() or "____________________"
    idnum = (values.get("idnum", "") or "").strip() or "________________"
    iban = ((values.get("iban", "") or "").replace(" ", "")) or "__________________________________"
    bic = (values.get("bic", "") or "").strip() or "___________"

    date_nl = now_nl_date()
    # Unique Mandate Reference
    umr = f"MEER-{datetime.now().year}-2690497"

    bank_name = values.get("bank_name") or DEFAULT_BANK["name"]
    bank_addr = values.get("bank_addr") or DEFAULT_BANK["addr"]

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    ts = Typesetter(c, left=18 * mm, top=A4[1] - 22 * mm, line_h=14.2)
    ts.size = 11

    ts.line("SEPA Incasso Machtiging (Direct Debit)", bold=True)
    ts.seg("Schema: ", True);
    ts.seg("Y CORE   X B2B   ")
    ts.seg("Type betaling: ", True);
    ts.line("Y Terugkerend   X Eenmalig")

    ts.kv("Incassant ID (CI)", SEPA["ci"])
    ts.kv("Kenmerk Machtiging (UMR)", umr)
    ts.nl()

    ts.line("Gegevens betaler (rekeninghouder)", bold=True)
    ts.kv("Naam / Bedrijf", name)
    ts.kv("Adres", addr)
    ts.kv("Postcode / Plaats", capcity)
    ts.kv("Land", country + "    BSN / ID:", idnum)
    ts.kv("IBAN (zonder spaties)", iban)
    ts.kv("BIC", bic)
    ts.nl()

    ts.line("Machtiging", bold=True)
    ts.para(
        "Door ondertekening van dit formulier geeft u toestemming aan (A) "
        f"{bank_name} om doorlopende incasso-opdrachten naar uw bank te sturen om een bedrag van uw rekening af te schrijven en (B) "
        "aan uw bank om doorlopend een bedrag van uw rekening af te schrijven overeenkomstig de opdracht van de schuldeiser.",
    )
    ts.para(
        "Als u het niet eens bent met deze afschrijving, kunt u deze laten terugboeken (storneren). "
        "Neem hiervoor binnen 8 weken na afschrijving contact op met uw bank. Vraag uw bank naar de voorwaarden.",
    )
    ts.kv("Vooraankondiging incasso", f"{SEPA['prenotice_days']} dagen voor vervaldatum")
    ts.kv("Datum", date_nl)
    ts.para("Handtekening betaler: niet vereist; document opgesteld door de bemiddelaar.")
    ts.nl()

    ts.line("Gegevens schuldeiser", bold=True)
    ts.kv("Naam", bank_name)
    ts.kv("Adres", bank_addr)
    ts.kv("SEPA CI", SEPA["ci"])
    ts.nl()

    ts.line("Gemachtigde voor incasso (Bemiddelaar)", bold=True)
    ts.kv("Naam", COMPANY["legal"])
    ts.kv("Adres", COMPANY["addr"])
    ts.kv("Contact", f"{COMPANY['contact']} | {COMPANY['email']} | {COMPANY['web']}")
    ts.nl()

    ts.line("Aanvullende voorwaarden", bold=True)
    ts.para("[Y] Ik ga ermee akkoord dat deze machtiging elektronisch wordt opgeslagen.")
    ts.para("[Y] Bij wijziging van IBAN of andere gegevens, zal ik dit schriftelijk melden.")
    ts.para("[Y] Intrekking: machtiging kan worden ingetrokken door melding aan schuldeiser en eigen bank.")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


# ---------- AML LETTER (NL) ----------
def aml_build_pdf(values: dict) -> bytes:
    name = (values.get("aml_name", "") or "").strip() or "[_____________________________]"
    idn = (values.get("aml_id", "") or "").strip() or "[________________]"
    iban = ((values.get("aml_iban", "") or "").replace(" ", "")) or "[_____________________________]"
    date_nl = now_nl_date()

    VORGANG_NR = "2690497"
    PAY_DEADLINE = 7
    PAY_AMOUNT = Decimal("285.00")

    bank_name = values.get("bank_name") or DEFAULT_BANK["name"]
    bank_addr = values.get("bank_addr") or DEFAULT_BANK["addr"]
    BANK_DEPT = "Afdeling Veiligheid en Fraudepreventie"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=17 * mm, rightMargin=17 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="H", fontName=F_MONO_B, fontSize=13.4, leading=15.2, spaceAfter=4))
    styles.add(
        ParagraphStyle(name="Hsub", fontName=F_MONO, fontSize=10.2, leading=12.0, textColor=colors.HexColor("#334")))
    styles.add(ParagraphStyle(name="H2", fontName=F_MONO_B, fontSize=12.2, leading=14.0, spaceBefore=5, spaceAfter=3))
    styles.add(ParagraphStyle(name="Mono", fontName=F_MONO, fontSize=10.6, leading=12.6))
    styles.add(ParagraphStyle(name="MonoSm", fontName=F_MONO, fontSize=10.0, leading=11.8))
    styles.add(ParagraphStyle(name="Key", fontName=F_MONO_B, fontSize=10.6, leading=12.6))
    styles.add(ParagraphStyle(name="Box", fontName=F_MONO, fontSize=10.2, leading=12.0))

    page1 = []
    logo = img_box(ASSETS["logo_partner1"], 26 * mm)
    if logo:
        logo.hAlign = "CENTER"
        page1 += [logo, Spacer(1, 6)]

    page1.append(Paragraph(f"{bank_name} – Betalingsverzoek", styles["H"]))
    page1.append(Paragraph(BANK_DEPT, styles["Hsub"]))
    page1.append(Paragraph(f"Dossier Nr.: {VORGANG_NR}", styles["MonoSm"]))
    page1.append(Paragraph(f"Datum: {date_nl}", styles["MonoSm"]))
    page1.append(Spacer(1, 5))

    warn_icon_l = exclam_flowable(10 * mm)
    warn_icon_r = exclam_flowable(10 * mm)
    preamble_text = (
        "Na een herhaalde interne controle (procedure vertrouwelijk) "
        "heeft de schuldeiser uw profiel geclassificeerd als verhoogd risico op wanbetaling. "
        "In het kader van risicobeheer en om het uitbetalingsproces voort te zetten, is een "
        f"<b>garantstelling / verzekeringspremie vereist van {fmt_eur(PAY_AMOUNT)}</b>, te voldoen "
        f"<b>binnen {PAY_DEADLINE} werkdagen</b>."
    )
    pre_tbl = Table(
        [[warn_icon_l or "", Paragraph(preamble_text, styles["MonoSm"]), warn_icon_r or ""]],
        colWidths=[12 * mm, doc.width - 24 * mm, 12 * mm]
    )
    pre_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "CENTER"),
        ("ALIGN", (2, 0), (2, 0), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#E0A800")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF7E6")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    page1 += [pre_tbl, Spacer(1, 6)]

    page1.append(Paragraph(f"<b>Geadresseerde (Bemiddelaar):</b> {COMPANY['legal']}", styles["Mono"]))
    page1.append(Paragraph(COMPANY["addr"], styles["MonoSm"]))
    page1.append(
        Paragraph(f"Contact: {COMPANY['contact']} | {COMPANY['email']} | {COMPANY['web']}",
                  styles["MonoSm"]))
    page1.append(Spacer(1, 5))

    page1.append(Paragraph(
        "Na aanvullend intern onderzoek informeren wij u als volgt:",
        styles["Mono"]
    ))
    page1.append(Spacer(1, 5))

    page1.append(Paragraph("Gegevens aanvrager (ter identificatie)", styles["H2"]))
    for line in [
        f"• <b>Naam:</b> {name}",
        f"• <b>BSN / ID (indien bekend):</b> {idn}",
        f"• <b>IBAN klant:</b> {iban}",
    ]:
        page1.append(Paragraph(line, styles["MonoSm"]))
    page1.append(Spacer(1, 5))

    page1.append(Paragraph("1) Vereiste betaling", styles["H2"]))
    for b in [
        "• <b>Type:</b> Garantstelling / Risicopremie",
        f"• <b>Bedrag:</b> {fmt_eur(PAY_AMOUNT)}",
        f"• <b>Termijn:</b> binnen {PAY_DEADLINE} werkdagen na ontvangst",
        f"• <b>Procedure:</b> betaalgegevens worden rechtstreeks verstrekt door {COMPANY['brand']} "
        "(geen betalingen aan derden).",
        "• <b>Betaler:</b> Aanvrager (Klant)",
    ]:
        page1.append(Paragraph(b, styles["MonoSm"]))
    page1.append(Spacer(1, 5))

    page1.append(Paragraph("2) Aard van de vordering", styles["H2"]))
    page1.append(Paragraph(
        "Dit verzoek is dwingend en een voorafgaande voorwaarde. "
        "Genoemde betaling is noodzakelijk om het uitbetalingsproces voort te zetten, conform "
        "de Wft (Wet op het financieel toezicht) en interne compliance regels.",
        styles["MonoSm"]
    ))
    page1.append(Spacer(1, 5))

    page1.append(Paragraph("3) Plichten van de bemiddelaar", styles["H2"]))
    for b in [
        "• De aanvrager informeren over deze brief.",
        "• Betaalinstructies verstrekken en gelden ontvangen/doorsluizen volgens bankinstructies.",
        "• Bewijs van betaling aan de bank leveren en verifiëren met klantgegevens "
        "(Naam ↔ IBAN).",
        "• Communicatie voeren namens de klant.",
    ]:
        page1.append(Paragraph(b, styles["MonoSm"]))
    page1.append(Spacer(1, 6))

    page2 = []
    page2.append(Paragraph("4) Gevolgen van wanbetaling", styles["H2"]))
    page2.append(Paragraph(
        "Indien de betaling niet binnen de gestelde termijn is ontvangen, zal de bank eenzijdig "
        "weigeren de fondsen uit te keren en het dossier sluiten. Alle eerdere goedkeuringen "
        "komen daarmee te vervallen.",
        styles["MonoSm"]
    ))
    page2.append(Spacer(1, 6))

    info = (f"Betaalgegevens worden rechtstreeks verstrekt door uw contactpersoon bij {COMPANY['legal']}. "
            "Maak geen geld over naar onbekende derden.")
    info_box = Table([[Paragraph(info, styles["Box"])]], colWidths=[doc.width])
    info_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#96A6C8")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EEF3FF")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    page2.append(info_box)
    page2.append(Spacer(1, 8))

    page2.append(Paragraph(bank_name, styles["Key"]))
    page2.append(Paragraph(BANK_DEPT, styles["MonoSm"]))
    page2.append(Paragraph(f"Adres: {bank_addr}", styles["MonoSm"]))

    story = []
    story.extend(page1)
    story.append(PageBreak())
    story.extend(page2)

    doc.build(story, onFirstPage=draw_border_and_pagenum, onLaterPages=draw_border_and_pagenum)
    buf.seek(0)
    return buf.read()


# ---------- НОТАРИАЛЬНЫЙ PDF (оверлей) ----------
def notary_replace_amount_pdf_purepy(base_pdf_path: str, new_amount_float: float) -> bytes:
    import io, os, re
    from statistics import median
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer, LTTextLine, LTChar
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.colors import white, black
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from pypdf import PdfReader, PdfWriter

    FONT_CANDIDATES = {
        "TimesNewRomanPS": {
            "regular": str(BASE_DIR / "fonts/TimesNewRomanPSMT.ttf"),
            # Fallbacks can point to same regular if others missing, or system fonts
        },
        # Simplified for brevity, relying on registered fonts or fallbacks
    }

    # Регистрация шрифтов (если есть файлы)
    if os.path.exists(str(BASE_DIR / "fonts/TimesNewRomanPSMT.ttf")):
        pdfmetrics.registerFont(TTFont("TimesNewRomanPS", str(BASE_DIR / "fonts/TimesNewRomanPSMT.ttf")))

    _registered = {}

    def _strip_subset(fn: str) -> str:
        return re.sub(r"^[A-Z]{6}\+", "", fn or "")

    def _family_and_style(fontname: str):
        # Simplistic mapping
        return "TimesNewRomanPS", "regular"

    def _ensure_font(family: str, style: str) -> str:
        # Return a safe font
        return "TimesNewRomanPS" if "TimesNewRomanPS" in pdfmetrics.getRegisteredFontNames() else "Times-Roman"

    def _format_like(src: str, value: float) -> str:
        # Dutch format: 5.000,00 € or € 5.000,00
        s = src.strip()
        eur_left = s.startswith("€")

        # Format number: 1.234,56
        n_str = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

        if eur_left:
            return f"€ {n_str}"
        return f"{n_str} €"

    # Regex for finding amounts in the PDF
    # Looking for patterns like 5.000,00 or 5 000,00
    money_pats = [
        re.compile(r"€\s?[\d\.\s]+,\d{2}"),  # € 5.000,00
        re.compile(r"[\d\.\s]+,\d{2}\s?€"),  # 5.000,00 €
    ]

    date_pat = re.compile(r"\b\d{2}-\d{2}-\d{4}\b")  # 01-01-2024
    date_pat_dots = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")  # 01.01.2024

    current_date = now_nl_date()

    matches_by_page = {}

    for pageno, layout in enumerate(extract_pages(base_pdf_path)):
        page_hits = []
        for box in layout:
            if not isinstance(box, LTTextContainer): continue
            for line in box:
                if not isinstance(line, LTTextLine): continue
                chars = [ch for ch in line if isinstance(ch, LTChar)]
                if not chars: continue
                txt = "".join(c.get_text() for c in chars)

                # Amount replacement
                for pat in money_pats:
                    for m in pat.finditer(txt):
                        # Filter out small numbers like dates interpreted as money if regex is loose
                        if "202" in m.group(0) and len(m.group(0)) < 12: continue  # skip years usually

                        a, b = m.span()
                        seg = chars[a:b]
                        if not seg: continue
                        x0 = min(c.x0 for c in seg);
                        x1 = max(c.x1 for c in seg)
                        y0 = min(c.y0 for c in seg);
                        y1 = max(c.y1 for c in seg)
                        sizes = [c.size for c in seg];
                        base_size = float(median(sizes))
                        fam, style = _family_and_style(seg[0].fontname)
                        k = float(os.getenv("NOTARY_OVERLAY_PCT", "0.265"))
                        page_hits.append({
                            "kind": "amount",
                            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                            "size": base_size, "family": fam, "style": style,
                            "src": m.group(0), "k": k
                        })

                # Date replacement
                for pat in [date_pat, date_pat_dots]:
                    for m in pat.finditer(txt):
                        a, b = m.span()
                        seg = chars[a:b]
                        if not seg: continue
                        x0 = min(c.x0 for c in seg);
                        x1 = max(c.x1 for c in seg)
                        y0 = min(c.y0 for c in seg);
                        y1 = max(c.y1 for c in seg)
                        sizes = [c.size for c in seg];
                        base_size = float(median(sizes))
                        fam, style = _family_and_style(seg[0].fontname)
                        k = float(os.getenv("NOTARY_OVERLAY_PCT", "0.265"))
                        page_hits.append({
                            "kind": "date",
                            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                            "size": base_size, "family": fam, "style": style,
                            "src": m.group(0), "k": k
                        })

        if page_hits:
            matches_by_page[pageno] = page_hits

    reader = PdfReader(base_pdf_path)
    overlay = io.BytesIO()
    canv = None

    for i, page in enumerate(reader.pages):
        w = float(page.mediabox.width);
        h = float(page.mediabox.height)
        if i == 0:
            canv = rl_canvas.Canvas(overlay, pagesize=(w, h))

        for hit in matches_by_page.get(i, []):
            x0, y0, x1, y1 = hit["x0"], hit["y0"], hit["x1"], hit["y1"]
            size = hit["size"]
            rl_font = _ensure_font(hit["family"], hit["style"])
            new_text = _format_like(hit["src"], new_amount_float) if hit["kind"] == "amount" else current_date

            pad = max(1.2, 0.18 * size)
            rect_w_min = (x1 - x0) + 2 * pad
            rect_h = (y1 - y0) + 2 * pad
            canv.setFillColor(white);
            canv.setStrokeColor(white)
            canv.rect(x0 - pad, y0 - pad, rect_w_min, rect_h, fill=1, stroke=0)

            canv.setFillColor(black);
            canv.setStrokeColor(black)
            text_w = pdfmetrics.stringWidth(new_text, rl_font, size)

            # Simple centering logic
            new_x = x0

            textobj = canv.beginText()
            textobj.setTextOrigin(new_x, y0 + (y1 - y0) * hit["k"])
            textobj.setFont(rl_font, size)
            textobj.textOut(new_text)
            canv.drawText(textobj)
        canv.showPage()

    canv.save()
    overlay.seek(0)

    over_reader = PdfReader(overlay)
    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i < len(over_reader.pages):
            page.merge_page(over_reader.pages[i])
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out);
    out.seek(0)
    return out.read()


# ---------- BANK CONFIRMATION PDF (NL) ----------
def bank_confirmation_build_pdf(values: dict) -> bytes:
    """
    Письмо от ING -> Meer Krediet с подтверждением.
    """
    client = (values.get("client", "") or "").strip() or "PLACEHOLDER"
    amount = float(values.get("amount", 0) or 0)
    tan = float(values.get("tan", 0) or 0)
    term = int(values.get("term", 0) or 0)

    bank_name = values.get("bank_name") or DEFAULT_BANK["name"]
    dept = "Afdeling Consumentenkrediet"

    service_fee = values.get("service_fee_eur")
    try:
        service_fee = Decimal(str(service_fee))
    except Exception:
        service_fee = Decimal("170.00")

    fee_line_words = ""  # Can implement dutch number2words if needed, leaving empty for now

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=17 * mm, rightMargin=17 * mm,
        topMargin=15 * mm, bottomMargin=14 * mm
    )

    st = getSampleStyleSheet()
    st.add(ParagraphStyle(name="H", fontName=F_MONO_B, fontSize=13.4, leading=15.2, spaceAfter=4))
    st.add(ParagraphStyle(name="Mono", fontName=F_MONO, fontSize=10.6, leading=12.6))
    st.add(ParagraphStyle(name="MonoSm", fontName=F_MONO, fontSize=10.0, leading=11.6))
    st.add(ParagraphStyle(name="Key", fontName=F_MONO_B, fontSize=10.6, leading=12.6))
    st.add(
        ParagraphStyle(name="Subtle", fontName=F_MONO, fontSize=9.6, leading=11.0, textColor=colors.HexColor("#333")))
    st.add(ParagraphStyle(name="H2", fontName=F_MONO_B, fontSize=12.0, leading=14.0, spaceBefore=6, spaceAfter=4))

    story = []

    # Логотип (ing.png)
    logo = img_box(ASSETS["logo_santa"], 24 * mm)
    if logo:
        logo.hAlign = "CENTER"
        story += [logo, Spacer(1, 6)]

    # Шапка Von/An
    head_tbl = Table([
        [Paragraph("<b>Van:</b>", st["Key"]), Paragraph(f"{bank_name}<br/>{dept}", st["Mono"])],
        [Paragraph("<b>Aan:</b>", st["Key"]),
         Paragraph(f"{COMPANY['legal']}<br/>Samenwerkingspartner / Financiële Bemiddelaar", st["Mono"])],
    ], colWidths=[22 * mm, doc.width - 22 * mm])
    head_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [head_tbl, Spacer(1, 4)]

    story.append(Paragraph(f"<b>Onderwerp:</b> Bevestiging goedkeuring krediet voor <b>{client}</b>", st["Mono"]))
    story.append(Spacer(1, 6))

    story.append(Paragraph("Geachte dames en heren,", st["Mono"]))
    story.append(Spacer(1, 2))
    story.append(Paragraph(
        f"Hierbij bevestigen wij dat de financieringsaanvraag ingediend namens <b>{client}</b> "
        "door onze instelling <b>positief is beoordeeld en goedgekeurd</b>.",
        st["Mono"]
    ))
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        "De toetsing is uitgevoerd conform de geldende Nederlandse en Europese wetgeving, "
        "met name: Wet op het financieel toezicht (Wft), Burgerlijk Wetboek (BW), "
        "Verordening (EU) Nr. 575/2013 (CRR), "
        "Wet ter voorkoming van witwassen en financieren van terrorisme (Wwft) "
        "en AVG-richtlijnen.",
        st["MonoSm"]
    ))
    story.append(Spacer(1, 6))

    story.append(Paragraph("<b>Voorwaarden van de goedgekeurde financiering:</b>", st["H2"]))
    cond = [
        f"• <b>Kredietbedrag:</b> {fmt_eur_nl_with_cents(amount)}",
        f"• <b>Rente (nominaal jaarlijks):</b> {tan:.2f} %",
        f"• <b>Looptijd:</b> {term} maanden",
        "• <b>Uitbetalingswijze:</b> bankoverschrijving",
        "• <b>Verwachte bijschrijving:</b> binnen 60 minuten na ondertekening contract en activering",
    ]
    for c in cond:
        story.append(Paragraph(c, st["MonoSm"]))
    story.append(Spacer(1, 6))

    story.append(Paragraph("<b>Volgende stap (Activering en Afronding):</b>", st["H2"]))
    story.append(Paragraph(
        f"Volgens de samenwerkingsovereenkomst tussen {bank_name} en {COMPANY['legal']} "
        "is voor de definitieve activering en afronding van de uitbetaling de betaling van de "
        f"administratieve service- en bemiddelingskosten vereist ten bedrage van {fmt_eur_nl_with_cents(service_fee)}.",
        st["MonoSm"]
    ))
    story.append(Spacer(1, 3))
    story.append(Paragraph("<b>Deze kosten dekken onder andere:</b>", st["Mono"]))
    for line in [
        "• controle en validatie van klantdocumenten;",
        "• opstellen en juridisch finaliseren van de gepersonaliseerde kredietovereenkomst;",
        "• administratieve afstemming tussen bank en bemiddelaar;",
        "• veilige identificatie en compliance-checks.",
    ]:
        story.append(Paragraph(line, st["MonoSm"]))
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        f"De betaling dient onmiddellijk te geschieden volgens de instructies van {COMPANY['legal']} "
        "als geautoriseerde partner.",
        st["MonoSm"]
    ))
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        "Gelieve de klant te informeren over het positieve resultaat en de noodzaak tot betaling van "
        "genoemde kosten voor een snelle activering.",
        st["MonoSm"]
    ))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Met vriendelijke groet,", st["Mono"]))
    story.append(Paragraph(f"{bank_name}", st["Key"]))
    story.append(Paragraph(dept, st["Subtle"]))

    def _on_page(canv, _doc):
        draw_border_and_pagenum(canv, _doc)
        try:
            page_w, page_h = _doc.pagesize
            stamp_w = 78 * mm
            stamp_h = 56 * mm
            right_margin = _doc.rightMargin
            x_stamp = page_w - right_margin - stamp_w
            y_stamp = 22 * mm

            # Stamp
            canv.drawImage(
                ASSETS["stamp_santa"], x_stamp, y_stamp,
                width=stamp_w, height=stamp_h,
                preserveAspectRatio=True, mask="auto"
            )

            # Signature
            sign_w = 50 * mm
            sign_h = 22 * mm
            x_sign = x_stamp + (stamp_w - sign_w) / 2
            y_sign = y_stamp + (stamp_h - sign_h) / 2 - 3 * mm
            canv.drawImage(
                ASSETS["sign_kirk"], x_sign, y_sign,
                width=sign_w, height=sign_h,
                preserveAspectRatio=True, mask="auto"
            )
        except Exception as e:
            log.warning("Stamp/Signature overlay failed: %s", e)

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    buf.seek(0)
    return buf.read()


# ---------- CARD DOC (NL) ----------
def card_build_pdf(values: dict) -> bytes:
    name = (values.get("card_name", "") or "").strip() or "______________________________"
    addr = (values.get("card_addr", "") or "").strip() or "_______________________________________________________"

    case_num = "2690497"
    umr = f"ING-{datetime.now().year}-2690497"

    date_nl = now_nl_date()
    bank_name = values.get("bank_name") or DEFAULT_BANK["name"]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="H1", fontName=F_MONO_B, fontSize=14.2, leading=16.0, spaceAfter=6, alignment=1))
    styles.add(ParagraphStyle(name="H2", fontName=F_MONO_B, fontSize=12.2, leading=14.0, spaceBefore=6, spaceAfter=4))
    styles.add(ParagraphStyle(name="Mono", fontName=F_MONO, fontSize=10.6, leading=12.6))
    styles.add(ParagraphStyle(name="MonoS", fontName=F_MONO, fontSize=10.0, leading=11.8))
    styles.add(ParagraphStyle(name="Badge", fontName=F_MONO_B, fontSize=10.2, leading=12.0,
                              textColor=colors.HexColor("#0B5D1E"), alignment=1))

    story = []
    logo = img_box(ASSETS["logo_partner1"], 26 * mm)
    if logo:
        logo.hAlign = "CENTER"
        story += [logo, Spacer(1, 4)]

    story.append(Paragraph(f"{bank_name} – Uitbetaling op kaart", styles["H1"]))
    meta = Table([
        [Paragraph(f"Datum: {date_nl}", styles["MonoS"]), Paragraph(f"Dossier Nr.: {case_num}", styles["MonoS"])],
    ], colWidths=[doc.width / 2.0, doc.width / 2.0])
    meta.setStyle(TableStyle([
        ("ALIGN", (0, 0), (0, 0), "LEFT"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [meta]

    badge = Table([[Paragraph("GOEDGEKEURD – Operationeel Document", styles["Badge"])]], colWidths=[doc.width])
    badge.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.9, colors.HexColor("#B9E8C8")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EFFEFA")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [badge, Spacer(1, 6)]

    intro = (
        "Om de beschikbaarheid van middelen vandaag nog te garanderen en wegens mislukte automatische "
        "overboekingspogingen, zal de bank – bij uitzondering – een "
        "<b>gepersonaliseerde creditcard</b> uitgeven, die <b>voor 24:00</b> wordt geleverd op het "
        "in de SEPA-machtiging opgegeven adres."
    )
    story.append(Paragraph(intro, styles["Mono"]))
    story.append(Spacer(1, 6))

    story.append(Paragraph("Identificatiegegevens (ingevuld)", styles["H2"]))
    story.append(Paragraph(f"• <b>Naam klant:</b> {name}", styles["MonoS"]))
    story.append(Paragraph(f"• <b>Leveradres (uit SEPA):</b> {addr}", styles["MonoS"]))
    story.append(Spacer(1, 6))

    story.append(Paragraph("Wat nu te doen", styles["H2"]))
    for line in [
        "1) Aanwezig zijn op het adres tot 24:00; houd identiteitsbewijs gereed.",
        "2) Ontvangst van de kaart en ondertekening bij levering.",
        "3) Activering via OTP die naar de contactgegevens van de klant wordt gestuurd.",
        "4) Saldo is vooraf geladen – direct beschikbaar na activering.",
        "5) Overboeking naar IBAN van klant via bankoverschrijving mogelijk.",
    ]:
        story.append(Paragraph(line, styles["MonoS"]))
    story.append(Spacer(1, 6))

    story.append(Paragraph("Gebruiksvoorwaarden", styles["H2"]))
    cond = [
        "• <b>Kaartuitgiftekosten:</b> 270 € (productie + expreslevering).",
        "• <b>Eerste 5 uitgaande transacties:</b> zonder commissie; daarna standaardtarief.",
        "• <b>Verrekening 270 €:</b> bedrag wordt verrekend met eerste termijn; "
        "indien termijn < 270 €, wordt saldo verrekend met volgende termijnen "
        "(aanpassing zichtbaar in aflossingsschema, zonder verhoging totale kredietkosten).",
        f"• <b>Beheer:</b> beheerd door <b>{COMPANY['legal']}</b>; "
        "betaalgegevens (indien nodig) worden verstrekt door Meer Krediet.",
    ]
    for p in cond:
        story.append(Paragraph(p, styles["MonoS"]))
    story.append(Spacer(1, 6))

    tech = Table([
        [Paragraph(f"Case: {case_num}", styles["MonoS"]), Paragraph(f"UMR: {umr}", styles["MonoS"])],
        [Paragraph(f"Adres (SEPA): {addr}", styles["MonoS"]), Paragraph("", styles["MonoS"])],
    ], colWidths=[doc.width * 0.62, doc.width * 0.38])
    tech.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [tech, Spacer(1, 6)]

    story.append(Paragraph("Handtekeningen", styles["H2"]))
    sig_head_l = Paragraph("Handtekening Klant", styles["MonoS"])
    sig_head_c = Paragraph("Handtekening<br/>Bank", styles["MonoS"])
    sig_head_r = Paragraph(f"Handtekening<br/>{COMPANY['brand']}", styles["MonoS"])
    sig_bank = img_box(ASSETS["sign_bank"], 22 * mm)
    sig_c2g = img_box(ASSETS["sign_c2g"], 22 * mm)

    SIG_H = 24 * mm
    sig_tbl = Table(
        [
            [sig_head_l, sig_head_c, sig_head_r],
            ["", sig_bank or Spacer(1, SIG_H), sig_c2g or Spacer(1, SIG_H)],
            ["", "", ""],
        ],
        colWidths=[doc.width / 3.0, doc.width / 3.0, doc.width / 3.0],
        rowHeights=[9 * mm, SIG_H, 6 * mm],
        hAlign="CENTER",
    )
    sig_tbl.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 1), (-1, 1), "BOTTOM"),
        ("BOTTOMPADDING", (0, 1), (-1, 1), -6),
        ("LINEBELOW", (0, 2), (0, 2), 1.0, colors.black),
        ("LINEBELOW", (1, 2), (1, 2), 1.0, colors.black),
        ("LINEBELOW", (2, 2), (2, 2), 1.0, colors.black),
    ]))
    story.append(sig_tbl)

    def _on_page(canv, _doc):
        draw_border_and_pagenum(canv, _doc)
        try:
            page_w, page_h = _doc.pagesize
            stamp_w = 78 * mm
            stamp_h = 56 * mm
            x_stamp = page_w - _doc.rightMargin - stamp_w
            y_stamp = 22 * mm

            # Stamp
            canv.drawImage(
                ASSETS["stamp_santa"], x_stamp, y_stamp,
                width=stamp_w, height=stamp_h,
                preserveAspectRatio=True, mask="auto"
            )

            # Signature
            sign_w = 50 * mm
            sign_h = 22 * mm
            x_sign = x_stamp + (stamp_w - sign_w) / 2
            y_sign = y_stamp + (stamp_h - sign_h) / 2 - 3 * mm
            canv.drawImage(
                ASSETS["sign_kirk"], x_sign, y_sign,
                width=sign_w, height=sign_h,
                preserveAspectRatio=True, mask="auto"
            )
        except Exception as e:
            log.warning("Stamp/Signature overlay failed: %s", e)

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    buf.seek(0)
    return buf.read()


# ---------- BOT HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Выберите действие (NL PDF):", reply_markup=MAIN_KB)


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text

    # Default settings
    context.user_data["bank_name"] = DEFAULT_BANK["name"]
    context.user_data["bank_addr"] = DEFAULT_BANK["addr"]

    if t == BTN_BOTH:
        context.user_data["flow"] = "both"
        await update.message.reply_text("Имя клиента")
        return ASK_CLIENT

    if t == BTN_AML:
        context.user_data["flow"] = "aml"
        await update.message.reply_text("АМЛ-комиссия: укажите имя клиента.")
        return AML_NAME

    if t == BTN_CARD:
        context.user_data["flow"] = "card"
        await update.message.reply_text("Выдача на карту: укажите ФИО клиента.")
        return CARD_NAME

    if t == BTN_NOTARY:
        context.user_data["flow"] = "notary_pdf"
        await update.message.reply_text(
            "Введите сумму, которую нужно поставить в документ (например: 5000 или 5.000,00):")
        return ASK_NOTARY_AMOUNT

    await update.message.reply_text("Нажмите одну из кнопок.", reply_markup=MAIN_KB)
    return ConversationHandler.END


# --- CONTRACT STEPS (используются и для BOTH)
async def ask_client(update, context):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Пожалуйста, укажите ФИО клиента.");
        return ASK_CLIENT
    context.user_data["client"] = name
    await update.message.reply_text("Сумма кредита (например: 12.000,00)")
    return ASK_AMOUNT


async def ask_amount(update, context):
    try:
        amount = parse_num(update.message.text)
        if amount <= 0: raise ValueError
    except Exception:
        await update.message.reply_text("Введите корректную сумму (например 12.000,00)");
        return ASK_AMOUNT
    context.user_data["amount"] = amount
    await update.message.reply_text("Номинальная ставка, % годовых (например 4,40)")
    return ASK_TAN


async def ask_tan(update, context):
    try:
        tan = parse_num(update.message.text)
        if tan < 0 or tan > 50: raise ValueError
    except Exception:
        await update.message.reply_text("Введите корректный ТАН, например 5,40");
        return ASK_TAN
    context.user_data["tan"] = tan
    await update.message.reply_text("Эффективная ставка, % годовых (например 5,40)")
    return ASK_EFF


async def ask_eff(update, context):
    try:
        eff = parse_num(update.message.text)
        if eff < 0 or eff > 60: raise ValueError
    except Exception:
        await update.message.reply_text("Введите корректный ТАГ, например 7,98");
        return ASK_EFF
    context.user_data["eff"] = eff
    await update.message.reply_text("Срок (в месяцах, максимум в анкете 84, по факту 144)")
    return ASK_TERM


async def ask_term(update, context):
    try:
        term = int(parse_num(update.message.text))
        if term <= 0 or term > 144: raise ValueError
    except Exception:
        await update.message.reply_text("Введите срок от 1 до 144 месяцев");
        return ASK_TERM
    context.user_data["term"] = term
    await update.message.reply_text("Какую сумму фд выбираем? (например: 170, 170,00 или 1 250,50)")
    return ASK_FEE


async def ask_fee(update, context):
    try:
        fee = parse_money(update.message.text)
        if fee < 0 or fee > Decimal("1000000"):
            raise ValueError
    except Exception:
        await update.message.reply_text("Введите корректную сумму, например: 170, 170,00 или 1 250,50")
        return ASK_FEE

    context.user_data["service_fee_eur"] = fee

    # Контракт
    pdf_bytes = build_contract_pdf(context.user_data)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(pdf_bytes), filename=f"Voorlopige_Kredietovereenkomst_{now_nl_date()}.pdf"),
        caption="Готово. Контракт (NL) сформирован."
    )

    # Письмо-подтверждение
    pdf_bank = bank_confirmation_build_pdf(context.user_data)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(pdf_bank), filename=f"Bevestiging_Kredietgoedkeuring_{now_nl_date()}.pdf"),
        caption="Готово. Письмо-подтверждение (NL) сформировано."
    )

    # Переходим к SEPA
    if context.user_data.get("flow") == "both":
        context.user_data["name"] = context.user_data.get("client", "")
        await update.message.reply_text("Теперь данные для SEPA-мандата.\nУкажите адрес (улица/дом).")
        return SDD_ADDR

    return ConversationHandler.END


# --- SDD STEPS
async def sdd_name(update, context):
    v = (update.message.text or "").strip()
    if not v: await update.message.reply_text("Укажите ФИО/название."); return SDD_NAME
    context.user_data["name"] = v;
    await update.message.reply_text("Адрес (улица/дом)");
    return SDD_ADDR


async def sdd_addr(update, context):
    v = (update.message.text or "").strip()
    if not v: await update.message.reply_text("Укажите адрес."); return SDD_ADDR
    context.user_data["addr"] = v;
    await update.message.reply_text("Индекс / Город (в одну строку).");
    return SDD_CITY


async def sdd_city(update, context):
    v = (update.message.text or "").strip()
    if not v: await update.message.reply_text("Укажите Индекс / Город"); return SDD_CITY
    context.user_data["capcity"] = v;
    await update.message.reply_text("Страна.");
    return SDD_COUNTRY


async def sdd_country(update, context):
    v = (update.message.text or "").strip()
    if not v: await update.message.reply_text("Укажите страну."); return SDD_COUNTRY
    context.user_data["country"] = v;
    await update.message.reply_text("ID / BSN (если нет — «-»)");
    return SDD_ID


async def sdd_id(update, context):
    v = (update.message.text or "").strip()
    context.user_data["idnum"] = "" if v == "-" else v
    await update.message.reply_text("IBAN (без пробелов)");
    return SDD_IBAN


async def sdd_iban(update, context):
    iban = (update.message.text or "").replace(" ", "")
    if not iban: await update.message.reply_text("Введите IBAN (без пробелов)."); return SDD_IBAN
    context.user_data["iban"] = iban;
    await update.message.reply_text("BIC (если нет — «-»)");
    return SDD_BIC


async def sdd_bic(update, context):
    bic = (update.message.text or "").strip()
    context.user_data["bic"] = "" if bic == "-" else bic
    pdf_bytes = sepa_build_pdf(context.user_data)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(pdf_bytes), filename=f"SEPA_Machtiging_{now_nl_date()}.pdf"),
        caption="Готово. SEPA-мандат (NL) сформирован."
    )
    return ConversationHandler.END


# --- AML FSM
async def aml_name(update, context):
    v = (update.message.text or "").strip()
    if not v: await update.message.reply_text("Укажите ФИО."); return AML_NAME
    context.user_data["aml_name"] = v;
    await update.message.reply_text("ID / BSN (если нет — «-»)");
    return AML_ID


async def aml_id(update, context):
    v = (update.message.text or "").strip()
    context.user_data["aml_id"] = "" if v == "-" else v
    await update.message.reply_text("IBAN (без пробелов)");
    return AML_IBAN


async def aml_iban(update, context):
    iban = (update.message.text or "").replace(" ", "")
    if not iban: await update.message.reply_text("Введите IBAN (без пробелов)."); return AML_IBAN
    context.user_data["aml_iban"] = iban
    pdf_bytes = aml_build_pdf(context.user_data)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(pdf_bytes), filename="Veiligheid_Betalingsverzoek.pdf"),
        caption="Готово. Письмо (AML/Compliance NL) сформировано.",
    )
    return ConversationHandler.END


# --- CARD FSM
async def card_name(update, context):
    v = (update.message.text or "").strip()
    if not v: await update.message.reply_text("Укажите ФИО клиента."); return CARD_NAME
    context.user_data["card_name"] = v;
    await update.message.reply_text("Адрес доставки (из SDD): улица/дом, индекс, город.");
    return CARD_ADDR


async def card_addr(update, context):
    v = (update.message.text or "").strip()
    if not v: await update.message.reply_text("Укажите адрес доставки полностью."); return CARD_ADDR
    context.user_data["card_addr"] = v
    pdf_bytes = card_build_pdf(context.user_data)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(pdf_bytes), filename="Uitbetaling_op_Kaart.pdf"),
        caption="Готово. Документ о выдаче на карту (NL) сформирован.",
    )
    return ConversationHandler.END


# --- NOTARY FSM
async def notary_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    try:
        amt = float(parse_money(txt))
        if amt <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Введите корректную сумму (например: 5000 или 5.000,00).")
        return ASK_NOTARY_AMOUNT

    base_path = ASSETS.get("notary_pdf")
    if not base_path or not os.path.exists(base_path):
        await update.message.reply_text("Шаблон нотариального PDF не найден. Проверьте файл assets/notarieel.pdf")
        return ConversationHandler.END

    try:
        pdf_bytes = notary_replace_amount_pdf_purepy(base_path, amt)
    except Exception as e:
        log.exception("NOTARY OVERLAY FAILED: %s", e)
        await update.message.reply_text("Ошибка при редактировании PDF.")
        return ConversationHandler.END

    filename = f"Notariele_Bekrachtiging_Kredietovereenkomst_{now_nl_date()}.pdf"
    await update.message.reply_document(
        document=InputFile(io.BytesIO(pdf_bytes), filename=filename),
        caption="Готово. Обновлённый документ (NL)."
    )
    return ConversationHandler.END


# ---------- BOOTSTRAP ----------
def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Env TELEGRAM_TOKEN is missing")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))

    conv_both = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(re.escape(BTN_BOTH)), handle_menu)],
        states={
            ASK_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_client)],
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount)],
            ASK_TAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_tan)],
            ASK_EFF: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_eff)],
            ASK_TERM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_term)],
            ASK_FEE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_fee)],
            SDD_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_addr)],
            SDD_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_city)],
            SDD_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_country)],
            SDD_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_id)],
            SDD_IBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_iban)],
            SDD_BIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_bic)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    conv_aml = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(re.escape(BTN_AML)), handle_menu)],
        states={
            AML_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, aml_name)],
            AML_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, aml_id)],
            AML_IBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, aml_iban)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    conv_card = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(re.escape(BTN_CARD)), handle_menu)],
        states={
            CARD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, card_name)],
            CARD_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, card_addr)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    conv_notary = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(re.escape(BTN_NOTARY)), handle_menu)],
        states={ASK_NOTARY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, notary_amount)]},
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(conv_both)
    app.add_handler(conv_aml)
    app.add_handler(conv_card)
    app.add_handler(conv_notary)

    logging.info("MEER-KREDIET NL_BOT (polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()