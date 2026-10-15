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
except ImportError:                                  # pragma: no cover
    from deal_parser import apply_bogo_mode

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
    lines = offer_lines(m, bogo_mode)
    tag_key = deal_tag(m)
    _, fg, bg = TAGS[tag_key]
    label = escape(tag_label(m, tag_key))
    img = m.get("image_url") or ""
    img_cell = (
        f'<img src="{escape(img)}" width="56" height="56" alt="" '
        f'style="display:block;width:56px;height:56px;object-fit:contain;border-radius:8px;background:#f4f2ec;">'
        if img.startswith("https://") else
        '<div style="width:56px;height:56px;border-radius:8px;background:#f4f2ec;"></div>'
    )
    clip = ""
    if m.get("coupon_id") and (m.get("has_coupon") or tag_key in ("coupon", "stacked")):
        url = f"https://www.publix.com/savings/digital-coupons?cid={escape(str(m['coupon_id']))}"
        clip = (f'<div style="margin-top:3px;"><a href="{url}" style="font-size:12px;font-weight:700;'
                f'color:{PURPLE};text-decoration:none;">✂️ Clip coupon →</a></div>')
    context = f'<span style="font-size:12px;color:{MUTED};">{escape(lines["context"])}</span>' if lines["context"] else ""
    extra = f'<div style="font-size:12px;font-weight:700;color:{PURPLE};">{escape(lines["extra"])}</div>' if lines["extra"] else ""
    valid = ""
    if _valid(m):
        valid = f'<div style="font-size:11px;color:#999;">Valid {escape(_valid(m)[0])} – {escape(_valid(m)[1])}</div>'
    return f"""
      <tr>
        <td style="padding:12px 0 12px 24px;width:56px;vertical-align:top;border-bottom:1px solid {LINE};">{img_cell}</td>
        <td style="padding:12px 14px;vertical-align:top;border-bottom:1px solid {LINE};">
          <div style="font-size:10px;font-weight:900;letter-spacing:.07em;text-transform:uppercase;color:{GREEN};">{escape(m.get('my_item', ''))}</div>
          <div style="font-size:14px;font-weight:700;color:{INK};margin:2px 0;">{escape(m.get('deal_name') or m.get('title', ''))}</div>
          <div><span style="font-size:9.5px;font-weight:900;letter-spacing:.06em;text-transform:uppercase;padding:2px 6px;border-radius:4px;background:{bg};color:{fg};">{label}</span> {context}</div>
          {valid}{clip}
        </td>
        <td style="padding:12px 24px 12px 0;text-align:right;vertical-align:top;white-space:nowrap;border-bottom:1px solid {LINE};">
          <div style="font-family:'DM Mono',Consolas,monospace;font-size:18px;font-weight:600;color:{lines['color']};">{escape(lines['headline'])}</div>
          {f'<div style="font-size:12px;font-weight:700;color:{RED};">{escape(lines["save"])}</div>' if lines['save'] else ''}
          {extra}
        </td>
      </tr>"""


def build_html(matches, store_id, store_name, frontend_url="", unsub_url="", account_email="",
               bogo_mode: str = "two") -> str:
    counts: dict = {}
    for m in matches:
        k = deal_tag(m)
        counts[k] = counts.get(k, 0) + 1
    chips = "".join(
        f'<span style="display:inline-block;margin:0 6px 4px 0;font-size:11px;font-weight:900;letter-spacing:.05em;'
        f'text-transform:uppercase;padding:3px 8px;border-radius:4px;background:{TAGS[k][2]};color:{TAGS[k][1]};">'
        f'{counts[k]} {TAGS[k][0]}</span>'
        for k in TAGS if counts.get(k)
    )
    wk = _week(matches)
    name = store_name or f"Store #{store_id}"
    store = escape(name if name.lower().startswith("publix") else f"Publix · {name}")
    valid = f" · Valid {escape(wk[0])} – {escape(wk[1])}" if wk else ""
    n = len(matches)
    rows = "".join(_row(m, bogo_mode) for m in matches[:MAX_ROWS])
    if n > MAX_ROWS:
        rows += (f'<tr><td colspan="3" style="padding:12px 24px;font-size:13px;color:{MUTED};border-bottom:1px solid {LINE};">'
                 f'…and {n - MAX_ROWS} more on Cartwise.</td></tr>')
    cta_url = escape(frontend_url or "https://cartwise.shopping")
    footer_to = f" for {escape(account_email)}" if account_email else ""
    unsub = (f'<a href="{escape(unsub_url)}" style="color:{MUTED};">Unsubscribe</a> · '
             if unsub_url else "")
    return f"""<!doctype html><html><body style="margin:0;padding:24px 0;background:#efece5;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;font-family:Lato,Arial,sans-serif;color:{INK};">
  <tr><td colspan="3" style="background:{GREEN};color:#ffffff;padding:22px 24px;">
    <div style="font-family:'DM Serif Display',Georgia,serif;font-size:24px;">Cartwise</div>
    <div style="font-size:14px;opacity:.9;margin-top:4px;">{n} item{'s' if n != 1 else ''} on your list {'are' if n != 1 else 'is'} on sale this week</div>
    <div style="font-size:12px;opacity:.75;margin-top:2px;">{store}{valid}</div>
  </td></tr>
  <tr><td colspan="3" style="padding:14px 24px 10px;border-bottom:1px solid {LINE};">{chips}</td></tr>
  {rows}
  <tr><td colspan="3" style="padding:20px 24px;background:#f0f7f3;text-align:center;">
    <a href="{cta_url}" style="display:inline-block;background:{GREEN};color:#ffffff;padding:11px 26px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px;">See all {n} match{'es' if n != 1 else ''}</a>
    <div style="font-size:11.5px;color:{MUTED};margin-top:12px;line-height:1.5;">You're getting this because weekly alerts are on{footer_to}.<br>{unsub}<a href="{cta_url}" style="color:{MUTED};">Edit my list</a></div>
  </td></tr>
</table></td></tr></table></body></html>"""


def build_text(matches, store_name="", unsub_url="", bogo_mode: str = "two") -> str:
    """Plain-text part (improves deliverability; some clients show only this)."""
    out = [f"Cartwise: {len(matches)} item(s) on your list are on sale at {store_name or 'your Publix'}.", ""]
    for m in matches:
        lines = offer_lines(m, bogo_mode)
        bits = [lines["headline"], lines["context"], lines["save"], lines["extra"]]
        out.append(f"- {m.get('deal_name') or m.get('title', '')}: " + " · ".join(b for b in bits if b))
    if unsub_url:
        out += ["", f"Unsubscribe: {unsub_url}"]
    return "\n".join(out)
