"""shared/email_render.py — Weekly deal email (v9.3).

Renders matches from shared.matcher using the normalized `offer` from
shared.deal_parser.classify_offer(), so the email shows exactly what the web
cards show: headline (what you pay, or the deal mechanic), context, save text.

Before v9.3 the email printed the raw Publix `savings` text in a "Price"
column. For price drops that text is the DISCOUNT ("$0.24"), for coupons it is
"Save $2.00", so discounts were presented as prices. Titles were not escaped.

Pure functions only (no AWS, no Resend) so it is unit-tested directly.
"""
from datetime import date
from html import escape

try:
    from shared.deal_parser import apply_bogo_mode
    from shared import email_layout as L
except ImportError:                                  # pragma: no cover
    from deal_parser import apply_bogo_mode
    import email_layout as L

GREEN, GREEN_BG = "#1a6b3c", "#e0efe7"
AMBER, AMBER_BG = "#8a4106", "#fdf0dc"
PURPLE, PURPLE_BG = "#6b21a8", "#f3e8fb"
BLUE, BLUE_BG = "#1e40af", "#e6edfb"
TEAL, TEAL_BG = "#0e6372", "#ddf1f4"     # v9.8.2: Multi-buy, so amber means BOGO only
RED, INK, MUTED, LINE = "#c0392b", "#1c1c1c", "#6b6b6b", "#ece8df"

# Same tag set and order as dealTypeKey() in frontend/app.js (v9.4 design):
# Sale = weekly ad price, price drop or $ off; the source feed is not a tag.
TAGS = {
    "sale":   ("Sale",   GREEN,  GREEN_BG),
    "bogo":   ("BOGO",   AMBER,  AMBER_BG),
    "multi":  ("Multi-buy", TEAL,   TEAL_BG),
    "coupon": ("Coupon", PURPLE, PURPLE_BG),
    "extra":  ("Extra",  BLUE,   BLUE_BG),
}


def deal_tag(m: dict) -> str:
    """One tag per deal. KEEP IN SYNC with dealTypeKey() in frontend/app.js."""
    o = m.get("offer") or {}
    st = (m.get("saving_type") or "").lower()
    stacked = st in ("stacked", "stackeddeals") or m.get("is_stacked")
    if o.get("kind") == "bogo" or m.get("is_bogo"):
        return "bogo"
    if (st == "digitalcoupon" or m.get("has_coupon")) and not stacked:
        return "coupon"
    if stacked or st == "extrasavings" or m.get("is_extra"):
        return "extra"
    if o.get("kind") == "multi":
        return "multi"
    return "sale"


def tag_label(m: dict, key: str) -> str:
    if key == "multi":
        h = (m.get("offer") or {}).get("headline", "")
        n = h.split(" for ")[0] if " for " in h else ""
        return f"{n} for" if n.isdigit() else "Multi-buy"
    return TAGS[key][0]


def headline_color(o: dict) -> str:
    """Green = what you pay; red = an amount off; ink = a mechanic (BOGO, % off)."""
    kind = o.get("kind")
    if kind in ("price", "multi"):
        return GREEN
    if kind in ("amount_off", "coupon", "free"):
        return RED
    return INK


def offer_lines(m: dict, bogo_mode: str = "two") -> dict:
    """headline / context / save for one match; falls back to raw text for pre-v9.3 data.
    v9.8: BOGO lines follow the user's store rule (half price on one, or buy two)."""
    o = apply_bogo_mode(m.get("offer") or {}, bogo_mode)
    if not o:
        return {"headline": m.get("savings", ""), "context": "", "save": m.get("save_line", ""),
                "color": INK, "extra": ""}
    save = o.get("save_text", "")
    if o.get("pct") and save:
        save += f" ({o['pct']}%)"
    return {"headline": o.get("headline", ""), "context": o.get("context", ""), "save": save,
            "color": headline_color(o), "extra": o.get("extra", "")}


MAX_ROWS = 30   # longer emails get clipped by Gmail (~102 KB); the CTA links to the full list


def _valid(m: dict):
    """(from, thru) when Publix sent real dates; TPR and coupons carry a "1/1" placeholder."""
    vf, vt = (m.get("valid_from") or "").strip(), (m.get("valid_thru") or "").strip()
    if not vf or not vt or vf in ("1/1", "01/01") or vt in ("1/1", "01/01"):
        return None
    return vf, vt


def _week(matches: list):
    return next((v for v in (_valid(m) for m in matches) if v), None)


def subject_line(matches: list, today: date | None = None) -> str:
    n = len(matches)
    today = today or date.today()
    wk = _week(matches)
    when = f"{wk[0]} – {wk[1]}" if wk else f"{today:%b} {today.day}"
    return f"{n} item{'s' if n != 1 else ''} on your list {'are' if n != 1 else 'is'} on sale ({when})"


def _row(m: dict, bogo_mode: str = "two") -> str:
    """One deal. v9.11: the name gets the full width and the price sits on its own line under
    it, so nothing gets squeezed into a narrow column on a phone (no media queries needed)."""
    lines = offer_lines(m, bogo_mode)
    tag_key = deal_tag(m)
    _, fg, bg = TAGS[tag_key]
    label = escape(tag_label(m, tag_key))
    img = m.get("image_url") or ""
    img_cell = (
        f'<img src="{escape(img)}" width="64" height="64" alt="" '
        f'style="display:block;width:64px;height:64px;object-fit:contain;border-radius:8px;background:#f4f2ec;border:0;">'
        if img.startswith("https://") else
        '<div style="width:64px;height:64px;border-radius:8px;background:#f4f2ec;"></div>'
    )
    nw = "white-space:nowrap;"
    gap = "display:inline-block;margin:0 10px 4px 0;vertical-align:middle;"
    price = (f'<span style="{nw}font-size:19px;line-height:24px;font-weight:700;color:{lines["color"]};">'
             f'{escape(lines["headline"])}</span>')
    if lines["context"]:
        price += f' <span style="font-size:13px;color:{MUTED};">{escape(lines["context"])}</span>'
    extra = (f'<div style="font-size:13px;font-weight:700;color:{PURPLE};margin-top:2px;">{escape(lines["extra"])}</div>'
             if lines["extra"] else "")
    # Tag, savings, dates, coupon: separate pieces, each kept whole, that wrap as units on a phone
    meta = (f'<span style="{gap}{nw}font-size:10px;line-height:16px;font-weight:900;letter-spacing:.06em;'
            f'text-transform:uppercase;padding:2px 7px;border-radius:4px;background:{bg};color:{fg};">{label}</span>')
    if lines["save"]:
        meta += f' <span style="{gap}{nw}font-size:13px;font-weight:700;color:{RED};">{escape(lines["save"])}</span>'
    if _valid(m):
        meta += (f' <span style="{gap}{nw}font-size:12px;color:#8a8a8a;">'
                 f'Valid {escape(_valid(m)[0])} – {escape(_valid(m)[1])}</span>')
    if m.get("coupon_id") and (m.get("has_coupon") or tag_key in ("coupon", "stacked")):
        url = f"https://www.publix.com/savings/digital-coupons?cid={escape(str(m['coupon_id']))}"
        meta += (f' <a href="{url}" style="{gap}{nw}font-size:12px;font-weight:700;color:{PURPLE};'
                 f'text-decoration:none;">✂️ Clip coupon →</a>')
    return f"""
      <tr>
        <td width="64" style="padding:16px 0 16px 24px;width:64px;vertical-align:top;border-bottom:1px solid {LINE};">{img_cell}</td>
        <td style="padding:14px 24px 16px 16px;vertical-align:top;border-bottom:1px solid {LINE};">
          <div style="font-size:10px;line-height:14px;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:{GREEN};">{escape(m.get('my_item', ''))}</div>
          <div style="font-size:15px;line-height:21px;font-weight:700;color:{INK};margin:2px 0 4px;">{escape(m.get('deal_name') or m.get('title', ''))}</div>
          <div style="line-height:24px;">{price}</div>{extra}
          <div style="margin-top:6px;line-height:20px;">{meta}</div>
        </td>
      </tr>"""


def build_html(matches, store_id, store_name, frontend_url="", unsub_url="", account_email="",
               bogo_mode: str = "two") -> str:
    """v9.11: on the shared brand layout (logo header, cream page, serif heading)."""
    counts: dict = {}
    for m in matches:
        k = deal_tag(m)
        counts[k] = counts.get(k, 0) + 1
    chips = "".join(
        f'<span style="display:inline-block;margin:0 6px 6px 0;font-size:11px;font-weight:900;letter-spacing:.05em;'
        f'text-transform:uppercase;padding:3px 8px;border-radius:4px;white-space:nowrap;'
        f'background:{TAGS[k][2]};color:{TAGS[k][1]};">{counts[k]} {TAGS[k][0]}</span>'
        for k in TAGS if counts.get(k)
    )
    wk = _week(matches)
    name = store_name or f"Store #{store_id}"
    store = escape(name if name.lower().startswith("publix") else f"Publix · {name}")
    valid = (f' <span style="white-space:nowrap">· Valid {escape(wk[0])} – {escape(wk[1])}</span>' if wk else "")
    n = len(matches)
    heading = f"{n} item{'s' if n != 1 else ''} on your list {'are' if n != 1 else 'is'} on sale"
    rows = "".join(_row(m, bogo_mode) for m in matches[:MAX_ROWS])
    if n > MAX_ROWS:
        rows += (f'<tr><td colspan="2" style="padding:12px 24px;font-size:13px;color:{MUTED};border-bottom:1px solid {LINE};">'
                 f'…and {n - MAX_ROWS} more on Cartwise.</td></tr>')
    cta_url = frontend_url or "https://www.cartwise.shopping"
    body = f"""<div style="padding:28px 24px 10px;font-family:{L.SANS};">
    <h1 class="cw-h1" style="margin:0 0 6px;font-family:{L.SERIF};font-size:26px;line-height:32px;font-weight:400;color:{INK};">{heading}</h1>
    <div style="font-size:14px;line-height:20px;color:{MUTED};margin:0 0 14px;">{store}{valid}</div>
    <div>{chips}</div>
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="font-family:{L.SANS};color:{INK};border-top:1px solid {LINE};">{rows}</table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td align="center" bgcolor="{L.MINT}" style="background:{L.MINT};padding:24px 24px 6px;border-radius:0 0 12px 12px;font-family:{L.SANS};">
      {L.button(f"See all {n} match{'es' if n != 1 else ''}", cta_url).replace('margin:8px 0 24px', 'margin:0 auto 12px')}
      <div style="font-size:13px;line-height:20px;padding-bottom:18px;"><a href="{escape(cta_url)}" style="color:{GREEN};font-weight:700;text-decoration:none;white-space:nowrap;">Edit my list</a></div>
    </td></tr></table>"""
    reason = "Sent because your weekly deal alerts are on."
    return L.email(title=heading, preheader=f"{heading} at {name}.", heading="", body=body, raw_body=True,
                   wide=True, unsub_url=unsub_url, reason=reason, site=frontend_url)


def build_text(matches, store_name="", unsub_url="", bogo_mode: str = "two", frontend_url: str = "") -> str:
    """Plain-text part (improves deliverability; some clients show only this)."""
    head = f"Cartwise: {len(matches)} item(s) on your list are on sale at {store_name or 'your Publix'}."
    rows = []
    for m in matches:
        lines = offer_lines(m, bogo_mode)
        bits = [lines["headline"], lines["context"], lines["save"], lines["extra"]]
        rows.append(f"- {m.get('deal_name') or m.get('title', '')}: " + " · ".join(b for b in bits if b))
    site = L.site_url(frontend_url)
    return L.text(head, "\n".join(rows), f"See all your matches and edit your list:\n{site}",
                  unsub_url=unsub_url, reason="Sent because your weekly deal alerts are on.", site=frontend_url)
