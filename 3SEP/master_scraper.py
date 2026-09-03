"""
master_scraper.py - Expedia Partner Central All-In-One Functional Pipeline

Features:
  - 100% Dynamic Requests & Playwright Automation (No hardcoded credentials/tokens in queries)
  - Modular Functional Design with Comprehensive Exception Handling
  - Live Data Extraction: Reservations, Cards, Invoices, Feedback, Reviews, Competitors
  - Consolidated Master JSON & CSV Export
"""

import json
import csv
import os
import logging
import re
import time
import uuid
import base64
import random
import sys
import argparse
# pyrefly: ignore [missing-import]
import primp
# pyrefly: ignore [missing-import]
import certifi
from typing import Optional
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import urllib.request

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    sync_playwright = None

# --- GLOBAL CONFIGURATION ---
PROPERTY_ID = 51
EXPEDIA_EMAIL = os.environ.get("EXPEDIA_EMAIL", "integrations@aptli.ai")
EXPEDIA_PASSWORD = os.environ.get("EXPEDIA_PASSWORD", "YOUR_EXPEDIA_PASSWORD")
TOTP_SECRET = os.environ.get("EXPEDIA_TOTP_SECRET", "YOUR_TOTP_SECRET")

def _force_utc_iso(ts):
    """Parses any ISO timestamp and strictly converts and formats it as UTC with a Z suffix."""
    if not ts: return None
    try:
        from datetime import datetime, timezone
        ts_str = str(ts).strip()
        if ts_str.endswith('Z'): ts_str = ts_str[:-1] + '+00:00'
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        return ts
# HEADLESS = os.environ.get("EXPEDIA_HEADLESS", "1").strip().lower() in ("1", "true", "yes")

HEADLESS = os.environ.get("EXPEDIA_HEADLESS", "0").strip().lower() in ("1", "true", "yes")


SSO_URL = "https://www.expediapartnercentral.com/lodging/sso/sign-in/oidc/eg"
DASHBOARD_URL_RE = re.compile(r"https?://apps\.expediapartnercentral\.com/lodging/", re.I)
MFA_URL_RE = re.compile(r"(account/mfa|challenge|factor|passcode)", re.I)

OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../JSONOUTPUT"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Create diagnostic directory
DIAGNOSTIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../saved_response29"))
os.makedirs(DIAGNOSTIC_DIR, exist_ok=True)

# Configure Logging
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../logs'))
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, 'master_scraper.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- DATE & SEARCH CONFIGURATION ---
# Add reservation IDs, confirmation numbers, or guest names to the list below to scrape specific reservations.
# Leave list empty [] to scrape by date range instead.
EXPEDIA_SEARCH_IDS = []
# Set SCRAPE_DAYS_BACK to widen/narrow the date window when NOT using EXPEDIA_SEARCH_IDS
SCRAPE_DAYS_BACK = 3
RESERVATION_CHUNK_DAYS = 14

# --- GMAIL OTP CONFIGURATION ---
GMAIL_OAUTH_TOKEN = os.environ.get("GMAIL_OAUTH_TOKEN", "YOUR_GMAIL_OAUTH_TOKEN_HERE")
GMAIL_RECIPIENT_EMAIL = "admin@expediapartnercentral.com"
GMAIL_SUBJECT_FILTER = "Your access code for Partner Central"


# --- LOGIN STATE ENUM ---
class LoginState(Enum):
    EMAIL = "EMAIL"
    PASSWORD = "PASSWORD"
    MFA = "MFA"
    LOGGED_IN = "LOGGED_IN"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


def detect_login_state(page):
    """Detect the current login state by inspecting the DOM."""
    try:
        current_url = page.url
        page_title = page.title()
        
        # Check for logged in state
        if DASHBOARD_URL_RE.search(current_url):
            # Verify dashboard content is visible
            try:
                if page.locator("nav, .navigation, [role='navigation']").count() > 0:
                    return LoginState.LOGGED_IN
            except:
                pass
            # URL is correct, assume logged in
            return LoginState.LOGGED_IN
        
        # Check for error state
        try:
            error_texts = ["error", "invalid", "failed", "locked", "blocked", "unauthorized"]
            page_text = page.inner_text("body").lower()
            if any(err in page_text for err in error_texts):
                return LoginState.ERROR
        except:
            pass
        
        # Check for MFA state
        try:
            # Look for OTP/passcode inputs
            otp_inputs = page.locator("input[type='tel'][maxlength='6'], input[type='text'][maxlength='6'], input[name*='code'], input[name*='otp'], input[data-testid*='code']")
            if otp_inputs.count() > 0:
                return LoginState.MFA
            
            # Look for 6 individual digit inputs
            digit_inputs = page.locator("input[type='text'][maxlength='1'], input[type='tel'][maxlength='1']")
            if digit_inputs.count() >= 6:
                return LoginState.MFA
            
            # Check for verification/access code text
            page_text = page.inner_text("body").lower()
            mfa_keywords = ["verification", "access code", "passcode", "one-time", "otp", "authenticator"]
            if any(kw in page_text for kw in mfa_keywords):
                return LoginState.MFA
        except:
            pass
        
        # Check for password state
        try:
            password_input = page.locator("input[type='password']")
            if password_input.count() > 0 and password_input.is_visible():
                return LoginState.PASSWORD
        except:
            pass
        
        # Check for email state
        try:
            email_input = page.locator("input[type='email'], input[name='email'], input[autocomplete='username']")
            if email_input.count() > 0 and email_input.is_visible():
                return LoginState.EMAIL
        except:
            pass
        
        return LoginState.UNKNOWN
        
    except Exception as e:
        logger.error(f"[!] Error detecting login state: {type(e).__name__}")
        return LoginState.UNKNOWN


def log_login_diagnostics(page, stage):
    """Log safe diagnostics about the current login state."""
    try:
        current_url = page.url
        page_title = page.title()
        
        # Get visible inputs
        inputs = []
        try:
            for inp in page.locator("input").all():
                inp_type = inp.get_attribute("type") or "text"
                inp_name = inp.get_attribute("name") or ""
                if inp.is_visible():
                    inputs.append(f"{inp_type}:{inp_name}")
        except:
            pass
        
        # Get visible buttons
        buttons = []
        try:
            for btn in page.locator("button").all():
                if btn.is_visible():
                    btn_text = btn.inner_text().strip()
                    if btn_text:
                        buttons.append(btn_text[:30])
        except:
            pass
        
        # Check specific fields
        password_visible = False
        mfa_visible = False
        try:
            password_visible = page.locator("input[type='password']").is_visible()
        except:
            pass
        
        try:
            mfa_visible = (page.locator("input[type='tel'][maxlength='6']").count() > 0 or
                          page.locator("input[type='text'][maxlength='6']").count() > 0 or
                          page.locator("input[type='text'][maxlength='1']").count() >= 6)
        except:
            pass
        
        # Check for iframe
        iframe_exists = False
        try:
            iframe_exists = page.locator("iframe").count() > 0
        except:
            pass
        
        logger.debug(f"[LOGIN DEBUG]")
        logger.info(f"stage={stage}")
        logger.info(f"url={current_url}")
        logger.info(f"title={page_title}")
        logger.info(f"inputs={','.join(inputs) if inputs else 'none'}")
        logger.info(f"buttons={','.join(buttons) if buttons else 'none'}")
        logger.info(f"password_visible={password_visible}")
        logger.info(f"mfa_visible={mfa_visible}")
        logger.info(f"iframe_exists={iframe_exists}")
        
    except Exception as e:
        logger.error(f"[!] Error logging diagnostics: {type(e).__name__}")


def save_login_failure(page, stage):
    """Save screenshot and HTML on login failure."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    try:
        screenshot_path = os.path.join(DIAGNOSTIC_DIR, f"login_failure_{timestamp}.png")
        page.screenshot(path=screenshot_path)
        logger.info(f"Screenshot saved: {screenshot_path}")
    except Exception as e:
        logger.error(f"[!] Failed to save screenshot: {e}")
    
    try:
        html_path = os.path.join(DIAGNOSTIC_DIR, f"login_failure_{timestamp}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
        logger.info(f"HTML saved: {html_path}")
    except Exception as e:
        logger.error(f"[!] Failed to save HTML: {e}")
    
    logger.error(f"LOGIN FAILED")
    logger.info(f"Current URL: {page.url}")
    logger.info(f"Page title: {page.title()}")
    logger.info(f"Failure stage: {stage}")
    
    return screenshot_path, html_path
# ==============================================================================
# 0. GMAIL AUTOMATED OTP FETCHER
# ==============================================================================

class GmailOtpFetcher:
    def __init__(self, token: str, recipient_email: str, subject_filter: str):
        self.token = token.strip()
        self.recipient_email = recipient_email.strip()
        self.subject_filter = subject_filter.strip()

    def _build_credentials(self):
        return Credentials(token=self.token)

    def _get_gmail_service(self, creds):
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _validate_token(self):
        url = f"https://oauth2.googleapis.com/tokeninfo?access_token={self.token}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if "error" in data or "expires_in" not in data:
                    logger.info("\n" + "="*60)
                    logger.error(" [!] GMAIL OAUTH TOKEN EXPIRED OR INVALID!")
                    logger.info(" Please generate a new token (ya29...) and update")
                    logger.info(" GMAIL_OAUTH_TOKEN on Line 39 of master_scraper.py")
                    logger.info("="*60 + "\n")
                    raise RuntimeError("Token invalid or expired.")
        except Exception as e:
            logger.info("\n" + "="*60)
            logger.error(" [!] GMAIL OAUTH TOKEN EXPIRED OR INVALID!")
            logger.info(" Please generate a new token (ya29...) and update")
            logger.info(" GMAIL_OAUTH_TOKEN on Line 39 of master_scraper.py")
            logger.info("="*60 + "\n")
            raise RuntimeError(f"Token validation failed: {e}")

    def _read_emails_by_filters(self, service, strategy):
        query_parts = []
        if strategy.get("to"):
            query_parts.append(f"to:{strategy['to']}")
        if strategy.get("from"):
            query_parts.append(f"from:{strategy['from']}")
        if strategy.get("subject"):
            query_parts.append(f"subject:\"{strategy['subject']}\"")
        query = " ".join(query_parts)

        result = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
        messages = result.get("messages", [])
        emails = []
        for m in messages:
            msg = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
            text_body = ""
            html_body = ""
            payload = msg.get("payload", {})
            parts = payload.get("parts", [])
            if not parts and payload.get("body", {}).get("data"):
                try:
                    text_body = base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="ignore")
                except Exception:
                    pass
            for part in parts:
                mime = part.get("mimeType", "")
                body_data = part.get("body", {}).get("data")
                if body_data:
                    try:
                        decoded = base64.urlsafe_b64decode(body_data).decode(errors="ignore")
                        if mime == "text/plain": text_body += decoded
                        elif mime == "text/html": html_body += decoded
                    except Exception:
                        pass
            emails.append({"id": m["id"], "fullTextBody": text_body, "htmlBody": html_body})
        return emails

    def _extract_otp(self, text_body: str, html_body: str) -> Optional[str]:
        patterns = [
            r"(?:code|verification|otp|password|pin|access\s*code)[^\d]{0,25}(\b\d{6}\b)",
            r"(?<!\d)(\b\d{6}\b)(?!\d)",
            r"(\b\d{3}[-\s]\d{3}\b)",
        ]
        for src in [text_body, html_body]:
            if not src: continue
            plain = re.sub(r"<[^>]+>", " ", src)
            for pat in patterns:
                m = re.search(pat, plain, re.IGNORECASE)
                if m:
                    code = re.sub(r"\D", "", m.group(1))
                    if len(code) == 6:
                        return code
        return None

    def clear_old_otps(self):
        """Trashes any existing emails matching the OTP filters to ensure we only get the freshest code."""
        try:
            self._validate_token()
            creds = self._build_credentials()
            service = self._get_gmail_service(creds)
            filter_strategies = [
                {"from": self.recipient_email, "subject": self.subject_filter},
                {"to": self.recipient_email, "subject": self.subject_filter},
                {"subject": self.subject_filter},
            ]
            for strategy in filter_strategies:
                emails = self._read_emails_by_filters(service, strategy)
                for e in emails:
                    if e.get("id"):
                        try:
                            service.users().messages().trash(userId="me", id=e["id"]).execute()
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"[!] Warning: failed to clear old OTPs: {e}")

    def fetch_latest_otp(self, timeout_seconds: int = 40, trash_email: bool = True) -> Optional[str]:
        self._validate_token()
        creds = self._build_credentials()
        service = self._get_gmail_service(creds)
        
        filter_strategies = [
            {"from": self.recipient_email, "subject": self.subject_filter},
            {"to": self.recipient_email, "subject": self.subject_filter},
            {"subject": self.subject_filter},
        ]
        
        started = time.monotonic()
        while time.monotonic() - started < timeout_seconds:
            for strategy in filter_strategies:
                try:
                    emails = self._read_emails_by_filters(service, strategy)
                    if emails:
                        latest = emails[0]
                        otp = self._extract_otp(latest.get("fullTextBody", ""), latest.get("htmlBody", ""))
                        if otp:
                            logger.info(f"[+] Gmail OTP auto-fetched successfully: {otp}")
                            if trash_email and latest.get("id"):
                                try:
                                    service.users().messages().trash(userId="me", id=latest["id"]).execute()
                                except Exception:
                                    pass
                            return otp
                except Exception:
                    pass
            time.sleep(3)
        return None


PASSWORDVAULT_PATH = r"E:\mnt\qwf-data\TestWorkFlow\output_passwordvault.json"

def _refresh_gmail_token():
    """
    Auto-refreshes the Gmail OAuth access token using the refresh_token
    stored in the passwordvault. Returns a fresh access token string, or
    falls back to the hardcoded GMAIL_OAUTH_TOKEN.
    """
    try:
        import json as _json, urllib.request as _req, urllib.parse as _parse
        vault = _json.load(open(PASSWORDVAULT_PATH, encoding="utf-8"))
        auth = vault.get("data", {}).get("GOOGLE_AUTH", {})
        refresh_token = auth.get("refresh_token")
        client_id     = auth.get("client_id")
        client_secret = auth.get("client_secret")
        token_uri     = auth.get("token_uri", "https://oauth2.googleapis.com/token")

        if not (refresh_token and client_id and client_secret):
            logger.error("[!] Passwordvault missing OAuth fields — using hardcoded token")
            return GMAIL_OAUTH_TOKEN

        payload = _parse.urlencode({
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     client_id,
            "client_secret": client_secret,
        }).encode()

        req = _req.Request(token_uri, data=payload, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with _req.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())

        new_token = result.get("access_token")
        if new_token:
            logger.info(f"[+] Gmail OAuth token auto-refreshed (expires in {result.get('expires_in','?')}s)")
            return new_token
        else:
            logger.error(f"[!] Token refresh response missing access_token: {result}")
    except Exception as e:
        logger.error(f"[!] Token auto-refresh failed: {e}")
    return GMAIL_OAUTH_TOKEN


def clear_gmail_otps():
    """Clears any old OTP emails from the inbox to prevent fetching expired codes."""
    try:
        token = _refresh_gmail_token()
        fetcher = GmailOtpFetcher(token, GMAIL_RECIPIENT_EMAIL, GMAIL_SUBJECT_FILTER)
        fetcher.clear_old_otps()
    except Exception as e:
        logger.error(f"[!] Could not clear old OTPs: {e}")

def fetch_gmail_otp():
    """Wrapper to automatically fetch OTP via Gmail API with auto token refresh."""
    logger.info("[*] Fetching 6-digit access code from Gmail API...")
    try:
        token = _refresh_gmail_token()
        fetcher = GmailOtpFetcher(token, GMAIL_RECIPIENT_EMAIL, GMAIL_SUBJECT_FILTER)
        otp = fetcher.fetch_latest_otp(timeout_seconds=35, trash_email=True)
        if otp:
            return otp
    except Exception as e:
        logger.error(f"[!] Gmail OTP API error: {e}")
    return None


def _fill_first(page, selectors, value, timeout=15000):
    combined_selector = ", ".join(selectors)
    try:
        loc = page.locator(combined_selector).first
        loc.wait_for(state="visible", timeout=timeout)
        loc.click(timeout=timeout)  # Click on the field first to focus it
        loc.fill(value, timeout=timeout)
        return combined_selector
    except Exception as e:
        raise RuntimeError(f"No matching input: {selectors}") from e


def _click_first(page, selectors, timeout=10000):
    combined_selector = ", ".join(selectors)
    try:
        loc = page.locator(combined_selector).first
        loc.wait_for(state="visible", timeout=timeout)
        loc.click(timeout=timeout)
        return combined_selector
    except Exception as e:
        raise RuntimeError(f"No matching button: {selectors}") from e


def _submit_otp(page, code):

    code = str(code).strip()
    digit_boxes = page.locator("input[maxlength='1']")
    try:
        n = digit_boxes.count()
    except Exception:
        n = 0

    if n >= 6:
        # Fill each digit box instantly — no sleep between digits
        for i, ch in enumerate(code[:n]):
            digit_boxes.nth(i).fill(ch)
        method = "digit-boxes"
    else:
        # Try single-field OTP input — use fill() which is instant
        filled = False
        selectors = [
            "input[autocomplete='one-time-code']",
            "input[name*='otp' i]",
            "input[id*='otp' i]",
            "input[name*='code' i]",
            "input[id*='code' i]",
            "input[inputmode='numeric']",
            "input[type='tel']",
            "input[type='text']",
            "input[type='number']",
        ]
        combined_sel = ", ".join(selectors)
        try:
            loc = page.locator(combined_sel).first
            loc.wait_for(state="visible", timeout=3000)
            loc.fill("")          # Clear first
            loc.fill(code)        # Fill full 6-digit code at once (instant)
            filled = True
        except Exception:
            pass
        if not filled:
            # Last resort: focus and type via keyboard (char by char, but still fast)
            try:
                page.keyboard.type(code, delay=0)
            except Exception:
                pass
        method = "single-field"

    # Press Enter immediately — no delay
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass

    # Also click submit button as fallback (with short timeout)
    try:
        _click_first(page, [
            "button[type='submit']",
            "button:has-text('Verify')",
            "button:has-text('Continue')",
            "button:has-text('Submit')",
            "button:has-text('Confirm')",
            "button:has-text('Log in')",
        ], timeout=3000)
    except Exception:
        pass
    return method


def login_playwright_auto(property_id=PROPERTY_ID):
    """Performs dynamic automated login with TOTP handling and captures cookies."""
    if sync_playwright is None:
        logger.error("[!] Playwright is not installed: pip install playwright && playwright install chromium")
        return None

    logger.info("=" * 60)
    logger.info(f" Automated Dynamic Login (Headless={HEADLESS})")
    logger.info("=" * 60)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=HEADLESS,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            logger.info("[*] Navigating to Partner Central SSO...")
            page.goto(SSO_URL, wait_until="domcontentloaded", timeout=60000)

            logger.info("[*] Submitting email...")
            _fill_first(page, [
                "input[type='email']",
                "input[name='email']",
                "input[id*='email' i]",
                "input[autocomplete='username']",
            ], EXPEDIA_EMAIL)

            # Click Continue / Next to proceed to Password page if separate
            try:
                if not page.locator("input[type='password']").is_visible():
                    _click_first(page, [
                        "button[type='submit']",
                        "button:has-text('Continue')",
                        "button:has-text('Next')",
                        "button:has-text('Sign in')",
                    ], timeout=5000)
            except Exception:
                pass

            logger.info("[*] Submitting password...")
            
            # 0) Clear out any old expired OTPs from previous runs BEFORE we submit the password,
            #    because Expedia automatically emails the code the moment the password is submitted.
            logger.info(" [*] Clearing old MFA emails from inbox...")
            clear_gmail_otps()
            page.locator("input[type='password'], input[name='password']").first.wait_for(state="visible", timeout=30000)
            # No extra wait — password field is visible, fill immediately

            _fill_first(page, [
                "input[type='password']",
                "input[name='password']",
                "input[id*='password' i]",
                "input[autocomplete='current-password']",
            ], EXPEDIA_PASSWORD)

            page.wait_for_timeout(100)  # tiny settle before pressing Enter
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass

            try:
                _click_first(page, [
                    "button[type='submit']",
                    "button:has-text('Sign in')",
                    "button:has-text('Log in')",
                    "button:has-text('Continue')",
                ], timeout=4000)
            except Exception:
                pass
            logger.info("[+] Credentials submitted.")

            logger.info("[*] Waiting for dashboard arrival and MFA processing...")
            start_time = time.time()
            otp_attempted = False

            while time.time() - start_time < 120:
                current_url = page.url
                if DASHBOARD_URL_RE.search(current_url):
                    break

                # Detect MFA — check URL pattern OR check page DOM for OTP input boxes
                # Expedia often shows MFA on the same /Account/Logon URL, so DOM check is essential
                def _mfa_on_page():
                    try:
                        return (
                            page.locator("input[maxlength='1']").count() >= 6 or
                            page.locator("input[type='tel'][maxlength='6']").count() > 0 or
                            page.locator("input[autocomplete='one-time-code']").count() > 0 or
                            page.locator("input[name*='otp' i], input[id*='otp' i], input[name*='code' i]").count() > 0
                        )
                    except Exception:
                        return False

                url_is_mfa = MFA_URL_RE.search(current_url) or "mfa" in current_url.lower()
                page_has_otp = _mfa_on_page()

                if not otp_attempted and (url_is_mfa or page_has_otp):
                    logger.info(f"\n" + "=" * 50)
                    logger.error(f" [!] MFA Verification Required (Email)")
                    logger.info(f" [*] Screen: {current_url}")
                    logger.info(f" [*] Detected via: {'URL' if url_is_mfa else 'DOM (OTP field found)'}")
                    logger.info("=" * 50)
                    
                    # 1) Trigger the email if we are on an initiation screen BEFORE fetching the code
                    try:
                        if "initiate" in page.url.lower():
                            logger.info(" [*] Clicking 'Send Email' / 'Get code' to trigger new OTP...")
                            _click_first(page, [
                                "button:has-text('Email')",
                                "button:has-text('Send code')",
                                "button:has-text('Send')",
                                "button:has-text('Continue')",
                                "input[type='submit']",
                            ], timeout=2000)
                            time.sleep(1) # wait for Expedia to send the email
                    except Exception as e:
                        pass
                    
                    # 2) Fetch the code
                    code = None
                    try:
                        code = fetch_gmail_otp()
                    except Exception as e:
                        logger.error(f"[!] Auto-fetch error: {e}")
                    
                    if not code:
                        try:
                            code = input("\n[?] Enter 6-digit MFA code from email: ").strip()
                        except Exception as e:
                            logger.error(f"[!] Input error: {e}")
                        
                    if code:
                        used = _submit_otp(page, code)
                        logger.info(f"[+] MFA code ({code}) submitted ({used}). Proceeding with login...")
                        otp_attempted = True
                        try:
                            page.wait_for_url(DASHBOARD_URL_RE, timeout=30000)
                            break
                        except Exception:
                            pass

                if "account/logon" in current_url.lower() and not page_has_otp:
                    time.sleep(0.5)

                time.sleep(0.3)  # Fast poll — check URL every 300ms

            if not DASHBOARD_URL_RE.search(page.url):
                try:
                    page.goto("https://apps.expediapartnercentral.com/lodging/multiproperty/MultiProperty.html", timeout=20000, wait_until="networkidle")
                except Exception:
                    pass

            if not DASHBOARD_URL_RE.search(page.url):
                logger.error(f"[!] Login timeout. Did not reach dashboard in 120s. Current URL: {page.url}")
                browser.close()
                return None

            page.wait_for_timeout(300)  # brief settle on dashboard
            logger.info(f"[+] Successfully arrived on dashboard: {page.url}")
            
            # Activate active property session context for target property
            logger.info(f"[*] Activating property {property_id} session context...")
            try:
                if "multiproperty" in page.url.lower():
                    prop_elem = page.locator(f"a[href*='htid={property_id}'], a[href*='{property_id}']").first
                    if prop_elem.count() > 0:
                        prop_elem.click()
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                    else:
                        page.goto(f"https://apps.expediapartnercentral.com/lodging/home/home.html?htid={property_id}", timeout=20000)
                else:
                    page.goto(f"https://apps.expediapartnercentral.com/lodging/home/home.html?htid={property_id}", timeout=20000)
            except Exception as e:
                logger.error(f"[!] Property navigation note: {e}")

            # Also seed /lodging/bookings, /lodging/reservations, and /lodging/conversations session cookies
            try:
                page.goto(f"https://apps.expediapartnercentral.com/lodging/bookings?htid={property_id}", timeout=20000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                page.goto(f"https://apps.expediapartnercentral.com/lodging/reservations/legacyReservationDetails.html?htid={property_id}", timeout=20000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                page.goto(f"https://apps.expediapartnercentral.com/supply/inbox?propertyId={property_id}", timeout=20000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                page.goto(f"https://apps.expediapartnercentral.com/lodging/conversations/messageCenter.html?htid={property_id}", timeout=20000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass

            page.wait_for_timeout(300)  # brief settle before extracting cookies
            logger.info(f"[+] Property session active at: {page.url}")

            cookies_list = context.cookies()
            cookie_dict = {}
            for c in cookies_list:
                name = c.get("name")
                val = c.get("value")
                cookie_dict[name] = val
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
            browser.close()
            return cookie_str
    except Exception as e:
        logger.error(f"[!] Error during automated browser login: {type(e).__name__} - {e}")
    return None


def get_valid_cookies(property_id=PROPERTY_ID, max_retries=3):
    """Always initiates a fresh dynamic automated login on every run, with retries."""
    for attempt in range(1, max_retries + 1):
        if attempt == 1:
            logger.info("[*] Initiating fresh dynamic automated login...")
        else:
            logger.info(f"[*] Retry {attempt - 1}/{max_retries - 1}: Initiating fresh dynamic automated login...")
        
        try:
            cookies = login_playwright_auto(property_id=property_id)
            if cookies:
                return cookies
        except Exception as e:
            logger.error(f"[!] Dynamic login failed: {type(e).__name__} - {e}")
            
        if attempt < max_retries:
            logger.error("[!] Login failed or timed out. Retrying in 5 seconds...")
            import time
            time.sleep(5)
            
    return None


def fetch_hotel_info(session, cookie_str, property_id=PROPERTY_ID):
    """Dynamically fetches the property name and metadata for any property ID using the official propertyInfo API."""
    api_url = f"https://apps.expediapartnercentral.com/lodging/bookings/propertyInfo?htid={property_id}"
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "client-name": "pc-reservations-web",
        "content-type": "application/json",
        "cookie": cookie_str,
        "referer": f"https://apps.expediapartnercentral.com/lodging/bookings?htid={property_id}",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    hotel_info = {
        "Property ID": str(property_id),
        "Name": f"Property #{property_id}"
    }
    
    try:
        r = session.get(api_url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            resp = data.get("response") or {}
            prop_name = resp.get("propertyName")
            if prop_name:
                hotel_info["Name"] = prop_name
            if resp.get("businessModel"):
                hotel_info["Business Model"] = resp.get("businessModel")
            if resp.get("currencyCode"):
                hotel_info["Currency"] = resp.get("currencyCode")
            if resp.get("propertyTimeZone", {}).get("timeZoneName"):
                hotel_info["Time Zone"] = resp.get("propertyTimeZone", {}).get("timeZoneName")
            return hotel_info
    except Exception:
        pass
        
    # Fallback to HTML scraping if API fails
    try:
        html_url = f"https://apps.expediapartnercentral.com/lodging/home/home.html?htid={property_id}"
        r = session.get(html_url, headers=headers, timeout=15)
        if r.status_code == 200:
            m = re.search(r'hotelName\s*:\s*["\'](.*?)["\']', r.text)
            if m and m.group(1).strip():
                hotel_info["Name"] = m.group(1).strip()
            else:
                title_match = re.search(r'<title>(.*?)</title>', r.text)
                if title_match:
                    clean_title = title_match.group(1).replace("Expedia Partner Central - ", "").replace("Expedia Partner Central", "").strip()
                    if clean_title:
                        hotel_info["Name"] = clean_title
    except Exception:
        pass
    return hotel_info


# ==============================================================================
# 2. RESERVATIONS & PAYMENT CARDS SCRAPER
# ==============================================================================

def fetch_reservations(session, cookie_str, property_id=PROPERTY_ID, start_date=None, end_date=None, search_term=None, max_retries=3):
    """Fetches reservation list with dynamic search date range, optional search term, and auto-retry."""
    url = "https://api.expediapartnercentral.com/supply/experience/gateway/graphql"
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "cookie": cookie_str,
        "client-name": "pc-reservations-web",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    
    today = datetime.now()
    if search_term:
        # If searching for a specific res/name, widen date range drastically to ensure it's found
        start_date = (today - timedelta(days=365*2)).strftime("%Y-%m-%d")
        end_date = (today + timedelta(days=365)).strftime("%Y-%m-%d")
        logger.info(f"[*] Scraping Reservations for Search Term: '{search_term}' (widened date range)")
        search_query_part = f'searchParam: "{search_term}",'
    else:
        if not start_date:
            start_date = "2026-08-29"
        if not end_date:
            end_date = "2026-09-01"
        logger.info(f"[*] Scraping Reservations for Date Range: {start_date} to {end_date} (checkIn)")
        search_query_part = ""
    
    payload = {
        "query": f"""query getReservationsBySearchCriteria {{
            reservationSearchV2(input: {{
                propertyId: {property_id}, 
                booked: true, canceled: true, confirmed: true, 
                startDate: "{start_date}", endDate: "{end_date}", 
                dateType: "checkIn", 
                expediaCollect: true, hotelCollect: true, 
                timezoneOffset: "-04:00", 
                isSpecialRequest: false, isVIPBooking: false, 
                reconciled: false, readyToReconcile: false, 
                returnBookingItemIDsOnly: false, 
                unconfirmed: true, searchForCancelWaiversOnly: false,
                {search_query_part}
            }}) {{ 
                reservationItems {{ 
                    reservationItemId 
                    reservationInfo {{ startDate endDate createDateTime product {{ unitName }} }}
                    customer {{ id guestName }}
                    confirmationInfo {{ productConfirmationCode }}
                    totalAmounts {{ propertyBookingTotal {{ value currencyCode }} }}
                }} 
            }}
        }}""",
        "variables": {}
    }

    for attempt in range(1, max_retries + 1):
        try:
            response = session.post(url, headers=headers, json=payload, timeout=35)
            if response.status_code == 200:
                data = response.json()
                if "errors" in data and not data.get("data"):
                    logger.error(f"[!] GraphQL error on attempt {attempt}/{max_retries}: {data['errors']}")
                    if attempt < max_retries:
                        time.sleep(2 * attempt)
                        continue
            else:
                logger.error(f"[!] Reservations query returned HTTP {response.status_code} (attempt {attempt}/{max_retries})")
                logger.error(f"    Error: {response.text}")
                if attempt < max_retries:
                    time.sleep(2 * attempt)
                    continue
                        
            data_body = data.get("data") or {}
            search_res = data_body.get("reservationSearchV2") or {}
            items = search_res.get("reservationItems") or []
            parsed = []
            for item in items:
                if not item: continue
                res_info = item.get("reservationInfo") or {}
                customer = item.get("customer") or {}
                conf_info = item.get("confirmationInfo") or {}
                total_amt = (item.get("totalAmounts") or {}).get("propertyBookingTotal") or {}
                
                parsed.append({
                    "Guest": customer.get("guestName"),
                    "Reservation": str(item.get("reservationItemId", "")),
                    "Confirmation": str(conf_info.get("productConfirmationCode", "")),
                    "Check-in": res_info.get("startDate"),
                    "Check-out": res_info.get("endDate"),
                    "Room": (res_info.get("product") or {}).get("unitName"),
                    "Booked on": res_info.get("createDateTime"),
                    "Booking amount": f"{total_amt.get('value')} {total_amt.get('currencyCode')}" if total_amt.get('value') is not None else None
                })
            logger.info(f"[+] Found {len(parsed)} reservations.")
            return parsed
        except Exception as e:
            logger.error(f"[!] Error fetching reservations list (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(2 * attempt)
    
    logger.error("[!] Failed to fetch reservations after max retries.")
    return []



def fetch_nightly_breakdown(session, cookie_str, reservation_id, reservation_status="BOOKED", property_id=PROPERTY_ID):
    """
    Calls the same ReservationDetailsQuery GraphQL endpoint to extract the detailed
    per-night breakdown (taxes + fees per day) from detailedPaymentLineItems.nightlyPaymentList.
    Returns a dict keyed by ISO date string: {"2026-09-01": {"tax": 12.28, "fee": 5.00}}
    """
    from datetime import datetime as _dt
    url = "https://apps.expediapartnercentral.com/graphql"
    headers = {
        "accept": "application/json, multipart/mixed",
        "content-type": "application/json",
        "cookie": cookie_str,
        "x-apollo-operation-name": "ReservationDetailsQuery",
        "x-apollo-operation-type": "query",
        "x-hcom-origin-id": "Supply.PaymentDisplay.v1",
        "x-page-id": "Supply.PaymentDisplay.v1",
        "client-info": "partner-central-web,6f3d36c52ad8220a6aaf884af6ab0eff18b67cd3,us-west-2",
        "referer": f"https://apps.expediapartnercentral.com/supply/reservations/payment-display?htid={property_id}&bookingId={reservation_id}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    payload = [{
        "operationName": "ReservationDetailsQuery",
        "variables": {
            "propertyContext": {"propertyId": str(property_id)},
            "context": {
                "siteId": 2056, "locale": "en_US", "eapid": 1, "tpid": 101, "currency": "USD",
                "device": {"type": "DESKTOP"},
                "identity": {"duaid": "5152a574-75f9-442f-9917-521578b3049b", "authState": "ANONYMOUS"},
                "privacyTrackingState": "CAN_TRACK", "debugContext": {"abacusOverrides": []}
            },
            "reservationContext": {
                "reservationId": str(reservation_id),
                "reservationStatus": reservation_status
            },
            "components": ["PAYMENT_DETAILS"]
        },
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "547cab402a95b3f0408653ee6ffe57a561f80c05351e3f83cc8f2d5ecf76c8f5"
            }
        }
    }]

    nightly_breakdown = {}  # {"2026-09-01": {"tax": 12.28, "fee": 5.00}}
    try:
        r = session.post(url, headers=headers, json=payload, timeout=20)
        data = r.json()
        
        # DEBUG DUMP
        with open(os.path.join(OUTPUT_DIR, f"graphql_bd_{reservation_id}.json"), "w", encoding="utf-8") as df:
            json.dump(data, df, indent=4)

        # Navigate into the response: list -> [0] -> data -> reservationDetails -> supplyReservationsPaymentDetails
        payment_details = (data[0].get("data", {}) if isinstance(data, list) else data.get("data", {}))
        payment_details = payment_details.get("reservationDetails", {}).get("supplyReservationsPaymentDetails", {})
        if not payment_details:
            logger.warning(f"    [?] No payment details found in GraphQL response for {reservation_id} (Expected if cancelled).")
            return nightly_breakdown
        nightly_list = payment_details.get("detailedPaymentLineItems", {}).get("nightlyPaymentList", [])
        for night in nightly_list:
            # label.text is like "September 1, 2026"
            label_text = night.get("label", {}).get("text", "")
            try:
                dt_obj = _dt.strptime(label_text.strip(), "%B %d, %Y")
                iso_date = dt_obj.strftime("%Y-%m-%d")
            except Exception:
                continue  # Skip if date parse fails

            base_val = None
            tax_total = 0.0
            fee_total = 0.0
            night_taxes = []
            
            for item in night.get("nightlyPaymentLineItems", []):
                item_name = (item.get("lineItemText", {}).get("text") or "").strip()
                item_name_lower = item_name.lower()
                amount_text = (item.get("amount", {}).get("amount", {}) or {}).get("text", "0")
                try:
                    amount_val = float(str(amount_text).replace(",", ""))
                except Exception:
                    amount_val = 0.0
                if amount_val == 0:
                    continue  # Only skip exact zeros, let negative values process

                if "room rate" in item_name_lower:
                    base_val = amount_val
                elif "promotion" in item_name_lower:
                    tax_total += amount_val
                    # Don't add to nightly taxes, promotions are handled in summary
                elif "retained by expedia" in item_name_lower or "amount retained" in item_name_lower:
                    fee_total += amount_val
                    # Don't add Expedia commission to the tax list
                elif amount_val > 0:
                    is_fee = "fee" in item_name_lower
                    is_tax = any(kw in item_name_lower for kw in ["tax", "assessment", "levy", "surcharge"])
                    if is_fee:
                        fee_total += amount_val
                    elif is_tax:
                        tax_total += amount_val
                    
                    if is_fee or is_tax:
                        night_taxes.append({
                            "stay_date": iso_date,
                            "tax_type": item_name,
                            "tax_rate": None,
                            "taxable_amount": None,
                            "tax_amount": amount_val
                        })
            
            res_dict = {"tax": round(tax_total, 2), "fee": round(fee_total, 2), "taxes": night_taxes}
            if base_val is not None:
                res_dict["base"] = round(base_val, 2)
            nightly_breakdown[iso_date] = res_dict
            
        summary_items = []
        summary_list = payment_details.get("detailedPaymentLineItems", {}).get("summaryPaymentLineItems", [])
        for item in summary_list:
            if item.get("__typename") == "SupplyReservationsPaymentDetailsSubLineItem":
                text = (item.get("lineItemText", {}).get("text") or "").strip()
                amt_str = (item.get("amount", {}).get("amount", {}) or {}).get("text", "0")
                try:
                    amount = float(str(amt_str).replace(",", ""))
                except Exception:
                    amount = 0.0
                if text and amount != 0:
                    summary_items.append({"description": text, "amount": amount})
                    
        if summary_items:
            nightly_breakdown["__summary_items__"] = summary_items
            
    except Exception as e:
        logger.error(f"    [!] Nightly breakdown fetch failed for {reservation_id}: {type(e).__name__} - {e}")
    return nightly_breakdown


def fetch_evc_card_data(session, cookie_str, property_id=PROPERTY_ID, card_resource_id=None, booking_id=None):
    """Fetches virtual card / payment details using modern Expedia EVC API endpoints supporting JSON and HTML embedded state."""
    headers = {
        "accept": "application/json, text/html, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "client-name": "pc-reservations-web",
        "cookie": cookie_str,
        "referer": f"https://apps.expediapartnercentral.com/lodging/bookings?htid={property_id}&bookingItemId={booking_id or ''}",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    # Candidate endpoints for modern Card / EVC details
    endpoints = []
    if card_resource_id:
        endpoints.append(f"https://apps.expediapartnercentral.com/lodging/reservations/getEVCCardDataByCardResourceId?htid={property_id}&cardResourceId={card_resource_id}")
        endpoints.append(f"https://apps.expediapartnercentral.com/lodging/bookings/getEVCCardDataByCardResourceId?htid={property_id}&cardResourceId={card_resource_id}")
    if booking_id:
        endpoints.append(f"https://apps.expediapartnercentral.com/lodging/reservations/getEVCCardDataByCardResourceId?htid={property_id}&cardResourceId={booking_id}")
        endpoints.append(f"https://apps.expediapartnercentral.com/supply/reservations/payment-display?htid={property_id}&bookingId={booking_id}")

    for endpoint in endpoints:
        try:
            r = session.get(endpoint, headers=headers, timeout=15)
            if r.status_code == 200:
                # 1. Try direct JSON parsing
                try:
                    data = r.json()
                    if isinstance(data, dict) and (data.get("cardNumber") or data.get("cvv") or data.get("cardData")):
                        card = data.get("cardData") or data
                        return {
                            "Card Type": card.get("cardType", "Virtual Card"),
                            "Card Number": str(card.get("cardNumber", "")),
                            "Expires": f"{card.get('expirationDate', {}).get('month', '')}/{card.get('expirationDate', {}).get('year', '')}" if isinstance(card.get("expirationDate"), dict) else str(card.get("expirationDate", "")),
                            "CVV": str(card.get("cvv", "")),
                            "Billing Address": card.get("billingAddress"),
                            "Card Status": card.get("status") or card.get("cardStatus") or card.get("state"),
                            "Charge Before": card.get("chargeBefore") or card.get("chargeBeforeDate") or card.get("validUntil"),
                            "Remaining Balance": card.get("remainingBalance") or card.get("balance"),
                            "Transactions": card.get("transactions") or card.get("cardTransactions") or card.get("authorizations") or card.get("activity") or [],
                            "EVC Raw": card
                        }
                except Exception:
                    pass

                # 2. Try HTML embedded state parsing
                html = r.text
                m_card = re.search(r'(?:cardData|cardInfo|evcInfo)\s*[:=]\s*(\{.*?\})', html)
                if not m_card:
                    m_card = re.search(r'var\s+jsonPayload\s*=\s*(\{.*?\});', html, re.DOTALL)
                if m_card:
                    try:
                        cdata = json.loads(m_card.group(1))
                        val = cdata.get("value", [{}])[0] if isinstance(cdata.get("value"), list) else cdata
                        evc = val.get("paymentInfo", {}).get("evcInfo") or val.get("evcInfo") or val
                        if evc.get("cardNumber"):
                            return {
                                "Card Type": evc.get("cardType", "Virtual Card"),
                                "Card Number": str(evc.get("cardNumber", "")),
                                "Expires": f"{evc.get('expirationDate', {}).get('month', '')}/{evc.get('expirationDate', {}).get('year', '')}" if isinstance(evc.get("expirationDate"), dict) else str(evc.get("expirationDate", "")),
                                "CVV": str(evc.get("cvv", "")),
                                "Billing Address": evc.get("billingAddress"),
                                "Card Status": evc.get("status") or evc.get("cardStatus") or evc.get("state"),
                                "Charge Before": evc.get("chargeBefore") or evc.get("chargeBeforeDate") or evc.get("validUntil"),
                                "Remaining Balance": evc.get("remainingBalance") or evc.get("balance"),
                                "Transactions": evc.get("transactions") or evc.get("cardTransactions") or evc.get("authorizations") or evc.get("activity") or [],
                                "EVC Raw": evc
                            }
                    except Exception:
                        pass
        except Exception:
            pass
    return None


def fetch_reservation_details(session, cookie_str, reservation_id, property_id=PROPERTY_ID, max_retries=2):
    """Fetches comprehensive card, pricing, and history details from reservationDetails.html / legacyReservationDetails.html."""
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "max-age=0",
        "client-name": "pc-reservations-web",
        "cookie": cookie_str,
        "referer": f"https://apps.expediapartnercentral.com/lodging/bookings?htid={property_id}&bookingItemId={reservation_id}",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    last_html = ""
    candidate_urls = [
        f"https://apps.expediapartnercentral.com/lodging/reservations/reservationDetails.html?htid={property_id}&reservationIds={reservation_id}",
        f"https://apps.expediapartnercentral.com/lodging/reservations/legacyReservationDetails.html?htid={property_id}&reservationIds={reservation_id}",
        f"https://apps.expediapartnercentral.com/supply/reservations/payment-display?htid={property_id}&bookingId={reservation_id}"
    ]
    
    for attempt in range(1, max_retries + 1):
        for target_url in candidate_urls:
            try:
                if attempt > 1:
                    headers["cache-control"] = "no-cache"
                    headers["pragma"] = "no-cache"

                response = session.get(target_url, headers=headers, timeout=25)
                if response.status_code == 200:
                    html = response.text
                    m = re.search(r'var\s+jsonPayload\s*=\s*(\{.*?\});', html, re.DOTALL)
                    if not m:
                        m = re.search(r'(?:bookingInfo|paymentInfo|reservationDetails)\s*=\s*(\{.*?\});', html, re.DOTALL)
                    
                    if m:
                        last_html = html
                        try:
                            data = json.loads(m.group(1))
                            res_val = data.get("value", [{}])[0] if isinstance(data.get("value"), list) else data
                            
                            payment = res_val.get("payment", {})
                            payment_info = res_val.get("paymentInfo", {})
                            evc = payment_info.get("evcInfo", {})
                            customer_card = payment_info.get("customerCardInfo", {})
                            booking_info = res_val.get("bookingInfo", {})
                            amounts = res_val.get("bookingAmounts", {}).get("lineItems", [])
                            total_amounts = res_val.get("totalAmounts", {})
                            
                            details = {
                                "Payment Information": payment.get("supplier_payment_instruction")
                            }
                            if evc and evc.get("cardNumber"):
                                details["Card Type"] = "Virtual Card"
                                details["Card Number"] = str(evc.get("cardNumber", ""))
                                details["Expires"] = f"{evc.get('expirationDate', {}).get('month')}/{evc.get('expirationDate', {}).get('year')}" if isinstance(evc.get("expirationDate"), dict) else str(evc.get("expirationDate", ""))
                                details["CVV"] = str(evc.get("cvv", ""))
                                details["Billing Address"] = evc.get("billingAddress")
                            elif customer_card and customer_card.get("cardNumber"):
                                details["Card Type"] = "Real Card"
                                details["Card Number"] = str(customer_card.get("cardNumber", ""))
                                details["Expires"] = customer_card.get("expirationDate")
                                details["CVV"] = str(customer_card.get("cvv", ""))
                                details["Billing Address"] = customer_card.get("billingAddress")
                            else:
                                # Try fetching from modern EVC endpoint if creditCardID or paymentToken is present
                                card_res_id = (evc.get("creditCardID") or payment.get("paymentToken") or "").strip()
                                evc_card = fetch_evc_card_data(session, cookie_str, property_id, card_resource_id=card_res_id, booking_id=reservation_id)
                                if evc_card:
                                    details.update(evc_card)
                                else:
                                    details["Card Type"] = None
                                
                            sub_total_item = next((item for item in amounts if item.get("type") == "SUB_TOTAL"), {})
                            total_item = next((item for item in amounts if item.get("type") == "TOTAL"), {})
                            
                            details.update({
                                "All Amounts": amounts,
                                "Nightly rates": [item for item in amounts if item.get("type") == "DAILY_RATE"],
                                "Subtotal": sub_total_item.get("priceAmount") or total_item.get("priceAmount") or total_amounts.get("totalBookingAmount", {}).get("amount"),
                                "Total payout": sub_total_item.get("costAmount") or total_item.get("costAmount"),
                                "Amount retained by Expedia Group": -(total_amounts.get("totalCommissionAmount", {}).get("amount") or 0.0),
                                "Hotel confirmation code": booking_info.get("hotelConfirmationCode"),
                                "Status": booking_info.get("status"),
                                "Itinerary number": booking_info.get("itineraryNumber"),
                                "Reservation made": booking_info.get("bookingDate"),
                                "Pricing model": booking_info.get("pricingModel"),
                                "IATA/TIDS #": booking_info.get("IATANumber"),
                                "Bedding request": booking_info.get("bedTypeName"),
                                "Rate plan code": booking_info.get("ratePlanCode"),
                                "Rate plan name": booking_info.get("ratePlanName"),
                                "Guest count": (booking_info.get("adultCount") or 0) + (booking_info.get("childCount") or 0),
                                "Cancellation Policy": res_val.get("cancelPolicy", {}),
                                "cancelPolicyDescription": res_val.get("cancelPolicyDescription", ""),
                                "Reservation History": res_val.get("history", {}),
                                "Guest Country": booking_info.get("country")
                            })
                            
                            # DEBUG DUMP
                            with open(os.path.join(OUTPUT_DIR, f"raw_res_{reservation_id}.json"), "w", encoding="utf-8") as df:
                                json.dump(res_val, df, indent=4)
                            
                            # Fetch per-night detailed tax/fee breakdown from ReservationDetailsQuery GraphQL
                            res_status = booking_info.get("status", "BOOKED").upper()
                            nightly_bd = fetch_nightly_breakdown(session, cookie_str, reservation_id, reservation_status=res_status, property_id=property_id)
                            details["Nightly Breakdown"] = nightly_bd
                                
                            return details
                        except json.JSONDecodeError as e:
                            logger.error(f"[!] JSON parsing error for {reservation_id}: {e}")
                        
                # Also try modern side-panel EVC data
                evc_card = fetch_evc_card_data(session, cookie_str, property_id, booking_id=reservation_id)
                if evc_card:
                    return evc_card
                    
            except Exception:
                pass

        # If payload was not received on this attempt, do a refresh delay before retrying
        if attempt < max_retries:
            retry_delay = random.uniform(2.5, 4.0) * attempt
            logger.info(f"    [↻] Refreshing / retrying ResID={reservation_id} (Attempt {attempt}/{max_retries}) in {retry_delay:.1f}s...")
            time.sleep(retry_delay)
            
    logger.error(f"[!] Warning: No jsonPayload matched for {reservation_id} after {max_retries} refresh attempts. Saving debug HTML.")
    debug_path = os.path.join(OUTPUT_DIR, f"debug_{reservation_id}.html")
    if last_html:
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(last_html)
    return {}


# ==============================================================================
# 3. GUEST MESSAGES SCRAPER
# ==============================================================================

def fetch_messages(session, cookie_str, property_id=PROPERTY_ID):
    """Fetches ALL message inbox conversation IDs by paginating through every page."""
    url = "https://apps.expediapartnercentral.com/graphql"
    headers = {
        "accept": "application/json, multipart/mixed",
        "content-type": "application/json",
        "cookie": cookie_str,
        "client-info": "partner-central-web,d668969128a5a0249bea879ddb5e72c6a7757ea2,us-west-2",
        "x-apollo-operation-name": "InteractionList",
        "x-apollo-operation-type": "query",
        "x-hcom-origin-id": "Supply.Inbox.v1",
        "x-page-id": "Supply.Inbox.v1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

    all_cids = []
    page_number = 1
    page_size = 50
    MAX_PAGES = 20  # Safety cap to avoid infinite loop

    while page_number <= MAX_PAGES:
        payload = [{
            "operationName": "InteractionList",
            "variables": {
                "propertyContext": { "propertyId": str(property_id) },
                "filterInput": { "filters": [] },
                "searchInput": "",
                "sortInput": None,
                "paginationInput": { "pageNumber": page_number, "pageSize": page_size },
                "context": {
                    "siteId": 2056, "locale": "en_US", "eapid": 1, "tpid": 101, "currency": "USD",
                    "device": { "type": "DESKTOP" },
                    "identity": { "duaid": "537a40d7-b662-4469-aae6-ebc920023ad4", "authState": "ANONYMOUS" },
                    "privacyTrackingState": "CAN_TRACK",
                    "debugContext": { "abacusOverrides": [] }
                },
                "clientDataInput": { "timezone": "America/Los_Angeles" }
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "a537441ee7c49257874bb0eed1f52e687812e459ae970262a571016f108f770a"
                }
            }
        }]
        try:
            r = session.post(url, headers=headers, json=payload, timeout=20)
            if r.status_code == 200:
                data = r.json()
                data_obj = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
                data_body = data_obj.get("data") or {}
                interaction_list_obj = data_body.get("interactions", {}).get("interactionList", {})
                interactions = interaction_list_obj.get("content", {}).get("interactionList", [])
                
                if not interactions:
                    if page_number == 1:
                        logger.error(f"[!] DEBUG GRAPHQL RESPONSE: {data}")
                    break  # No more pages

                page_cids = [item.get("interactionId") for item in interactions if isinstance(item, dict) and item.get("interactionId")]
                all_cids.extend(page_cids)
                logger.info(f"    [Inbox page {page_number}] Fetched {len(page_cids)} conversations (total so far: {len(all_cids)})")

                # Check pagination metadata if available
                pagination = interaction_list_obj.get("pagination") or {}
                total_count = pagination.get("totalCount") or pagination.get("total") or 0
                if total_count and len(all_cids) >= int(total_count):
                    break  # Got all records

                # If we got a full page, there may be more
                if len(page_cids) < page_size:
                    break  # Partial page = last page

                page_number += 1
                time.sleep(random.uniform(0.8, 1.5))  # Polite pacing between pages
            else:
                logger.error(f"[!] Messages inbox query returned HTTP {r.status_code} on page {page_number}")
                break
        except Exception as e:
            logger.error(f"[!] Messages inbox error on page {page_number}: {type(e).__name__} - {e}")
            break

    logger.info(f"[+] Total inbox conversations found: {len(all_cids)}")
    return all_cids


def fetch_chat_thread(session, cookie_str, cid, property_id=PROPERTY_ID):
    """Fetches chat log for an interaction conversation ID."""
    url = f"https://apps.expediapartnercentral.com/lodging/conversations/messageCenter.html?htid={property_id}&cid={cid}&view=2pv&propertyId={property_id}"
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "max-age=0",
        "client-name": "pc-reservations-web",
        "referer": f"https://apps.expediapartnercentral.com/supply/inbox?propertyId={property_id}",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "cookie": cookie_str
    }
    import requests
    try:
        r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        
        # Save raw HTML to debug
        import os
        os.makedirs(os.path.join(os.path.dirname(__file__), "../saved_response29"), exist_ok=True)
        with open(os.path.join(os.path.dirname(__file__), f"../saved_response29/chat_cid_{cid}.html"), "w", encoding="utf-8") as f:
            f.write(r.text)
            
        if r.status_code == 200:
            text = r.text
            start_str = 'w.initialData'
            start_idx = text.find(start_str)
            if start_idx != -1:
                brace_idx = text.find('{', start_idx)
                if brace_idx != -1:
                    brace_start = brace_idx
                    count = 0
                    in_string = False
                    string_char = None
                    escape = False
                    raw_js_str = ""
                    
                    for i in range(brace_start, len(text)):
                        char = text[i]
                        if escape: 
                            escape = False
                            continue
                        if char == '\\': 
                            escape = True
                            continue
                            
                        if in_string:
                            if char == string_char:
                                in_string = False
                        else:
                            if char in ('"', "'", "`"):
                                in_string = True
                                string_char = char
                            elif char == '{': 
                                count += 1
                            elif char == '}':
                                count -= 1
                                if count == 0:
                                    raw_js_str = text[brace_start:i+1]
                                    break
                                    
                    if raw_js_str:
                        # Safely convert JS object to JSON by quoting keys ONLY outside strings!
                        out = []
                        in_string = False
                        escape = False
                        string_char = None
                        i_idx = 0
                        while i_idx < len(raw_js_str):
                            c = raw_js_str[i_idx]
                            if escape:
                                out.append(c)
                                escape = False
                                i_idx += 1
                                continue
                            if c == '\\':
                                escape = True
                                out.append(c)
                                i_idx += 1
                                continue
                            if in_string:
                                if c == string_char:
                                    in_string = False
                                out.append(c)
                                i_idx += 1
                                continue
                            if c in ('"', "'", "`"):
                                in_string = True
                                string_char = c
                                out.append(c)
                                i_idx += 1
                                continue
                                
                            if c.isalpha() or c == '_':
                                word = ""
                                while i_idx < len(raw_js_str) and (raw_js_str[i_idx].isalnum() or raw_js_str[i_idx] == '_'):
                                    word += raw_js_str[i_idx]
                                    i_idx += 1
                                j = i_idx
                                while j < len(raw_js_str) and raw_js_str[j].isspace():
                                    j += 1
                                if j < len(raw_js_str) and raw_js_str[j] == ':':
                                    out.append('"' + word + '"')
                                else:
                                    out.append(word)
                                continue
                                
                            out.append(c)
                            i_idx += 1
                            
                        json_str = "".join(out)
                        
                        try:
                            data = json.loads(json_str)
                        except json.JSONDecodeError as e:
                            logger.error(f"[!] JSONDecodeError after string-aware extraction: {e}")
                            data = {}
                            
                        conversation = data.get("selectedConversation") or (data.get('conversationListResult', {}).get('conversations', [])[0] if data.get('conversationListResult', {}).get('conversations') else {})
                        if conversation:
                            res_info = conversation.get("reservationInfo") or conversation.get("metadata", {}).get("reservationInfo", {})
                            res_id = res_info.get("reservationId")
                            parsed_chat = {
                                "Reservation ID": res_id,
                                "Guest": res_info.get("guestName") or conversation.get("guest", {}).get("name") or "Unknown",
                                "Messages": conversation.get("messages", [])
                            }
                            return parsed_chat, res_id
        elif r.status_code in (301, 302, 303, 307, 308):
            logger.error(f"[!] messageCenter.html redirected to: {r.headers.get('location')}")
            return None, None
    except Exception as e:
        logger.error(f"[!] Chat thread extraction error for CID {cid}: {type(e).__name__} - {e}")
    return None, None

def fetch_chat_by_reservation(session, cookie_str, res_id, property_id=PROPERTY_ID):
    """Fetches chat log directly using the reservationId instead of cid."""
    url = f"https://apps.expediapartnercentral.com/lodging/conversations/messageCenter.html?htid={property_id}&reservationId={res_id}&view=2pv&propertyId={property_id}"
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "max-age=0",
        "client-name": "pc-reservations-web",
        "referer": f"https://apps.expediapartnercentral.com/supply/inbox?propertyId={property_id}",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    import requests
    try:
        r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        if r.status_code == 200:
            text = r.text
            start_str = 'w.initialData = {'
            start_idx = text.find(start_str)
            if start_idx != -1:
                brace_start = start_idx + len('w.initialData = ')
                count = 0
                escape = False
                for i in range(brace_start, len(text)):
                    char = text[i]
                    if escape: escape = False; continue
                    if char == '\\': escape = True
                    elif char == '{': count += 1
                    elif char == '}':
                        count -= 1
                        if count == 0:
                            raw_js_str = text[brace_start:i+1]
                            json_str = re.sub(r'([{\[,]\s*)([a-zA-Z0-9_]+)(\s*:)', r'\1"\2"\3', raw_js_str)
                            data = json.loads(json_str)
                            conversation = data.get("selectedConversation") or (data.get('conversationListResult', {}).get('conversations', [])[0] if data.get('conversationListResult', {}).get('conversations') else {})
                            if conversation:
                                return conversation.get("messages", [])
                            break
    except Exception as e:
        logger.error(f"[!] Chat extraction error for ResID {res_id}: {type(e).__name__} - {e}")
    return []


# ==============================================================================
# 4. IN-HOUSE FEEDBACK & COMPETITOR MARKET DATA
# ==============================================================================

def fetch_feedback(session, cookie_str, days=7):
    """Fetches in-house guest feedback using the get_rtr_reviews.json API to fetch the last X days."""
    parsed_feedback = []
    start_at = 0
    size = 10
    more_available = True
    
    # Calculate cutoff date
    cutoff_date = datetime.now() - timedelta(days=days)
    
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "cookie": cookie_str,
        "referer": f"https://apps.expediapartnercentral.com/lodging/review/realtime_feedback.html?htid={PROPERTY_ID}",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    logger.info(f"[*] Fetching In-House Feedback for the last {days} days via API...")

    while more_available:
        url = f"https://apps.expediapartnercentral.com/lodging/review/get_rtr_reviews.json?htid={PROPERTY_ID}&startAt={start_at}&stubbing=false&filteredBy=&size={size}"
        
        try:
            r = session.get(url, headers=headers, timeout=20)
            if r.status_code == 200:
                data = r.json()
                reviews = data.get("reviews", [])
                
                if not reviews:
                    break
                    
                for rv in reviews:
                    guest_name = f"{rv.get('guestFirstName', '')} {rv.get('guestLastName', '')}".strip()
                    received_date = rv.get("updateDate") or rv.get("createDate")
                    comment = rv.get("comment", "")
                    reservation_id = str(rv.get("reservationId", ""))
                    
                    feedback_parts = []
                    for ans in rv.get("structuredAnswers", []):
                        cat = ans.get("category", "")
                        tags = ans.get("tagTexts", [])
                        custom_tags = ans.get("customTags", [])
                        all_tags = tags + custom_tags
                        if cat and all_tags:
                            feedback_parts.append(f"{cat}: {', '.join(all_tags)}")
                            
                    guest_feedback = " | ".join(feedback_parts)
                    
                    hotel_responses = rv.get("supplierResponses")
                    response_text = ""
                    if hotel_responses:
                        resps = []
                        for resp in hotel_responses:
                            t = resp.get("responseType", "")
                            m = resp.get("message", "")
                            d = resp.get("createdTime", "")
                            if t == "THANK":
                                resps.append(f"[{d}] Thanked Guest")
                            else:
                                resps.append(f"[{d}] {t}: {m}")
                        response_text = "\n".join(resps)
                        
                    parsed_feedback.append({
                        "Guest name": guest_name,
                        "Received date": received_date,
                        "Guest Feedback": guest_feedback,
                        "Comments if available": comment,
                        "Hotel's Response if made": response_text,
                        "ReservationId": reservation_id
                    })
                    
                more_available = data.get("moreReviewsAvailable", False)
                
                # Check if the last review in this batch is older than our cutoff
                last_review = reviews[-1]
                last_time_str = last_review.get("reviewTime") or last_review.get("createDate")
                if last_time_str:
                    try:
                        # e.g., '2026-08-31T09:40:41.554Z'
                        dt_str = last_time_str[:10] # '2026-08-31'
                        review_date = datetime.strptime(dt_str, "%Y-%m-%d")
                        if review_date < cutoff_date:
                            more_available = False # Stop fetching
                    except Exception as e:
                        pass
                
                start_at += size
                if more_available:
                    time.sleep(random.uniform(0.5, 1.0))
            else:
                logger.error(f"[!] In-house feedback API returned HTTP {r.status_code}")
                break
        except Exception as e:
            logger.error(f"[!] Error fetching feedback API: {type(e).__name__} - {e}")
            break

    logger.info(f"[+] Successfully fetched {len(parsed_feedback)} in-house feedback items (cutoff: {days} days).")
    return parsed_feedback


def fetch_competitors(session, cookie_str, property_id=PROPERTY_ID):
    """Fetches competitor set market comparison rates."""
    url = f"https://apps.expediapartnercentral.com/lodging/review/get_all_competitors.json?htid={property_id}&pageNo=0"
    headers = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "accept-language": "en-US,en;q=0.9",
        "client-name": "pc-reservations-web",
        "cookie": cookie_str,
        "referer": f"https://apps.expediapartnercentral.com/lodging/competitiveset/main.html?htid={property_id}",
        "x-requested-with": "XMLHttpRequest",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        r = session.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            data = r.json()
            comps = data.get("competitorsRate", [])
            parsed = []
            for comp in comps:
                hotel_name = comp.get("hotelName", "").strip()
                if not hotel_name or str(comp.get("hotelId")) == str(property_id):
                    hotel_name = "Country Inn & Suites (You)"
                parsed.append({
                    "Property": hotel_name,
                    "Response%": f"{comp.get('responseRate')}%" if comp.get('responseRate') is not None else None,
                    "Happy": f"{comp.get('happyRate')}%" if comp.get('happyRate') is not None else None
                })
            logger.info(f"[+] Found {len(parsed)} properties in market data.")
            return parsed
        else:
            logger.error(f"[!] Competitors query returned HTTP {r.status_code}")
    except Exception as e:
        logger.error(f"[!] Error fetching competitors: {type(e).__name__} - {e}")
    return []


# ==============================================================================
# 5. POST STAY REVIEWS SCRAPER
# ==============================================================================

def fetch_post_stay_reviews(session, cookie_str, property_id=PROPERTY_ID):
    """Fetches post stay guest reviews & rating insights via GraphQL."""
    url = "https://apps.expediapartnercentral.com/graphql"
    headers = {
        "accept": "application/json, multipart/mixed",
        "accept-language": "en-US,en;q=0.9",
        "client-name": "pc-reservations-web",
        "client-info": "partner-central-web,d668969128a5a0249bea879ddb5e72c6a7757ea2,us-west-2",
        "content-type": "application/json",
        "cookie": cookie_str,
        "x-apollo-operation-name": "SupplyReviewsQuery",
        "x-apollo-operation-type": "query",
        "referer": f"https://apps.expediapartnercentral.com/lodging/reviews/postStayReviews.html?htid={property_id}"
    }
    payload = [{
        "operationName": "SupplyReviewsQuery",
        "variables": {
            "propertyContext": {"propertyId": str(property_id)},
            "reviewId": "",
            "reviewsContext": {
                "filters": [],
                "page": 1
            },
            "context": {
                "siteId": 2056, "locale": "en_US", "eapid": 1, "tpid": 101, "currency": "USD",
                "device": {"type": "DESKTOP"},
                "identity": {"duaid": "537a40d7-b662-4469-aae6-ebc920023ad4", "authState": "ANONYMOUS"},
                "privacyTrackingState": "CAN_TRACK",
                "debugContext": {"abacusOverrides": []}
            }
        },
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "c63e63868f37816ef733f60eeaa5047bc4f7a688494504477f226485fde58d49"
            }
        }
    }]
    try:
        r = session.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code == 200:
            json_data = r.json()
            if isinstance(json_data, list) and json_data and json_data[0].get("data"):
                data = json_data[0].get("data", {}).get("supplyReviews", {})
                
                # Parse Insights
                insights = {}
                insights_pane = data.get("insightsSecondaryPane", {})
                modules = insights_pane.get("modules", [])
                if modules:
                    rating_module = modules[0]
                    insights["Overall Rating"] = rating_module.get("overAllRating", "")
                    insights["Total Reviews"] = rating_module.get("totalReviews", "")
                    breakdown = {}
                    for pb in rating_module.get("progressBars", []):
                        label = pb.get("title", "")
                        count = pb.get("progressDescription", "")
                        if label and count: breakdown[label] = count
                    insights["Review Section Count"] = breakdown
                
                # Parse Reviews
                parsed_reviews = []
                for review in data.get("list", []):
                    traveler = review.get("traveler", {})
                    res_text = traveler.get("action", {}).get("text", "")
                    res_match = re.search(r'#(\d+)', res_text)
                    details = traveler.get("details", [])
                    ratings = review.get("rating", [])
                    contents = review.get("content", {}).get("content", [])
                    
                    parsed_reviews.append({
                        "ReviewId": review.get("reviewCardIdentifier", ""),
                        "Guest name": traveler.get("name", ""),
                        "Res Number Text": res_text,
                        "ReservationId": res_match.group(1) if res_match else "",
                        "Checkin - Checkout dates": details[0].get("text", "") if len(details) >= 1 else "",
                        "Brand": details[1].get("text", "") if len(details) >= 2 else "",
                        "Posted date": review.get("postedDate", {}).get("text", ""),
                        "Rating out of 10": ratings[0].get("text", "") if ratings else "",
                        "Review": " ".join([c.get("text", "") for c in contents]),
                        "Responded message if any from Hotel": str(review.get("response", "")) if review.get("response") else ""
                    })
                logger.info(f"[+] Found {len(parsed_reviews)} Post Stay Reviews.")
                return parsed_reviews, insights
        else:
            logger.error(f"[!] Post Stay Reviews query returned HTTP {r.status_code}")
    except Exception as e:
        logger.error(f"[!] Post Stay Reviews error: {type(e).__name__} - {e}")
    return [], {}


# ==============================================================================
# 6. INVOICES & FINANCIAL SCRAPER
# ==============================================================================

def fetch_invoice_details(session, cookie_str, invoice_number, property_id=PROPERTY_ID):
    """Fetches detailed reservation line items for an invoice across all pages."""
    all_items = []
    page = 1
    max_pages = 10
    
    while page <= max_pages:
        url = "https://apps.expediapartnercentral.com/lodging/finance/hcInvoiceDetails.json"
        params = {
            "htid": str(property_id),
            "invoice": str(invoice_number),
            "page": str(page),
            "printAll": "false"
        }
        headers = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-language": "en-US,en;q=0.8",
            "client-name": "pc-reservations-web",
            "origin-request-id": str(uuid.uuid4()),
            "referer": f"https://apps.expediapartnercentral.com/lodging/finance/hcInvoiceDetails.html?htid={property_id}&invoice={invoice_number}",
            "x-requested-with": "XMLHttpRequest",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "cookie": cookie_str
        }
        try:
            r = session.get(url, headers=headers, params=params, timeout=20)
            if r.status_code == 200:
                data = r.json()
                payload = data.get("payload", {})
                items = payload.get("hcInvoiceDetailsList", [])
                if not items: break
                all_items.extend(items)
                
                pagination = payload.get("pagination", {})
                total_results = pagination.get("totalResults", 0)
                if len(all_items) >= total_results: break
                page += 1
            else:
                break
        except Exception as e:
            logger.error(f"[!] Error fetching invoice {invoice_number} page {page}: {type(e).__name__} - {e}")
            break
            
    return all_items


def fetch_invoices(session, cookie_str, property_id=PROPERTY_ID, start_date=None, end_date=None, chain_group_only=False):
    """Fetches hotel statements & invoices along with itemized breakdowns.
    
    Args:
        session: HTTP session
        cookie_str: Session cookies
        property_id: Property ID
        start_date: Optional start date filter (YYYY-MM-DD format)
        end_date: Optional end date filter (YYYY-MM-DD format) 
        chain_group_only: If True, show only invoices paid by chain/group
    """
    url = f"https://apps.expediapartnercentral.com/lodging/accounting/statementsAndInvoices.html?htid={property_id}&tab=invoices"
    
    # Note: Date and chain/group filters are UI filters in the web interface
    # The API endpoint returns all invoices, but we can filter the results
    logger.info(f"[*] Fetching invoices from: {url}")
    if start_date or end_date:
        logger.info(f"    Date filter: {start_date} to {end_date} (will be applied to results)")
    if chain_group_only:
        logger.info(f"    Chain/Group filter: Yes (will be applied to results)")
    
    headers = {
        "accept": "text/html,application/xhtml+xml",
        "client-name": "pc-reservations-web",
        "cookie": cookie_str,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    try:
        r = session.get(url, headers=headers, timeout=20)
        
        # DEBUG: Save to saved_response29
        os.makedirs(os.path.join(os.path.dirname(__file__), "../saved_response29"), exist_ok=True)
        with open(os.path.join(os.path.dirname(__file__), f"../saved_response29/invoice_list_{property_id}.html"), "w", encoding="utf-8") as f:
            f.write(r.text)
            
        if r.status_code == 200:
            html = r.text
            start = html.find('statementsAndInvoicesPayload:')
            if start != -1:
                start_obj = html.find('{', start)
                brace_count = 0
                json_str = ""
                for i in range(start_obj, len(html)):
                    if html[i] == '{': brace_count += 1
                    elif html[i] == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            json_str = html[start_obj:i+1]
                            break
                            
                if json_str:
                    data = json.loads(json_str)
                    invoices_list = data.get("invoices", {}).get("invoices", [])
                    
                    # Apply date filter if specified
                    if start_date or end_date:
                        filtered_invoices = []
                        for inv in invoices_list:
                            inv_date = inv.get("transactionDate", "")
                            if inv_date:
                                try:
                                    from datetime import datetime
                                    inv_dt = datetime.strptime(inv_date, "%Y-%m-%d").date()
                                    
                                    if start_date:
                                        start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
                                        if inv_dt < start_dt:
                                            continue
                                    
                                    if end_date:
                                        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
                                        if inv_dt > end_dt:
                                            continue
                                    
                                    filtered_invoices.append(inv)
                                except Exception:
                                    # If date parsing fails, include the invoice
                                    filtered_invoices.append(inv)
                        invoices_list = filtered_invoices
                        logger.info(f"    Filtered to {len(invoices_list)} invoices by date range")
                    
                    # Apply chain/group filter if specified
                    if chain_group_only:
                        # Filter invoices that are paid by chain/group
                        # This would typically be indicated by a specific flag or status
                        chain_invoices = [inv for inv in invoices_list if inv.get("isChainGroup", False)]
                        if not chain_invoices:
                            # If no explicit flag, try to infer from status or other properties
                            # This is a placeholder - actual logic depends on API response structure
                            chain_invoices = invoices_list
                        invoices_list = chain_invoices
                        logger.info(f"    Filtered to {len(invoices_list)} chain/group invoices")
                    
                    # Fetch line items concurrently with ThreadPoolExecutor
                    def _load_inv(inv):
                        inv_num = inv.get("transactionNumber", "")
                        items = []
                        if inv_num:
                            items = fetch_invoice_details(session, cookie_str, inv_num, property_id)
                        return {
                            "Type": inv.get("transactionType", ""),
                            "Invoice number": inv_num,
                            "Invoice date": inv.get("transactionDate", ""),
                            "Amount": inv.get("originalAmount", 0.0),
                            "Download": inv.get("pdfFilePath", ""),
                            "Status": inv.get("status", ""),
                            "Payment": inv.get("amountApplied", 0.0),
                            "Line items count": len(items),
                            "Line items": items
                        }

                    with ThreadPoolExecutor(max_workers=6) as executor:
                        parsed_invoices = list(executor.map(_load_inv, invoices_list))
                    return parsed_invoices
        else:
            logger.error(f"[!] Statements & Invoices page returned HTTP {r.status_code}")
    except Exception as e:
        logger.error(f"[!] Error fetching invoices: {type(e).__name__} - {e}")
    return []


# ==============================================================================
# 7. CARD TRANSACTION SCRAPER
# ==============================================================================

def fetch_card_transactions(session, cookie_str, reservation_id, check_in_date, property_id=PROPERTY_ID):
    """Fetches virtual card transaction data for a specific reservation using GraphQL."""
    
    # Normalize check-in date to YYYY-MM-DD format
    if isinstance(check_in_date, str):
        # Handle formats like "Aug 30, 2026" or "2026-08-30"
        if "," in check_in_date:
            # Parse "Aug 30, 2026" format
            from datetime import datetime
            try:
                dt = datetime.strptime(check_in_date, "%b %d, %Y")
                check_in_date = dt.strftime("%Y-%m-%d")
            except:
                pass
    else:
        check_in_date = str(check_in_date)
    
    reservation_id = str(reservation_id)
    
    url = "https://apps.expediapartnercentral.com/graphql"
    headers = {
        "accept": "application/json, multipart/mixed",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "client-info": "partner-central-web,da1b1b8f339b73a18d73bcce62d87973b6c02988,us-west-2",
        "content-type": "application/json",
        "cookie": cookie_str,
        "x-apollo-operation-name": "evcManagementCardData",
        "x-apollo-operation-type": "query",
        "x-hcom-origin-id": "Reservations.Evc.Manager.v1",
        "x-page-id": "Reservations.Evc.Manager.v1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    }
    
    payload = [{
        "operationName": "evcManagementCardData",
        "variables": {
            "evcSearchInput": {
                "checkInDate": check_in_date,
                "inputType": "RESERVATION_ID",
                "textInput": reservation_id
            },
            "context": {
                "siteId": 2056,
                "locale": "en_US",
                "eapid": 1,
                "tpid": 101,
                "currency": "USD",
                "device": {"type": "MOBILE_PHONE"},
                "identity": {"duaid": "537a40d7-b662-4469-aae6-ebc920023ad4", "authState": "ANONYMOUS"},
                "privacyTrackingState": "CAN_TRACK",
                "debugContext": {"abacusOverrides": []}
            }
        },
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "d5cb3f4debd7bad92768da21ea4e96e8969aefb6a28768eff1814e15120a1819"
            }
        }
    }]
    
    logger.info(f"[*] Fetching card transactions: ResID={reservation_id}, CheckIn={check_in_date}")
    
    response = session.post(url, headers=headers, json=payload, timeout=20)
    logger.info(f"[*] Card transaction API response status: {response.status_code}")
    
    if response.status_code != 200:
        logger.error(f"[!] Card transaction API returned HTTP {response.status_code}")
        logger.info(f"    Response: {response.text[:500]}")
        return None
    
    json_data = response.json()
    logger.info(f"[*] Response type: {type(json_data)}")
    
    # Check for GraphQL errors
    if isinstance(json_data, list) and json_data:
        if json_data[0].get("errors"):
            errors = json_data[0].get("errors", [])
            logger.error(f"[!] GraphQL errors: {errors}")
            return None
        
        if not json_data[0].get("data"):
            logger.error(f"[!] No data in response")
            return None
    elif isinstance(json_data, dict):
        if json_data.get("errors"):
            errors = json_data.get("errors", [])
            logger.error(f"[!] GraphQL errors: {errors}")
            return None
        
        if not json_data.get("data"):
            logger.error(f"[!] No data in response")
            return None
    else:
        logger.error(f"[!] Unexpected response structure")
        return None
    
    # Parse the response
    data_container = json_data[0] if isinstance(json_data, list) else json_data
    evc_data = data_container.get("data", {}).get("evcManagementCardData", {})
    
    if not evc_data:
        logger.error(f"[!] evcManagementCardData not found in response")
        return None
    
    card_table = evc_data.get("cardDataTable", {})
    card_data_list = card_table.get("cardDataList", [])
    logger.info(f"[*] cardDataList length: {len(card_data_list)}")
    
    if not card_data_list:
        logger.error(f"[!] No card data found for reservation {reservation_id}")
        return None
    
    card_info = card_data_list[0]
    returned_res_id = card_info.get("reservationId", {}).get("label")
    logger.info(f"[*] API returned reservation ID: {returned_res_id}")
    logger.info(f"[*] Requested reservation ID: {reservation_id}")
    
    if returned_res_id != reservation_id:
        logger.error(f"[!] Reservation ID mismatch")
        return None
    
    activity_table = card_info.get("activityTable", {})
    activity_list = activity_table.get("cardActivityDataList", [])
    additional_rows = activity_table.get("additionalComputationRows", [])
    
    logger.info(f"[*] cardActivityDataList length: {len(activity_list)}")
    logger.info(f"[*] additionalComputationRows length: {len(additional_rows)}")
    
    # Parse transactions
    transactions = []
    for activity in activity_list:
        transactions.append({
            "auth_date": activity.get("authDate"),
            "posted_date": activity.get("postedDate"),
            "auth_code": activity.get("authCode"),
            "status": activity.get("statusOrDeclineReason"),
            "amount": activity.get("amount")
        })
    
    # Get remaining balance
    remaining_balance = card_info.get("remainingBalance")
    logger.info(f"[*] remaining_balance: {remaining_balance}")
    
    # Get original payout from additional computation rows
    original_payout = None
    for row in additional_rows:
        if row.get("label") == "Original payout":
            original_payout = row.get("value")
            break
    logger.info(f"[*] original_payout: {original_payout}")
    
    card_transaction_data = {
        "guest_name": card_info.get("guestName"),
        "reservation_id": returned_res_id,
        "check_in_date": card_info.get("checkInDate"),
        "transactions": transactions,
        "remaining_balance": remaining_balance,
        "original_payout": original_payout
    }
    
    logger.info(f"[+] Successfully fetched {len(transactions)} card transactions")
    return card_transaction_data


# ==============================================================================
# 8. UNIFIED DATA MERGING & CSV EXPORT
# ==============================================================================

def export_to_csv(reservations, output_dir=OUTPUT_DIR):
    """Exports reservations and payment details to a clean CSV."""
    try:
        output_file = os.path.join(output_dir, "Expedia_Final_Data.csv")
        headers = [
            "Guest name", "Reservation reference", "Confirmation number",
            "Check-in date", "Check-out date", "Room type / number booked",
            "Date the reservation was made", "Total booking amount",
            "Who collects payment?", "Virtual Card number", "Virtual Card expiry date",
            "Virtual Card CVV", "Virtual Card billing address", "Real Card number",
            "Real Card expiry date", "Real Card billing country", "Nightly rates (Summary)",
            "Subtotal", "Total payout", "Status", "Itinerary number", "Pricing model",
            "IATA / TIDS #", "Bedding request", "Rate plan code", "Rate plan name",
            "Guest count", "Cancellation policy", "Reservation history - Booking date",
            "Reservation history - Confirmation date", "Reservation history - Reconciliation channel",
            "Reservation history - Last notification date"
        ]
        rows = []
        for conf_code, res in reservations.items():
            card_type = res.get("Card Type")
            is_vcard = card_type == "Virtual Card"
            is_real = card_type == "Real Card"

            vcard_num = res.get("Card Number", "") if is_vcard else ""
            vcard_exp = res.get("Expires", "") if is_vcard else ""
            vcard_cvv = res.get("CVV", "") if is_vcard else ""
            vcard_addr_obj = res.get("Billing Address") if is_vcard else None
            vcard_address = ""
            if isinstance(vcard_addr_obj, dict):
                vcard_address = f"{vcard_addr_obj.get('addressLine1', '')}, {vcard_addr_obj.get('city', '')}, {vcard_addr_obj.get('postalCode', '')}"
            elif vcard_addr_obj:
                vcard_address = str(vcard_addr_obj)

            real_num = res.get("Card Number", "") if is_real else ""
            real_exp = res.get("Expires", "") if is_real else ""
            real_addr_obj = res.get("Billing Address") if is_real else None
            real_country = real_addr_obj.get("countryCode", "") if isinstance(real_addr_obj, dict) else (str(real_addr_obj) if real_addr_obj else "")

            nightly_rates_summary = " | ".join([f"{rate.get('date')}: {rate.get('priceAmount')} {rate.get('priceCurrency')}" for rate in res.get("Nightly rates", [])]) if res.get("Nightly rates") else ""
            cancel_policy = f"Type: {res.get('Cancellation Policy', {}).get('cancellationPolicyType')}, Window: {res.get('Cancellation Policy', {}).get('cancellationWindowInHours')}h" if res.get("Cancellation Policy") else ""
            history = res.get("Reservation History", {}) or {}

            rows.append([
                res.get("Guest", ""), res.get("Reservation", ""), res.get("Confirmation", ""),
                res.get("Check-in", ""), res.get("Check-out", ""), res.get("Room", ""),
                res.get("Booked on", ""), res.get("Booking amount", ""), res.get("Payment Information", ""),
                vcard_num, vcard_exp, vcard_cvv, vcard_address,
                real_num, real_exp, real_country,
                nightly_rates_summary, res.get("Subtotal", ""), res.get("Total payout", ""),
                res.get("Status", ""), res.get("Itinerary number", ""), res.get("Pricing model", ""),
                res.get("IATA/TIDS #", ""), res.get("Bedding request", ""), res.get("Rate plan code", ""),
                res.get("Rate plan name", ""), res.get("Guest count", ""), cancel_policy,
                history.get("bookedDateTime", ""), history.get("hotelConfirmationDateTime", ""),
                history.get("reconciliationSourceSystem", ""), history.get("lastNotificationDateTime", "")
            ])
        with open(output_file, "w", encoding="utf-8", newline='') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        logger.info(f"[+] CSV Exported: {output_file}")
    except Exception as e:
        logger.error(f"[!] Error during CSV export: {type(e).__name__} - {e}")


def format_to_custom_schema(final_dataset):
    """Maps the data_integrator output into the specific JSON schema."""
    hotel_id = final_dataset.get("Hotel", {}).get("Property ID", PROPERTY_ID)
    reservations_out = []
    
    res_data = final_dataset.get("Reservations", [])
    if isinstance(res_data, dict):
        res_list = list(res_data.values())
    else:
        res_list = res_data
        
    for res in res_list:
        if not isinstance(res, dict):
            continue
        # Guest name split
        full_name = res.get("Guest", "")
        parts = full_name.split(" ", 1)
        first_name = parts[0] if len(parts) > 0 else ""
        last_name = parts[1] if len(parts) > 1 else ""
        
        # Payment model logic
        pd = res.get("Payment Details", {})
        is_vc = pd.get("Card Type") == "Virtual Card"
        payment_model = "VC" if is_vc else "HOTEL_COLLECTS"
        
        # Card logic
        billing_addr = pd.get("Billing Address")
        b_addr = {
            "addressLine1": None,
            "addressLine2": None,
            "city": None,
            "postalCode": None,
            "province": None,
            "countryCode": None
        }
        if isinstance(billing_addr, dict):
            b_addr["addressLine1"] = billing_addr.get("addressLine1")
            b_addr["addressLine2"] = billing_addr.get("addressLine2")
            b_addr["city"] = billing_addr.get("city")
            b_addr["postalCode"] = billing_addr.get("postalCode")
            b_addr["province"] = billing_addr.get("province")
            b_addr["countryCode"] = billing_addr.get("countryCode")
        
        c_num = str(pd.get("Card Number", ""))
        last4 = c_num[-4:] if len(c_num) >= 4 else c_num
        
        # Dates and nights
        arr = res.get("Check-in", "")
        dep = res.get("Check-out", "")
        nights = 1
        try:
            arr_d = datetime.strptime(arr, "%Y-%m-%d")
            dep_d = datetime.strptime(dep, "%Y-%m-%d")
            nights = (dep_d - arr_d).days
        except Exception:
            pass
            
        # Amounts
        try:
            gross = float(pd.get("Subtotal", 0) or 0)
        except:
            gross = pd.get("Subtotal")
            
        try:
            net = float(pd.get("Total payout", 0) or 0)
        except:
            net = pd.get("Total payout")
        
        nightly_map = {}
        tax_detail_arr = []
        discount_arr = []
        tax_detail_list = []
        
        all_amounts = pd.get("All Amounts", [])
        if not all_amounts:
            all_amounts = pd.get("Nightly rates", [])
            
        # First pass: Setup nights
        for n in all_amounts:
            t = str(n.get("type", "")).upper()
            dt = n.get("date") or n.get("startDate")
            try:
                val = float(n.get("costAmount", 0) or n.get("priceAmount", 0) or 0)
            except: val = 0.0
            
            if t == "DAILY_RATE" or t == "ROOM_RATE":
                if dt not in nightly_map:
                    nightly_map[dt] = {"base": 0.0, "tax": 0.0, "fee": 0.0, "details": []}
                nightly_map[dt]["base"] += val
                
                # Check nested items
                nested = n.get("taxesAndFees") or n.get("taxes") or n.get("lineItems") or []
                for tax_item in nested:
                    try:
                        t_val = float(tax_item.get("costAmount", 0) or tax_item.get("amount", 0) or 0)
                        t_raw_name = tax_item.get("name") or tax_item.get("description") or tax_item.get("type", "")
                        t_name = str(t_raw_name).lower()
                        if "fee" in t_name:
                            nightly_map[dt]["fee"] += t_val
                        elif "tax" in t_name:
                            nightly_map[dt]["tax"] += t_val
                        else:
                            nightly_map[dt]["details"].append({
                                "description": t_raw_name,
                                "amount": t_val
                            })
                    except: pass

        # Second pass: Flat items
        total_obj = next((x for x in all_amounts if str(x.get("type", "")).upper() == "TOTAL"), None)
        if total_obj:
            try:
                gross = float(total_obj.get("costAmount", 0))
            except: pass
            try:
                commission = float(total_obj.get("commisionAmount", 0))
                net = round(gross - commission, 2)
                commission_pct = round((commission / gross) * 100, 2) if gross > 0 else 0.0
                commission = -commission  # Typically negative expense in output
            except: pass
        else:
            try:
                ret = pd.get("Amount retained by Expedia Group")
                if ret is not None:
                    commission = float(ret)
                else:
                    commission = round(gross - net, 2)
                    
                if commission > 0:
                    commission = -commission
                commission_pct = round((abs(commission) / gross) * 100, 2) if gross > 0 else 0.0
            except:
                commission = 0.0
                commission_pct = 0.0

        for n in all_amounts:
            t = str(n.get("type", "")).upper()
            dt = n.get("date") or n.get("startDate")
            try:
                val = float(n.get("costAmount", 0) or n.get("priceAmount", 0) or 0)
            except: val = 0.0
            
            if t not in ["DAILY_RATE", "ROOM_RATE"]:
                desc = str(n.get("description") or n.get("name") or t)
                # Add to tax_detail_arr for summary level
                tax_detail_arr.append({
                    "description": desc,
                    "amount": val
                })

        # Ensure we have the basic summary totals in tax_detail if they were missing from lineItems
        added_descs = [x["description"].lower() for x in tax_detail_arr]
        
        if not any("subtotal" in d or "sub_total" in d for d in added_descs):
            tax_detail_arr.append({"description": "Subtotal", "amount": gross})
            
        if commission != 0 and not any("expedia" in d or "retained" in d or "commission" in d for d in added_descs):
            tax_detail_arr.append({"description": "Amount retained by Expedia Group", "amount": commission})
            
        if net > 0 and not any("payout" in d or "net" in d for d in added_descs):
            tax_detail_arr.append({"description": "Your total payout", "amount": net})

        # Build final nightly array - merge with detailed per-night tax/fee from GraphQL breakdown
        nightly_breakdown = res.get("Nightly Breakdown", {})  # {"2026-09-01": {"tax": 12.28, "fee": 5.00}}
        nightly_arr = []
        for dt, amounts_dict in sorted(nightly_map.items()):
            if not dt: continue
            b = round(amounts_dict["base"], 2)
            # Prefer detailed per-night breakdown from GraphQL if available; fallback to 0.0
            bd = nightly_breakdown.get(dt, {})
            b = round(bd.get("base", b), 2)
            t = round(bd.get("tax", amounts_dict["tax"]), 2)
            f = round(bd.get("fee", amounts_dict["fee"]), 2)
            nightly_arr.append({
                "stay_date": dt,
                "base_amount": b,
                "tax_amount": t,
                "fee_amount": f,
                "total_amount": round(b + t + f, 2),
                "currency_code": "USD"
            })
            if "taxes" in bd:
                tax_detail_list.extend(bd["taxes"])

        # Override summary amounts using the completely accurate nightly breakdown if available
        if nightly_breakdown:
            b_sum = round(sum(n["base_amount"] for n in nightly_arr), 2)
            t_sum = round(sum(n["tax_amount"] for n in nightly_arr), 2)
            f_sum = round(sum(n["fee_amount"] for n in nightly_arr), 2)
            
            # Recalculate accurately
            net = round(b_sum + t_sum + f_sum, 2)
            commission = f_sum
            gross = round(b_sum, 2)  # Raw Nightly rates before any promotion or tax
            commission_pct = round((commission / gross) * 100, 2) if gross > 0 else 0.0
            
            # Rebuild tax_detail_arr to exactly match the Expedia UI
            tax_detail_arr = []
            tax_detail_arr.append({"description": "Subtotal", "amount": net})
            
            # Add any specific summary items (promotions, fees) from GraphQL
            summary_items = nightly_breakdown.get("__summary_items__", [])
            for item in summary_items:
                desc_lower = item["description"].lower()
                if not any(kw in desc_lower for kw in ["payout", "retained by expedia", "amount retained", "subtotal", "payment details"]):
                    tax_detail_arr.append(item)
                    if item["amount"] < 0 or "promotion" in desc_lower or "discount" in desc_lower:
                        discount_arr.append({"label": item["description"], "amount": item["amount"]})
            
            if commission != 0:
                tax_detail_arr.append({"description": "Amount retained by Expedia Group", "amount": commission})
            tax_detail_arr.append({"description": "Your total payout", "amount": net})
            
        cp = res.get("Cancellation Policy", {})
        
        last_modified = None
        history = res.get("Reservation History", {})
        if isinstance(history, dict):
            last_modified = history.get("lastUpdatedDateTime") or history.get("lastNotificationDateTime")
        
        raw_exp = pd.get("Expires", "")
        if isinstance(raw_exp, dict):
            y = raw_exp.get('year', '')
            m = str(raw_exp.get('month', '')).zfill(2)
            exp_str = f"{y}-{m}" if y and m else str(raw_exp)
        elif isinstance(raw_exp, str) and "/" in raw_exp:
            parts = raw_exp.split("/")
            if len(parts) == 2:
                exp_str = f"{parts[1].strip()}-{str(parts[0].strip()).zfill(2)}"
            else:
                exp_str = raw_exp
        else:
            exp_str = str(raw_exp)
        input_card = res.get("card", {})
            
        # Parse credit_limit into numeric value
        cl_raw = input_card.get("credit_limit") or input_card.get("original_payout") or (net if is_vc else None)
        if isinstance(cl_raw, str) and cl_raw:
            # e.g. "USD 73.10" -> numeric 73.10
            import re as _re
            cl_match = _re.search(r'[\d.]+', cl_raw.replace(',', ''))
            cl_numeric = float(cl_match.group()) if cl_match else None
        elif isinstance(cl_raw, (int, float)):
            cl_numeric = float(cl_raw)
        else:
            cl_numeric = None

        # Parse card transactions: normalize txn_ts to ISO, add txn_type
        raw_txns = input_card.get("card_transactions") or pd.get("Transactions") or []
        normalized_txns = []
        for txn in raw_txns:
            # Parse human-readable date to ISO
            txn_date_raw = txn.get("auth_date") or txn.get("txn_ts") or ""
            txn_ts_iso = None
            if txn_date_raw and txn_date_raw not in ("N/A", ""):
                try:
                    from datetime import datetime as _dt2
                    txn_ts_iso = _dt2.strptime(txn_date_raw.strip(), "%b %d, %Y").strftime("%Y-%m-%dT00:00:00Z")
                except Exception:
                    txn_ts_iso = txn_date_raw
            # Determine txn_type from status
            txn_status = str(txn.get("status", "")).upper()
            txn_amount_str = str(txn.get("amount", ""))
            try:
                import re as _re2
                txn_amt = float(_re2.search(r'[\d.]+', txn_amount_str.replace(',','') or '0').group())
            except Exception:
                txn_amt = 0.0
            if "DECLINED" in txn_status:
                txn_type = "AUTH"
            elif txn.get("posted_date") and txn.get("posted_date") != "N/A":
                txn_type = "CAPTURE"
            elif "APPROVED" in txn_status:
                txn_type = "AUTH"
            else:
                txn_type = "AUTH"
            normalized_txns.append({
                "vc_txn_id": txn.get("vc_txn_id") or txn.get("id"),
                "txn_ts": txn_ts_iso,
                "auth_date": txn.get("auth_date"),
                "posted_date": txn.get("posted_date"),
                "auth_code": txn.get("auth_code"),
                "txn_type": txn_type,
                "status": txn.get("status"),
                "amount": txn_amt,
                "decline_reason": txn.get("decline_reason")
            })

        card_obj = {
            "card_type": input_card.get("card_type") or pd.get("Card Type") or res.get("Card Type"),
            "card_number": pd.get("Card Number", ""),
            "last4": last4,
            "cvv": str(pd.get("CVV", "")),
            "expiration_date": exp_str,
            "billing_address": b_addr,
            "vc_id": None,
            "activation_date": None,
            "credit_limit": cl_numeric,
            "currency": pd.get("Nightly rates", [{}])[0].get("priceCurrency", "USD") if pd.get("Nightly rates") else "USD",
            "charge_window_start": None,
            "charge_window_end": pd.get("Charge Before"),
            "status": pd.get("Card Status") or ("ACTIVE" if is_vc else None),
            "card_transactions": normalized_txns,
            "remaining_balance": input_card.get("remaining_balance") or pd.get("Remaining Balance"),
            "raw_data": pd.get("EVC Raw")
        }

        raw_chat = res.get("Messages", [])
        clean_chat = []
        for msg_idx, msg in enumerate(raw_chat):
            sender_raw = msg.get("creatorRole") or msg.get("creatorName") or "Unknown"
            ts_raw = msg.get("createDateTimeISOString") or msg.get("timestamp") or ""
            body = msg.get("body") or msg.get("message") or ""
            # Map Expedia sender roles to canonical direction
            sender_upper = str(sender_raw).upper()
            if "TRAVELER" in sender_upper or "GUEST" in sender_upper:
                direction = "INBOUND"
                sender_type = "GUEST"
            elif "HOTELIER" in sender_upper or "PARTNER" in sender_upper or "HOTEL" in sender_upper:
                direction = "OUTBOUND"
                sender_type = "HOTEL"
            else:
                direction = "OUTBOUND"  # scheduled/automated messages are outbound
                sender_type = sender_raw
            clean_chat.append({
                "message_id": f"MSG-{res.get('Reservation', '')}-{msg_idx+1}",
                "sender_type": sender_type,
                "direction": direction,
                "sent_ts": ts_raw,
                "message_text": body,
                "attachment_url": msg.get("attachmentUrl") or msg.get("attachment_url")
            })
        
        # Translation map for Expedia's internal penalty constants to readable UI strings
        penalty_i18n = {
            "1stNightRoomAndTax": "1 Night Stay + Taxes",
            "FirstNightRoomAndTax": "1 Night Stay + Taxes",
            "FullCostOfStay": "100% cost of stay",
            "20PercentCostOfStay": "20% cost of stay",
            "40PercentCostOfStay": "40% cost of stay",
            "60PercentCostOfStay": "60% cost of stay",
            "80PercentCostOfStay": "80% cost of stay",
            "100PercentCostOfStay": "100% cost of stay",
            "None": "None"
        }

        # Determine cancellation text if explicit description is missing
        cancel_text = cp.get("description") or cp.get("text") or cp.get("cancelPolicyDescription") or res.get("cancelPolicyDescription", "")
        if not cancel_text:
            window = cp.get("cancellationWindowInHours")
            if window is not None:
                outside_raw = cp.get("outsideWindowPenalties", {}).get("cancelPenaltyPerStayFee", "None")
                inside_raw = cp.get("insideWindowPenalties", {}).get("cancelPenaltyPerStayFee", "None")
                
                # Map raw constants using our i18n dictionary, fallback to the raw string if unknown
                outside = penalty_i18n.get(outside_raw, outside_raw)
                inside = penalty_i18n.get(inside_raw, inside_raw)
                
                cancel_text = f"More than {window} hours prior to arrival: {outside}. Less than {window} hours prior to arrival: {inside}."

        # Status mapping: Expedia BOOKED -> CONFIRMED, keep CANCELLED/NO_SHOW
        raw_status = (res.get("Status") or "BOOKED").upper()
        status_map = {
            "BOOKED": "CONFIRMED",
            "CONFIRMED": "CONFIRMED",
            "CANCELLED": "CANCELLED",
            "CANCELED": "CANCELLED",
            "NO_SHOW": "NO_SHOW",
            "STAYED": "STAYED"
        }
        mapped_status = status_map.get(raw_status, raw_status)

        # Compute actual timestamp for free_cancel_until_ts
        cancel_ts_computed = None
        window = cp.get("cancellationWindowInHours")
        if window is not None and arr:
            try:
                arr_dt = datetime.strptime(arr, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                calc_dt = arr_dt - timedelta(hours=window)
                cancel_ts_computed = calc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass

        # Compute numerical penalty_amount
        penalty_pct = cp.get("percent", 100) if cp.get("percent") is not None else 100
        penalty_amount_computed = round(gross * (penalty_pct / 100.0), 2)

        formatted_res = {
            "ota_res_id": str(res.get("Reservation", "")),
            "hotel_confirmation": str(res.get("Confirmation", "")),
            "status": mapped_status,
            "guest": {
                "guest_id": res.get("Reservation", ""),  # Use reservation ID as guest ID since Expedia doesn't provide one
                "first_name": first_name,
                "last_name": last_name,
                "email": pd.get("Email"),
                "phone": pd.get("Phone"),
                "country_code": res.get("Guest Country") or (b_addr.get("countryCode") if not is_vc else None),
                "language_code": pd.get("Language")
            },
            "stay": {
                "arrival_date": arr,
                "departure_date": dep,
                "nights": nights,
                "room_type_code": res.get("Room", ""),
                "rate_plan_code": res.get("Rate plan code", "")
            },
            "payment_model": payment_model,
            "card": {
                "card_type": input_card.get("card_type") or pd.get("Card Type") or res.get("Card Type"),
                "card_last4": last4,
                "expiration_date": exp_str,
                "vc_id": None,
                "credit_limit": cl_numeric,
                "currency": pd.get("Nightly rates", [{}])[0].get("priceCurrency", "USD") if pd.get("Nightly rates") else "USD",
                "activation_date": None,
                "charge_window_start": None,
                "charge_window_end": pd.get("Charge Before"),
                "vc_status": pd.get("Card Status") or ("ACTIVE" if is_vc else None),
                "vc_transactions": normalized_txns,
                "remaining_balance": input_card.get("remaining_balance") or pd.get("Remaining Balance"),
                "raw_data": pd.get("EVC Raw")
            },
            "no_show_flag": mapped_status == "NO_SHOW",
            "booking_ts": _force_utc_iso(res.get("Booked on", "")),
            "cancel_ts": _force_utc_iso(res.get("Cancellation Date") or res.get("cancelDate") or res.get("cancellationDate") or (res.get("Reservation History", {}) or {}).get("cancelledDateTime")),
            "last_modified_ts": _force_utc_iso(last_modified),
            "amounts": {
                "gross_amount": gross,
                "commissionable_amount": gross,
                "commission_pct": commission_pct,
                "commission_amount": commission,
                "net_amount": net,
                "currency_code": pd.get("Nightly rates", [{}])[0].get("priceCurrency", "USD") if pd.get("Nightly rates") else "USD"
            },
            "cancellation_policy": {
                "free_cancel_until_ts": cancel_ts_computed,
                "penalty_type": cp.get("cancellationPolicyType", ""),
                "penalty_amount": penalty_amount_computed,
                "penalty_percent": penalty_pct,
                "currency_code": pd.get("Nightly rates", [{}])[0].get("priceCurrency", "USD") if pd.get("Nightly rates") else "USD",
                "policy_text": cancel_text
            },
            "nightly": nightly_arr,
            "tax_detail": tax_detail_list,
            "chat": clean_chat
        }
        reservations_out.append(formatted_res)
        
    return {
        "hotel_id": str(hotel_id),
        "reservations": reservations_out
    }


def merge_all_data(reservations, invoices, competitors, post_stay_insights, post_stay_reviews, feedback, hotel_info=None, start_date=None, end_date=None, output_dir=OUTPUT_DIR):
    """Compiles the master comprehensive JSON dataset inline, bypassing data_integrator."""
    try:
        hotel_obj = hotel_info if hotel_info else {
            "Property ID": str(PROPERTY_ID),
            "Name": f"Property #{PROPERTY_ID}"
        }
        
        # Consolidate top-level payment fields into Payment Details directly
        for r_key, r_obj in reservations.items():
            if "Payment Details" not in r_obj or not isinstance(r_obj["Payment Details"], dict): 
                r_obj["Payment Details"] = {}
                
            payment_keys = ["Card Type", "Card Number", "Expires", "CVV", "Billing Address", "Payment Information", "Nightly rates", "Subtotal", "Total payout"]
            for p_key in payment_keys:
                if p_key in r_obj and r_obj[p_key] is not None:
                    r_obj["Payment Details"][p_key] = r_obj.pop(p_key)

        final_dataset = {
            "Hotel": hotel_obj,
            "Scraped Date Range": {
                "Filter": "checkIn",
                "Start Date": start_date,
                "End Date": end_date,
                "Scraped Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "Reservations": reservations,
            "Competitor Market Data": [],
            "Post Stay Insights": {}
        }

        all_data_path = os.path.join(output_dir, 'all_data.json')
        formatted_dataset = format_to_custom_schema(final_dataset)
        with open(all_data_path, 'w', encoding='utf-8') as f:
            json.dump(formatted_dataset, f, indent=4, ensure_ascii=False)

        # Recalculate accurate stats directly from the final formatted dataset
        actual_total = len(formatted_dataset.get("reservations", []))
        actual_chats = sum(1 for r in formatted_dataset.get("reservations", []) if r.get("chat") and len(r.get("chat")) > 0)
        actual_reviews = sum(1 for r in formatted_dataset.get("reservations", []) if r.get("post_stay_reviews"))
        actual_feedback = sum(1 for r in formatted_dataset.get("reservations", []) if r.get("in_house_feedback"))
        actual_unmatched = sum(1 for r in formatted_dataset.get("reservations", []) if r.get("status", "").startswith("UNKNOWN"))

        logger.info(f"[+] Comprehensive JSON saved to: {all_data_path}")
        logger.info(f"    - Hotel: {hotel_obj.get('Name')} (ID: {hotel_obj.get('Property ID')})")
        logger.info(f"    - Date Range: {start_date} to {end_date}")
        logger.info(f"    - Total Reservations: {actual_total}")
        logger.info(f"    - Matched Messages: {actual_chats}")
        return final_dataset
    except Exception as e:
        logger.error(f"[!] Error compiling master all_data.json: {type(e).__name__} - {e}")
    return None


# ==============================================================================
# 7. LOGOUT & SESSION CLEANUP
# ==============================================================================

def logout(session, cookie_str):
    """Gracefully terminates and invalidates the session on Expedia Partner Central."""
    logger.info("[*] Logging out of Expedia Partner Central...")
    logout_urls = [
        "https://www.expediapartnercentral.com/Account/SignOut",
        "https://apps.expediapartnercentral.com/lodging/sso/logout",
        "https://www.expediapartnercentral.com/account/logoff"
    ]
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "cookie": cookie_str,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    for url in logout_urls:
        try:
            r = session.get(url, headers=headers, timeout=12)
            if r.status_code in (200, 302):
                logger.info(f"[+] Successfully signed out via {url} (HTTP {r.status_code})")
                break
        except Exception:
            pass


def build_date_chunks(start_date, end_date, chunk_days):
    from datetime import datetime, timedelta
    chunks = []
    current_start = datetime.strptime(start_date, "%Y-%m-%d")
    final_end = datetime.strptime(end_date, "%Y-%m-%d")
    while current_start <= final_end:
        current_end = current_start + timedelta(days=chunk_days - 1)
        if current_end > final_end:
            current_end = final_end
        chunks.append({
            "start": current_start.strftime("%Y-%m-%d"),
            "end": current_end.strftime("%Y-%m-%d")
        })
        current_start = current_end + timedelta(days=1)
    return chunks

# ==============================================================================
# 8. MAIN ORCHESTRATION PIPELINE
# ==============================================================================

def main():
    pipeline_start_time = time.time()
    logger.info("========================================")
    logger.info(" Expedia Master Scraper & Data Integrator")
    logger.info("========================================")
    
    # 1. Authenticate & Obtain Valid Cookies
    cookie_str = get_valid_cookies(property_id=PROPERTY_ID)
    if not cookie_str:
        logger.error("[!] Authentication failed. Could not obtain valid session cookies.")
        return
        
    session = primp.Client(impersonate="chrome", ca_cert_file=certifi.where(), verify=False)
    
    # Inject cookies into primp's internal cookie jar to allow automatic Set-Cookie handling on redirects
    cookie_dict = {}
    for item in cookie_str.split(';'):
        if '=' in item:
            k, v = item.split('=', 1)
            cookie_dict[k.strip()] = v.strip()
    session.set_cookies("https://apps.expediapartnercentral.com", cookie_dict)
    session.set_cookies("https://www.expediapartnercentral.com", cookie_dict)

    # --- Date range & Search ---
    _today = datetime.now()
    scraped_start = (_today - timedelta(days=SCRAPE_DAYS_BACK)).strftime("%Y-%m-%d")
    scraped_end   = _today.strftime("%Y-%m-%d")
    
    try:
        # 2. Fetch Reservations List
        reservations = []
        search_terms = EXPEDIA_SEARCH_IDS if EXPEDIA_SEARCH_IDS else [""]
        
        for term in search_terms:
            if term:
                logger.info(f"[*] Pipeline search mode for: '{term}'")
                res_list = fetch_reservations(session, cookie_str, property_id=PROPERTY_ID,
                                              start_date=scraped_start, end_date=scraped_end, search_term=term)
                if res_list:
                    reservations.extend(res_list)
            else:
                logger.info(f"[*] Overall reservation range:\n    {scraped_start} → {scraped_end}")
                logger.info(f"[*] Reservation chunk size: {RESERVATION_CHUNK_DAYS} days")
                chunks = build_date_chunks(scraped_start, scraped_end, RESERVATION_CHUNK_DAYS)
                logger.info(f"[*] Total reservation chunks: {len(chunks)}\n")
                
                for i, chunk in enumerate(chunks, 1):
                    logger.info(f"[Chunk {i}/{len(chunks)}] {chunk['start']} → {chunk['end']}")
                    try:
                        res_list = fetch_reservations(session, cookie_str, property_id=PROPERTY_ID,
                                                      start_date=chunk['start'], end_date=chunk['end'], search_term=None)
                        if res_list:
                            logger.info(f"    Found {len(res_list)} reservations")
                            reservations.extend(res_list)
                    except Exception as e:
                        logger.error(f"[!] Reservation chunk failed:\n    {chunk['start']} → {chunk['end']}")
                
                logger.info(f"[+] Total reservations before deduplication: {len(reservations)}")
                
        # Deduplicate reservations by Reservation ID just in case
        seen_ids = set()
        unique_reservations = []
        for res in reservations:
            res_id = res.get("Reservation")
            if res_id and res_id not in seen_ids:
                seen_ids.add(res_id)
                unique_reservations.append(res)
        reservations = unique_reservations

        if not reservations:
            logger.error("[!] Stopping pipeline due to empty reservations list.")
            return

        DEBUG_FAST_MESSAGES = False  # Set to False for production
        final_data = {}
        if DEBUG_FAST_MESSAGES:
            logger.debug("[*] DEBUG: Skipping slow card & pricing fetches to rapidly test Messages.")
            for res in reservations:
                conf_code = res.get("Confirmation")
                if conf_code:
                    final_data[conf_code] = res.copy()
        else:
            logger.info(f"[*] Fetching card & pricing details for {len(reservations)} reservations (parallel, up to 4 workers)...")
            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac

            def _fetch_one_res(res, idx):
                res_id = res.get("Reservation")
                conf_code = res.get("Confirmation")
                if not res_id or not conf_code:
                    return None, None, None
                # Stagger start: thread 0 → ~0s, thread 1 → ~0.8s, thread 2 → ~1.6s, etc. + random jitter
                stagger = (idx * 0.8) + random.uniform(0.1, 0.5)
                time.sleep(stagger)
                details = fetch_reservation_details(session, cookie_str, res_id)
                merged = res.copy()
                merged.update(details)
                
                # Fetch card transactions for virtual cards
                # Check card type from multiple sources
                card_obj = merged.get("card", {})
                card_type = card_obj.get("card_type", "")
                
                # Fallback: check Payment Details if card type is empty
                if not card_type:
                    pd_dict = merged.get("Payment Details", {})
                    card_type = pd_dict.get("Card Type") or ""
                
                # Fallback: check direct Card Type set by fetch_reservation_details
                if not card_type:
                    card_type = merged.get("Card Type") or ""
                
                # Fallback: check Pricing model (EVC = Expedia Virtual Card)
                if not card_type:
                    pricing_model = merged.get("Pricing model") or ""
                    if pricing_model.upper() in ("EVC", "NMC", "EXPEDIA_COLLECT", "EC"):
                        card_type = "Virtual Card"
                
                # Final fallback: try fetching EVC card data directly — if it succeeds, it's a VC
                if not card_type and session and cookie_str:
                    evc_data = fetch_evc_card_data(session, cookie_str, PROPERTY_ID, booking_id=res_id)
                    if evc_data and evc_data.get("Card Type") == "Virtual Card":
                        card_type = "Virtual Card"
                        merged.update(evc_data)  # pre-populate card fields from EVC response
                
                logger.info(f"    [{idx+1}/{len(reservations)}] Card Type: '{card_type}' for ResID={res_id}")
                
                # Check for virtual card (case-insensitive)
                is_virtual = bool(card_type) and "virtual" in card_type.lower()
                if is_virtual and session and cookie_str:
                    check_in = merged.get("Check-in")
                    if check_in:
                        logger.info(f"    [{idx+1}/{len(reservations)}] Fetching card transactions for ResID={res_id}...")
                        card_tx_data = fetch_card_transactions(session, cookie_str, res_id, check_in, PROPERTY_ID)
                        if card_tx_data:
                            # Verify reservation ID matches
                            returned_res_id = card_tx_data.get("reservation_id")
                            if str(returned_res_id) == str(res_id):
                                # Store transactions in card_obj and push it back into merged
                                card_obj["card_transactions"] = card_tx_data.get("transactions") or []
                                card_obj["remaining_balance"] = card_tx_data.get("remaining_balance")
                                card_obj["original_payout"] = card_tx_data.get("original_payout")
                                merged["card"] = card_obj  # ensure updated card_obj is in merged
                                logger.info(f"    [{idx+1}/{len(reservations)}] Added {len(card_obj['card_transactions'])} transactions")
                            else:
                                logger.info(f"    [{idx+1}/{len(reservations)}] Reservation ID mismatch: expected {res_id}, got {returned_res_id}")
                        else:
                            logger.info(f"    [{idx+1}/{len(reservations)}] No card transaction data returned")
                    else:
                        logger.info(f"    [{idx+1}/{len(reservations)}] No check-in date available")
                else:
                    logger.info(f"    [{idx+1}/{len(reservations)}] Skipping card transactions (not virtual card)")
                
                return conf_code, res_id, merged

            with _TPE(max_workers=4) as executor:
                futures = {executor.submit(_fetch_one_res, res, idx): res for idx, res in enumerate(reservations)}
                done = 0
                for future in _ac(futures):
                    done += 1
                    try:
                        conf_code, res_id, merged = future.result()
                        if conf_code and merged:
                            final_data[conf_code] = merged
                            logger.info(f"    [{done}/{len(reservations)}] Done ResID={res_id} (Conf={conf_code})")
                    except Exception as ex:
                        logger.error(f"    [{done}/{len(reservations)}] Error: {ex}")

        logger.info(f"[+] Successfully fetched and merged all {len(final_data)} reservation details!")
        time.sleep(random.uniform(0.5, 1.0))  # Reduced from 1.5-3.0s → 0.5-1.0s

        # 4. Fetch Messages & Match to Reservations
        logger.info("[*] Fetching messages only for active reservations...")
        
        # Helper function to explicitly search for a reservation's CID
        def fetch_cid_by_search(search_query):
            url = "https://apps.expediapartnercentral.com/graphql"
            headers = {
                "accept": "application/json, multipart/mixed",
                "content-type": "application/json",
                "cookie": cookie_str,
                "client-info": "partner-central-web,d668969128a5a0249bea879ddb5e72c6a7757ea2,us-west-2",
                "x-apollo-operation-name": "InteractionList",
                "x-apollo-operation-type": "query",
                "x-hcom-origin-id": "Supply.Inbox.v1",
                "x-page-id": "Supply.Inbox.v1"
            }
            payload = [{
                "operationName": "InteractionList",
                "variables": {
                    "propertyContext": { "propertyId": str(PROPERTY_ID) },
                    "filterInput": { "filters": [] },
                    "searchInput": search_query,
                    "sortInput": None,
                    "paginationInput": { "pageNumber": 1, "pageSize": 50 },
                    "context": {
                        "siteId": 2056, "locale": "en_US", "eapid": 1, "tpid": 101, "currency": "USD",
                        "device": { "type": "DESKTOP" },
                        "identity": { "duaid": "537a40d7-b662-4469-aae6-ebc920023ad4", "authState": "ANONYMOUS" },
                        "privacyTrackingState": "CAN_TRACK",
                        "debugContext": { "abacusOverrides": [] }
                    },
                    "clientDataInput": { "timezone": "America/Los_Angeles" }
                },
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "a537441ee7c49257874bb0eed1f52e687812e459ae970262a571016f108f770a"
                    }
                }
            }]
            try:
                r = session.post(url, headers=headers, json=payload, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    data_obj = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
                    data_body = data_obj.get("data") or {}
                    interactions = data_body.get("interactions", {}).get("interactionList", {}).get("content", {}).get("interactionList", [])
                    cids = [item.get("interactionId") for item in interactions if isinstance(item, dict) and item.get("interactionId")]
                    return cids[0] if cids else None
            except:
                pass
            return None

        matched_messages = 0
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        def fetch_chat_for_res(res_id):
            time.sleep(random.uniform(0.1, 0.5))
            cid = fetch_cid_by_search(res_id)
            if cid:
                chat_history, _ = fetch_chat_thread(session, cookie_str, cid, property_id=PROPERTY_ID)
                if chat_history and chat_history.get("Messages"):
                    return res_id, chat_history.get("Messages")
            return res_id, None
            
        res_ids_to_fetch = [str(res.get("Reservation")) for res in final_data.values() if res.get("Reservation")]
        logger.info(f"    [*] Searching messages for {len(res_ids_to_fetch)} active reservations (Max 10 workers)...")
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_resid = {executor.submit(fetch_chat_for_res, rid): rid for rid in res_ids_to_fetch}
            completed_count = 0
            for future in as_completed(future_to_resid):
                completed_count += 1
                logger.info(f"    [{completed_count}/{len(res_ids_to_fetch)}] Fetching targeted chats in parallel...")
                try:
                    res_id, messages = future.result()
                    if messages:
                        for conf_code, res in final_data.items():
                            if str(res.get("Reservation")) == res_id:
                                res["Messages"] = messages
                                matched_messages += 1
                                break
                except Exception as e:
                    pass
        logger.info("")

        logger.info(f"[+] Added/Matched {matched_messages} reservations with non-empty chat history!")
        time.sleep(random.uniform(0.5, 1.0))  # Reduced from 1.5-3.0s → 0.5-1.0s

        # 5. Fetch In-House Feedback
        feedback = []
        logger.info("[*] Running In-house Feedback Extractor...")
        feedback = fetch_feedback(session, cookie_str)
        feedback_index = {str(item.get("ReservationId")): item for item in feedback}
        matched_feedback = 0
        for conf_code, res in final_data.items():
            res_id = str(res.get("Reservation"))
            conf = str(res.get("Confirmation"))
            if res_id in feedback_index:
                f_item = feedback_index[res_id].copy()
                f_item.pop("ReservationId", None)
                res["In-House Feedback"] = f_item
                matched_feedback += 1
            elif conf in feedback_index:
                f_item = feedback_index[conf].copy()
                f_item.pop("ReservationId", None)
                res["In-House Feedback"] = f_item
                matched_feedback += 1
        logger.info(f"[+] Matched {matched_feedback} In-House Feedback items to reservations!")
        time.sleep(random.uniform(1.5, 3.0))

        # 6. Fetch Post Stay Reviews & Insights
        post_stay_reviews, post_stay_insights = [], {}
        logger.info("[*] Running Post Stay Reviews Extractor...")
        post_stay_reviews, post_stay_insights = fetch_post_stay_reviews(session, cookie_str, property_id=PROPERTY_ID)
        reviews_index = {str(item.get("ReservationId")): item for item in post_stay_reviews}
        matched_reviews = 0
        for conf_code, res in final_data.items():
            res_id = str(res.get("Reservation"))
            conf = str(res.get("Confirmation"))
            if res_id in reviews_index:
                r_item = reviews_index.pop(res_id).copy()
                r_item.pop("ReservationId", None)
                res["Post Stay Review"] = r_item
                matched_reviews += 1
            elif conf in reviews_index:
                r_item = reviews_index.pop(conf).copy()
                r_item.pop("ReservationId", None)
                res["Post Stay Review"] = r_item
                matched_reviews += 1

        logger.info(f"[+] Matched {matched_reviews} Post Stay Reviews to reservations!")
        time.sleep(random.uniform(1.5, 3.0))

        # 7. Fetch Competitors
        competitors = []
        logger.info("[*] Running Competitor Exporter...")
        competitors = fetch_competitors(session, cookie_str)
        time.sleep(random.uniform(1.5, 3.0))

        # 8. Fetch Invoices & Detailed Line Items
        invoices = []
        logger.info("[*] Running Invoices Extractor...")
        
        # Invoice filter configuration from environment variables
        invoice_start_date = os.environ.get("INVOICE_START_DATE", scraped_start)
        invoice_end_date = os.environ.get("INVOICE_END_DATE", scraped_end)
        invoice_chain_group = os.environ.get("INVOICE_CHAIN_GROUP", "false").strip().lower() in ("true", "1", "yes")
        
        invoices = fetch_invoices(
            session, 
            cookie_str, 
            property_id=PROPERTY_ID,
            start_date=invoice_start_date,
            end_date=invoice_end_date,
            chain_group_only=invoice_chain_group
        )
        if invoices:
            logger.info(f"[+] Found {len(invoices)} invoices.")
            if invoice_start_date or invoice_end_date:
                logger.info(f"    Filtered: {invoice_start_date} to {invoice_end_date}")
            if invoice_chain_group:
                logger.info(f"    Chain/Group invoices only: Yes")

        # 9. Save Module JSON Files
        try:
            res_out_path = os.path.join(OUTPUT_DIR, "reservations_output.json")
            with open(res_out_path, "w", encoding="utf-8") as f:
                json.dump(final_data, f, indent=4, ensure_ascii=False)
                
            if competitors:
                with open(os.path.join(OUTPUT_DIR, "competitors.json"), "w", encoding="utf-8") as f:
                    json.dump(competitors, f, indent=4, ensure_ascii=False)
                    
            if invoices:
                with open(os.path.join(OUTPUT_DIR, "invoices.json"), "w", encoding="utf-8") as f:
                    json.dump(invoices, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[!] Error saving module JSON files: {type(e).__name__} - {e}")

        # 10. Compile Consolidated Master JSON (CSV Export Disabled)
        # export_to_csv(final_data, output_dir=OUTPUT_DIR)
        hotel_info = fetch_hotel_info(session, cookie_str, property_id=PROPERTY_ID)
        merge_all_data(final_data, invoices, competitors, post_stay_insights, post_stay_reviews, feedback, hotel_info=hotel_info, start_date=scraped_start, end_date=scraped_end, output_dir=OUTPUT_DIR)

        elapsed_seconds = int(time.time() - pipeline_start_time)
        minutes = elapsed_seconds // 60
        seconds = elapsed_seconds % 60

        logger.info("========================================")
        logger.info(" Pipeline Completed Successfully!")
        logger.info(f" Total Time Taken: {minutes} minutes {seconds} seconds ({elapsed_seconds}s)")
        logger.info("========================================")

    finally:
        # Gracefully log out and invalidate the session
        logout(session, cookie_str)


def login_test_mode():
    """Test only the login flow without running the full scraper."""
    logger.info("=" * 60)
    logger.info(" LOGIN TEST MODE")
    logger.info("=" * 60)
    
    try:
        cookie_str = get_valid_cookies(property_id=PROPERTY_ID)
        if cookie_str:
            logger.info("\n" + "=" * 60)
            logger.info(" LOGIN TEST: SUCCESS")
            logger.info("=" * 60)
            logger.info(f"Authentication successful for property {PROPERTY_ID}")
            logger.info(f"Captured cookies: {len(cookie_str.split(';'))} cookies")
            return True
        else:
            logger.info("\n" + "=" * 60)
            logger.error(" LOGIN TEST: FAILED")
            logger.info("=" * 60)
            logger.error("Authentication failed - no cookies returned")
            return False
    except Exception as e:
        logger.info("\n" + "=" * 60)
        logger.error(" LOGIN TEST: FAILED")
        logger.info("=" * 60)
        logger.error(f"Error: {type(e).__name__} - {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Expedia Partner Central Scraper")
    parser.add_argument("--login-test", action="store_true", help="Run login test only")
    args = parser.parse_args()
    
    if args.login_test:
        success = login_test_mode()
        sys.exit(0 if success else 1)
    else:
        main()
