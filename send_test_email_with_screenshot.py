#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import time
import argparse
import html
import mimetypes
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Tuple

# email utils
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from email.utils import formataddr, make_msgid, getaddresses

# Selenium imports
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ---------------- Defaults ---------------- #

DEFAULT_URLS: List[str] = [
    "https://docs.newrelic.com/docs/apis/nerdgraph/examples/nerdgraph-cloud-integrations-api-tutorial/#list-enabled-provider-accounts",
]
DEFAULT_SELECTOR: str = "h1"
DEFAULT_EXPECTED_TEXT: str = "NerdGraph tutorial: Configure cloud integrations"
DEFAULT_TIMEOUT_MS: int = 15000
DEFAULT_OUT_DIR: str = "screenshots"
DEFAULT_WINDOW: str = "1366x768"
DEFAULT_HEADED: bool = False
DEFAULT_CHECK_SIZE: bool = False
DEFAULT_MIN_WIDTH: int = 10
DEFAULT_MIN_HEIGHT: int = 5

DEFAULT_EMAIL_TO: List[str] = "TO_EMAIL"
DEFAULT_EMAIL_CC: List[str] = []
DEFAULT_EMAIL_ATTACH_SCREENS: bool = True

# SMTP defaults
DEFAULT_SMTP_SERVER: str = "smtp.gmail.com"
DEFAULT_SMTP_PORT: int = 587  # STARTTLS
DEFAULT_SMTP_USE_SSL: bool = False  # Use STARTTLS by default
DEFAULT_SMTP_FROM_NAME: str = ""
DEFAULT_SMTP_PASS_ENV: str = "SMTP_PASSWORD"
DEFAULT_SMTP_USER_ENV: str = "SMTP_USERNAME"
DEFAULT_EMAIL_INLINE_IMAGES: bool = True  # Embed screenshots inline in HTML


# ---------------- Utilities ---------------- #

def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sanitize(url: str) -> str:
    return (
        url.replace("https://", "")
        .replace("http://", "")
        .replace("/", "_")
        .replace("?", "_")
        .replace(":", "_")
        .replace("&", "_")
        .replace("=", "_")
    )


def build_driver(headed: bool, window: str) -> webdriver.Chrome:
    """Build a Chrome driver using Selenium Manager (no webdriver-manager)."""
    w, h = (int(x) for x in window.lower().split("x"))

    options = Options()
    if not headed:
        # The "new" headless is preferred on recent Chrome
        try:
            options.add_argument("--headless=new")
        except Exception:
            options.add_argument("--headless")
    options.add_argument(f"--window-size={w},{h}")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--hide-scrollbars")

    service = Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    return driver


def validate_ui(
    driver: webdriver.Chrome,
    url: str,
    selector: str,
    expected_text: str,
    timeout_ms: int,
    check_size: bool,
    min_w: int,
    min_h: int,
):
    print(f"\n--- Visiting: {url} ---")
    driver.get(url)
    time.sleep(10) #wait time for accurate result
    wait = WebDriverWait(driver, timeout_ms / 1000.0)
    element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))

    if not element.is_displayed():
        raise AssertionError(f"Element {selector} is NOT visible on {url}")

    if check_size:
        size = element.size or {}
        if size.get("width", 0) <= min_w:
            raise AssertionError(
                f"Element width too small ({size.get('width', 0)}px) on {url} (min {min_w})"
            )
        if size.get("height", 0) <= min_h:
            raise AssertionError(
                f"Element height too small ({size.get('height', 0)}px) on {url} (min {min_h})"
            )
        print(f"Size OK on {url}: {size.get('width', 0)}x{size.get('height', 0)}")

    css = driver.execute_script(
        """
        const el = arguments[0];
        const c = window.getComputedStyle(el);
        return {
          display: c.display,
          visibility: c.visibility,
          opacity: c.opacity,
          color: c.color,
          backgroundColor: c.backgroundColor,
          fontSize: c.fontSize,
          position: c.position
        };
        """,
        element,
    ) or {}

    if css.get("display") == "none":
        raise AssertionError(f"Element is display:none on {url}")
    if css.get("visibility") == "hidden":
        raise AssertionError(f"Element is visibility:hidden on {url}")
    try:
        if float(css.get("opacity", "1")) == 0:
            raise AssertionError(f"Element opacity 0 (invisible) on {url}")
    except (ValueError, TypeError):
        pass

    print(f"CSS OK on {url}: {css}")

    text = (element.text or "").strip()
    if text != expected_text:
        raise AssertionError(
            f'Text mismatch on {url}. Expected "{expected_text}", got "{text}"'
        )
    print(f'Text OK on {url}: "{text}"')


def fullpage_screenshot(
    driver: webdriver.Chrome, out_path: Path, min_w: int, min_h: int
):
    width = driver.execute_script(
        "return Math.max(document.body.scrollWidth, document.documentElement.scrollWidth)"
    )
    height = driver.execute_script(
        "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
    )

    width = max(int(width or 0), min_w)
    height = max(int(height or 0), min_h)

    driver.set_window_size(width, height)
    driver.save_screenshot(str(out_path))


@dataclass
class UrlResult:
    url: str
    ok: bool
    message: str
    screenshot_path: Optional[Path] = None
    failure_screenshot_path: Optional[Path] = None


def parse_args() -> argparse.Namespace:
    """
    CLI is optional—defaults above are used when flags are omitted.
    """
    p = argparse.ArgumentParser(
        description=(
            "UI validation with Selenium Manager: visibility, CSS, exact text, screenshots.\n"
            "Includes Gmail SMTP email summary with robust attachment verification."
        )
    )
    p.add_argument("--urls", nargs="+", help="List of URLs to test")
    p.add_argument("--selector", help=f"CSS selector (default: {DEFAULT_SELECTOR})")
    p.add_argument(
        "--expected-text", help=f'Exact expected text (default: "{DEFAULT_EXPECTED_TEXT}")'
    )
    p.add_argument(
        "--timeout-ms",
        type=int,
        help=f"Wait timeout in ms (default: {DEFAULT_TIMEOUT_MS})",
    )
    p.add_argument("--out-dir", help=f"Screenshot directory (default: {DEFAULT_OUT_DIR})")
    p.add_argument("--window", help=f"Viewport WxH (default: {DEFAULT_WINDOW})")
    p.add_argument(
        "--headed", action="store_true", help="Show browser (default: headless)"
    )
    p.add_argument(
        "--check-size",
        action="store_true",
        help=f"Assert element size (default: {DEFAULT_CHECK_SIZE})",
    )
    p.add_argument(
        "--min-width", type=int, help=f"Min width if --check-size (default: {DEFAULT_MIN_WIDTH})"
    )
    p.add_argument(
        "--min-height", type=int, help=f"Min height if --check-size (default: {DEFAULT_MIN_HEIGHT})"
    )

    # --- Email options ---
    p.add_argument("--email-to", nargs="+", help="One or more recipient emails")
    p.add_argument("--email-cc", nargs="+", help="One or more CC emails")
    p.add_argument("--email-subject", help="Subject for the results email")
    p.add_argument(
        "--email-attach-screens",
        action="store_true",
        help="Attach screenshots to the results email",
    )
    p.add_argument(
        "--email-inline-images",
        action="store_true",
        help="Embed screenshots inline in the email body (default: True)",
    )
    p.add_argument(
        "--no-email-inline-images",
        dest="email_inline_images",
        action="store_false",
        help="Do not embed screenshots inline; only attach",
    )
    p.set_defaults(email_inline_images=True)

    # SMTP options
    p.add_argument("--smtp-server", default=DEFAULT_SMTP_SERVER, help="SMTP server (default: smtp.gmail.com)")
    p.add_argument("--smtp-port", type=int, default=DEFAULT_SMTP_PORT, help="SMTP port (default: 587)")
    p.add_argument("--smtp-use-ssl", action="store_true", help="Use SSL (port 465). Default: STARTTLS on 587")
    p.add_argument("--smtp-user", help="SMTP username (usually your Gmail address)")
    p.add_argument("--smtp-user-env", default=DEFAULT_SMTP_USER_ENV,
                   help=f"Env var name for SMTP username (default: {DEFAULT_SMTP_USER_ENV})")
    p.add_argument("--smtp-pass", help="SMTP password or app password (NOT recommended; prefer env)")
    p.add_argument("--smtp-pass-env", default=DEFAULT_SMTP_PASS_ENV,
                   help=f"Env var name containing SMTP password (default: {DEFAULT_SMTP_PASS_ENV})")
    p.add_argument("--smtp-from-name", default=DEFAULT_SMTP_FROM_NAME, help="From display name")

    return p.parse_args()


# ---------------- SMTP helpers ---------------- #

def _guess_mime_type(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


def build_email_html_and_inline_map(
    results: List[UrlResult],
    overall_ok: bool,
    inline_images: bool,
) -> Tuple[str, Dict[Path, str]]:
    """
    Builds the HTML body and returns a mapping of Path -> content-id (for inline images).
    """
    inline_cid_by_path: Dict[Path, str] = {}

    rows_html = []
    for r in results:
        status_color = "#28a745" if r.ok else "#dc3545"
        status_text = "PASS" if r.ok else "FAIL"
        shot = r.screenshot_path or r.failure_screenshot_path

        if shot and inline_images:
            # Create a content-id for this image
            cid = make_msgid(domain="local").strip("<>")
            inline_cid_by_path[Path(shot).resolve()] = cid
            shot_cell = f'cid:{cid}'
        elif shot:
            # If not embedding inline, just show filename; the file will be attached
            shot_cell = f'{html.escape(Path(shot).name)} (attached)'
        else:
            shot_cell = "(none)"

        safe_msg = html.escape(r.message or "")
        url_escaped = html.escape(r.url)

        rows_html.append(
            f"""
            <tr>
              <td style="white-space:nowrap;">{url_escaped}</td>
              <td style="color:{status_color}; font-weight:bold; text-align:center;">{status_text}</td>
              <td>{safe_msg}</td>
              <td><img style="width: 33%; height: 33%;" src="{shot_cell}"></td>
            </tr>
            """
        )

    overall_status_text = "PASS" if overall_ok else "FAIL"
    overall_color = "#28a745" if overall_ok else "#dc3545"

    body_html = f"""
    <html>
    <body style="font-family:Segoe UI, Arial, sans-serif; font-size:13px;">
      <p>Hi,</p>
      <p>We have completed the JP Agent Web checking and <strong>{overall_status_text}</strong> result was found for ({datetime.now().strftime("%Y-%m-%d %H:%M")}):</p>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;">
        <thead style="background:#f2f2f2;">
          <tr>
            <th align="left">URL</th>
            <th>Status</th>
            <th align="left">Message</th>
            <th align="left">Screenshot</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows_html)}
        </tbody>
      </table>
      <p>
        Overall result:
        <strong style="color:{overall_color};">
          {"ALL PASSED" if overall_ok else "SOME FAILURES"}
        </strong>
      </p>
      <p>Regards,<br/>Mexican Boy</p>
    </body>
    </html>
    """.strip()

    return body_html, inline_cid_by_path


def compose_email_message(
    smtp_from_name: str,
    smtp_user: str,
    email_to: List[str],
    email_cc: List[str],
    subject: str,
    html_body: str,
    attachments: List[Path],
    inline_cid_by_path: Dict[Path, str],
) -> MIMEMultipart:
    """
    Create a MIME email with HTML + inline images + regular attachments.
    """
    # Use multipart/related for inline images
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = formataddr((smtp_from_name, smtp_user))
    msg["To"] = ", ".join(email_to) if email_to else ""
    if email_cc:
        msg["Cc"] = ", ".join(email_cc)

    # Add HTML body inside multipart/alternative
    alt = MIMEMultipart("alternative")
    msg.attach(alt)
    alt.attach(MIMEText("This email contains HTML content. Please use an HTML-compatible email client.", "plain"))
    alt.attach(MIMEText(html_body, "html"))

    # Attach inline images first (so they can be referenced by cid)
    inline_paths_set = set(p.resolve() for p in inline_cid_by_path.keys())

    for path in inline_paths_set:
        try:
            with open(path, "rb") as f:
                img_data = f.read()
            ctype = _guess_mime_type(path)
            subtype = ctype.split("/", 1)[1] if "/" in ctype else "png"
            mime_image = MIMEImage(img_data, _subtype=subtype)
            cid = inline_cid_by_path[path]
            mime_image.add_header("Content-ID", f"<{cid}>")
            mime_image.add_header("Content-Disposition", "inline", filename=path.name)
            msg.attach(mime_image)
            print(f"[Embed Inline] OK: {path} (cid:{cid})")
        except Exception as e:
            print(f"[Embed Inline] Failed: {path} -> {e}", file=sys.stderr)

    # Attach files (including screenshots if requested), skipping those already embedded inline
    for path in attachments:
        rp = Path(path).resolve()
        if rp in inline_paths_set:
            # Already embedded inline; skip duplicate attachment.
            continue
        ctype = _guess_mime_type(rp)
        maintype, subtype = ctype.split("/", 1)
        try:
            with open(rp, "rb") as f:
                file_data = f.read()
            if maintype == "image":
                part = MIMEImage(file_data, _subtype=subtype)
            else:
                part = MIMEBase(maintype, subtype)
                part.set_payload(file_data)
                encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=rp.name)
            msg.attach(part)
            print(f"[Attach] OK: {rp}")
        except Exception as e:
            print(f"[Attach] Failed: {rp} -> {e}", file=sys.stderr)

    return msg


def send_via_smtp(
    message: MIMEMultipart,
    smtp_server: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    use_ssl: bool = False,
):
    # Build recipient list from headers (To + Cc)
    recipients: List[str] = []
    for hdr in ("To", "Cc"):
        vals = message.get_all(hdr, [])
        recipients.extend([addr for _, addr in getaddresses(vals) if addr])

    if not recipients:
        print("WARNING: No recipients found in To/Cc. Email will not be sent.", file=sys.stderr)
        return

    # Envelope sender should be the actual SMTP user
    envelope_from = smtp_user

    if use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
            server.login(smtp_user, smtp_password)
            server.sendmail(envelope_from, recipients, message.as_string())
    else:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.sendmail(envelope_from, recipients, message.as_string())

    print(f"SMTP email sent to: {', '.join(recipients)}")


# ---------------- Main ---------------- #

def main():
    args = parse_args()

    urls: List[str] = args.urls if args.urls else DEFAULT_URLS
    selector: str = args.selector or DEFAULT_SELECTOR
    expected_text: str = args.expected_text or DEFAULT_EXPECTED_TEXT
    timeout_ms: int = args.timeout_ms if args.timeout_ms is not None else DEFAULT_TIMEOUT_MS
    out_dir_str: str = args.out_dir or DEFAULT_OUT_DIR
    window: str = args.window or DEFAULT_WINDOW
    headed: bool = args.headed or DEFAULT_HEADED
    check_size: bool = args.check_size or DEFAULT_CHECK_SIZE
    min_w: int = args.min_width if args.min_width is not None else DEFAULT_MIN_WIDTH
    min_h: int = args.min_height if args.min_height is not None else DEFAULT_MIN_HEIGHT

    # Email params
    email_to: List[str] = args.email_to if args.email_to else DEFAULT_EMAIL_TO
    email_cc: List[str] = args.email_cc if args.email_cc else DEFAULT_EMAIL_CC
    email_subject_arg: Optional[str] = args.email_subject
    email_attach_screens: bool = args.email_attach_screens or DEFAULT_EMAIL_ATTACH_SCREENS
    email_inline_images: bool = args.email_inline_images

    # SMTP params
    smtp_server: str = args.smtp_server
    smtp_port: int = args.smtp_port
    smtp_use_ssl: bool = args.smtp_use_ssl

    # Prefer CLI values; else read env via provided env var names; else fall back to defaults (SMTP_USERNAME/PASSWORD).
    smtp_user = args.smtp_user or os.environ.get(args.smtp_user_env) or os.environ.get(DEFAULT_SMTP_USER_ENV)
    smtp_pass = args.smtp_pass or os.environ.get(args.smtp_pass_env) or os.environ.get(DEFAULT_SMTP_PASS_ENV)
    smtp_from_name: str = args.smtp_from_name

    if not smtp_user:
        print("ERROR: SMTP username missing. Provide --smtp-user or set env SMTP_USERNAME.", file=sys.stderr)
        sys.exit(2)
    if not smtp_pass:
        print("ERROR: SMTP password missing. Provide --smtp-pass or set env SMTP_PASSWORD.", file=sys.stderr)
        sys.exit(2)

    out_dir = Path(out_dir_str)
    out_dir.mkdir(parents=True, exist_ok=True)

    driver = build_driver(headed=headed, window=window)

    results: List[UrlResult] = []
    overall_ok = True
    try:
        for url in urls:
            screenshot_path: Optional[Path] = None
            failure_path: Optional[Path] = None

            try:
                validate_ui(
                    driver=driver,
                    url=url,
                    selector=selector,
                    expected_text=expected_text,
                    timeout_ms=timeout_ms,
                    check_size=check_size,
                    min_w=min_w,
                    min_h=min_h,
                )
                fname = out_dir / f"{stamp()}_{sanitize(url)}.png"
                fullpage_screenshot(driver, fname, min_w, min_h)
                screenshot_path = fname
                print(f"Screenshot captured for {url}: {fname}")
                results.append(UrlResult(url=url, ok=True, message="OK", screenshot_path=screenshot_path))
            except Exception as e:
                overall_ok = False
                msg = f"{type(e).__name__}: {e}"
                print("FAILURE:", msg, file=sys.stderr)

                try:
                    failure_path = out_dir / f"{stamp()}_{sanitize(url)}_failure.png"
                    driver.save_screenshot(str(failure_path))
                    print(f"Failure screenshot captured: {failure_path}")
                except Exception:
                    pass

                results.append(UrlResult(url=url, ok=False, message=msg, failure_screenshot_path=failure_path))

        # Prepare dynamic subject if not provided
        status_word = "PASS" if overall_ok else "FAIL"
        email_subject = email_subject_arg or f"GOCC – Health Check – JP AgentWeb URL – ({datetime.now().strftime('%H%M')}) HKT – {status_word}"

        # --- Build email body + attachments ---
        body_html, inline_cid_by_path = build_email_html_and_inline_map(
            results=results,
            overall_ok=overall_ok,
            inline_images=email_inline_images,
        )

        attachments: List[Path] = []
        if email_attach_screens:
            cand_paths: List[Path] = []
            for r in results:
                if r.screenshot_path:
                    cand_paths.append(r.screenshot_path)
                if r.failure_screenshot_path:
                    cand_paths.append(r.failure_screenshot_path)

            for p in cand_paths:
                try:
                    rp = Path(p).resolve()
                except Exception as e:
                    print(f"[Attach] Skipping (resolve failed): {p} -> {e}", file=sys.stderr)
                    continue

                if not rp.exists():
                    print(f"[Attach] Skipping (does not exist): {rp}", file=sys.stderr)
                    continue
                if not rp.is_file():
                    print(f"[Attach] Skipping (not a file): {rp}", file=sys.stderr)
                    continue
                if len(str(rp)) > 230:
                    print(f"[Attach] Warning: path long ({len(str(rp))} chars). Consider shorter --out-dir. {rp}", file=sys.stderr)

                attachments.append(rp)

        # Compose and send via SMTP
        try:
            msg = compose_email_message(
                smtp_from_name=smtp_from_name,
                smtp_user=smtp_user,
                email_to=email_to,
                email_cc=email_cc,
                subject=email_subject,
                html_body=body_html,
                attachments=attachments,
                inline_cid_by_path=inline_cid_by_path if email_inline_images else {},
            )
            send_via_smtp(
                message=msg,
                smtp_server=smtp_server,
                smtp_port=smtp_port,
                smtp_user=smtp_user,
                smtp_password=smtp_pass,
                use_ssl=smtp_use_ssl,
            )
        except Exception as e:
            print(f"Failed to compose/send SMTP email: {e}", file=sys.stderr)

        if not overall_ok:
            sys.exit(1)
        print("\nAll URLs passed UI validation. Screenshots captured.")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()

