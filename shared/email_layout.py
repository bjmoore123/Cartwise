"""shared/email_layout.py — the one look for every Cartwise email and email landing page (v9.11).

Follows the brand package (docs: Cartwise-brand-guide.pdf, colors.txt):
  Cartwise Green #1A6B3C (header, buttons, links) · Cart Amber #F5A524 (accent only, never text)
  Ink #1C1C1C (text) · Cream #FAF8F3 (page) · Mint #F0F7F3 (callouts)
  DM Serif Display for headings, Lato for body; Georgia / Arial where web fonts don't load.

Email rules this follows, so it looks the same in Gmail, Outlook, Apple Mail and phones:
  - table layout, inline styles, 560 px card that shrinks on phones
  - the logo is a hosted PNG ({site}/email-logo.png): Gmail and Outlook don't show SVG.
    Its alt text is white on green, so with images off the header still reads "Cartwise".
  - buttons are table cells (they work in Outlook) and never wrap; email addresses never
    break in the middle; footer links stay on one line each
  - hidden preheader = the preview line in the inbox
  - light color scheme declared, so dark-mode apps don't turn the green header muddy

Pure functions: no AWS, no Resend.
"""
from html import escape

GREEN, LEAF, AMBER = "#1a6b3c", "#2d8f55", "#f5a524"
INK, BODY, MUTED, LINE = "#1c1c1c", "#3d3d3d", "#7a7a7a", "#e8e4da"
CREAM, MINT, WHITE = "#faf8f3", "#f0f7f3", "#ffffff"
SERIF = "'DM Serif Display',Georgia,'Times New Roman',serif"
SANS = "Lato,'Helvetica Neue',Helvetica,Arial,sans-serif"
LOGO_W, LOGO_H = 134, 30            # email-logo.png is 430 x 96 (3x for sharp phones)
FONTS = ("https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=Lato:wght@400;700&display=swap")


def site_url(site: str = "") -> str:
    return (site or "https://www.cartwise.shopping").rstrip("/")


def site_label(site: str = "") -> str:
    return site_url(site).replace("https://", "").replace("http://", "")


ZWNJ = "&#8204;"   # zero-width non-joiner: invisible, and not a place where the line can break


def addr(email: str) -> str:
    """An email address in body text, shown as ONE link (brand green, underlined) to mailto:.

    Gmail, Outlook and Apple Mail auto-link anything that looks like an address or a domain,
    even inside a link we made, and style it their own way: Gmail turned just "gmail.com" blue.
    So the visible text gets an invisible zero-width non-joiner after the @ and before each dot
    of the domain: people see the same address, but it no longer matches what the auto-linkers
    look for, so our link (whose href is the real address) is the only one.

    The address never splits mid-word; on a narrow phone it may break once, before the @."""
    local, at, domain = (email or "").partition("@")
    if not at:
        return f'<strong style="color:{INK}">{escape(email)}</strong>'
    style = (f"color:{GREEN};text-decoration:underline;font-weight:700;")
    shown_domain = (ZWNJ + ".").join(escape(part) for part in domain.split("."))
    return (f'<a href="mailto:{escape(email)}" style="{style}">'
            f'<span style="white-space:nowrap;{style}">{escape(local)}</span><wbr>'
            f'<span style="white-space:nowrap;{style}">@{ZWNJ}{shown_domain}</span></a>')


def p(html: str, *, small: bool = False, muted: bool = False, last: bool = False) -> str:
    size, lh = ("14px", "22px") if small else ("16px", "25px")
    color = MUTED if muted else BODY
    return (f'<p style="margin:0 0 {0 if last else 16}px;font-family:{SANS};font-size:{size};'
            f'line-height:{lh};color:{color}">{html}</p>')


def button(label: str, url: str, *, color: str = GREEN) -> str:
    """Bulletproof button: a table cell with a link filling it. Never wraps."""
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 24px">'
            f'<tr><td align="center" bgcolor="{color}" style="border-radius:8px;background:{color}">'
            f'<a href="{escape(url)}" target="_blank" style="display:inline-block;padding:14px 30px;'
            f'font-family:{SANS};font-size:16px;font-weight:700;line-height:20px;color:{WHITE};'
            f'text-decoration:none;border-radius:8px;white-space:nowrap">{escape(label)}</a></td></tr></table>')


def callout(html: str) -> str:
    """A mint panel for the one thing to notice (an expiry, a warning)."""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:0 0 20px"><tr><td style="background:{MINT};border-left:3px solid {GREEN};'
            f'border-radius:6px;padding:12px 16px;font-family:{SANS};font-size:14px;line-height:21px;'
            f'color:{BODY}">{html}</td></tr></table>')


def _footer(unsub_url: str, reason: str, site: str) -> str:
    links = []
    if unsub_url:
        links.append(f'<a href="{escape(unsub_url)}" style="color:{MUTED};text-decoration:underline;'
                     f'white-space:nowrap">Unsubscribe from Cartwise emails</a>')
    links.append(f'<a href="{escape(site_url(site))}" style="color:{MUTED};text-decoration:none;'
                 f'white-space:nowrap">{escape(site_label(site))}</a>')
    why = (f'<div style="margin:0 0 6px">{escape(reason)}</div>' if reason else "")
    return (f'<tr><td align="center" style="padding:20px 8px 0;font-family:{SANS};font-size:12px;'
            f'line-height:18px;color:{MUTED}">{why}<div>{" &nbsp;· ".join(links)}</div></td></tr>')


def email(*, title: str, preheader: str, heading: str, body: str,
          unsub_url: str = "", reason: str = "", site: str = "",
          header_sub: str = "", wide: bool = False, raw_body: bool = False) -> str:
    """A whole branded email. `body` is HTML built with p(), button(), callout().
    raw_body=True puts `body` in the card without padding (the weekly deal table)."""
    width = 680 if wide else 560
    s = site_url(site)
    sub = (f'<div style="font-family:{SANS};font-size:14px;line-height:20px;color:#d7eadf;'
           f'padding-top:10px">{header_sub}</div>' if header_sub else "")
    head = (f'<h1 class="cw-h1" style="margin:0 0 16px;font-family:{SERIF};font-size:26px;line-height:32px;'
            f'font-weight:400;color:{INK}">{escape(heading)}</h1>' if heading else "")
    inner = body if raw_body else f'<div class="cw-body" style="padding:32px 36px 16px">{head}{body}</div>'
    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{escape(title)}</title>
<link href="{FONTS}" rel="stylesheet">
<style>
  :root {{ color-scheme: light; supported-color-schemes: light; }}
  body {{ margin:0; padding:0; -webkit-text-size-adjust:100%; }}
  a {{ color:{GREEN}; }}
  @media (max-width:600px) {{
    .cw-pad {{ padding:20px 10px 28px !important; }}
    .cw-body {{ padding:26px 22px 10px !important; }}
    .cw-head {{ padding:20px 22px !important; }}
    .cw-h1 {{ font-size:23px !important; line-height:29px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:{CREAM}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{CREAM};font-size:1px;line-height:1px">{escape(preheader)}&#8199;&#847;&#8199;&#847;&#8199;&#847;&#8199;&#847;&#8199;&#847;&#8199;&#847;&#8199;&#847;&#8199;&#847;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{CREAM}" style="background:{CREAM}">
<tr><td align="center" class="cw-pad" style="padding:32px 12px 40px">
  <table role="presentation" class="cw-card" width="{width}" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:{width}px">
    <tr><td bgcolor="{GREEN}" class="cw-head" style="background:{GREEN};border-radius:12px 12px 0 0;padding:22px 36px">
      <a href="{escape(s)}" target="_blank" style="text-decoration:none"><img src="{escape(s)}/email-logo.png" width="{LOGO_W}" height="{LOGO_H}" alt="Cartwise" style="display:block;border:0;outline:none;width:{LOGO_W}px;height:{LOGO_H}px;font-family:{SERIF};font-size:24px;line-height:30px;color:{WHITE}"></a>{sub}
    </td></tr>
    <tr><td bgcolor="{WHITE}" style="background:{WHITE};border:1px solid {LINE};border-top:0;border-radius:0 0 12px 12px">{inner}</td></tr>
    {_footer(unsub_url, reason, site)}
  </table>
</td></tr></table>
</body></html>"""


def text(*paragraphs: str, unsub_url: str = "", reason: str = "", site: str = "") -> str:
    """Plain-text part: paragraphs separated by blank lines, links on their own line, then the footer."""
    out = "\n\n".join(x.strip() for x in paragraphs if x and x.strip())
    foot = ["--"]
    if reason:
        foot.append(reason)
    if unsub_url:
        foot.append(f"Unsubscribe from Cartwise emails: {unsub_url}")
    foot.append(site_url(site))
    return out + "\n\n" + "\n".join(foot)


def landing_page(title: str, message_html: str, *, ok: bool = True, action_html: str = "",
                 site: str = "", status: int = 200, ask: bool = False) -> dict:
    """The pages people land on from an email link (verify, unsubscribe): same header and colors."""
    s = site_url(site)
    color = GREEN if ok else "#b3261e"
    glyph = "?" if ask else ("✓" if ok else "!")       # ask: a question, nothing has happened yet
    mark = (f'<div style="width:44px;height:44px;border-radius:22px;background:{MINT if ok else "#fdecea"};'
            f'color:{color};font:700 22px/44px {SANS};margin:0 auto 14px">{glyph}</div>')
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · Cartwise</title>
<link rel="icon" href="{escape(s)}/favicon.ico">
<link href="{FONTS}" rel="stylesheet">
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;background:{CREAM};font-family:{SANS};color:{BODY};
       display:flex;align-items:center;justify-content:center;padding:24px}}
  .card{{width:100%;max-width:460px;background:{WHITE};border:1px solid {LINE};border-radius:14px;
        overflow:hidden;box-shadow:0 6px 24px rgba(28,28,28,.06)}}
  .top{{background:{GREEN};padding:20px 28px}}
  .top img{{display:block;width:{LOGO_W}px;height:{LOGO_H}px}}
  .body{{padding:30px 28px 28px;text-align:center}}
  h1{{font-family:{SERIF};font-weight:400;font-size:26px;line-height:1.25;color:{INK};margin:0 0 10px}}
  p{{font-size:16px;line-height:1.55;margin:0 0 22px}}
  strong{{color:{INK}}}
  .btn{{display:inline-block;background:{GREEN};color:{WHITE};border:0;border-radius:8px;padding:13px 28px;
       font:700 16px/20px {SANS};text-decoration:none;cursor:pointer;white-space:nowrap}}
  .btn:hover{{background:{LEAF}}}
  .btn.danger{{background:#b3261e}} .btn.danger:hover{{background:#8f1d17}}
  .alt{{display:block;margin-top:14px;font-size:14px;color:{MUTED};text-decoration:none}}
  .alt:hover{{color:{GREEN};text-decoration:underline}}
  form{{margin:0}}
</style></head>
<body><div class="card">
  <div class="top"><a href="{escape(s)}"><img src="{escape(s)}/email-logo.png" alt="Cartwise"></a></div>
  <div class="body">{mark}<h1>{escape(title)}</h1><p>{message_html}</p>{action_html}</div>
</div></body></html>"""
    return {"statusCode": status, "headers": {"Content-Type": "text/html; charset=utf-8"}, "body": html}
