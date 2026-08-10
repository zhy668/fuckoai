#!/usr/bin/env python3
"""
ChatGPT 注册 + OAuth CPA 回调（最终版）
用法: python3 uc_signup.py
"""
import argparse, base64, json, os, re, select, shutil, signal, socket, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException

# ── 配置 ────────────────────────────────────────────────
API    = os.getenv("UC_SIGNUP_API_BASE", os.getenv("API_BASE", "http://127.0.0.1:3030"))
PROXY  = os.getenv("UC_SIGNUP_PROXY", os.getenv("BROWSER_PROXY", os.getenv("PROXY", ""))).strip()
ROOT   = Path(__file__).resolve().parent
MAX_RETRIES    = 3   # 每步最大重试次数
MAX_ERROR_REFRESH = 5  # 错误页刷新次数
PHONE_RETRY_LIMIT = int(os.getenv("UC_SIGNUP_PHONE_RETRIES", "0"))
SMS_TIMEOUT_SECONDS = int(os.getenv("UC_SIGNUP_SMS_TIMEOUT_SECONDS", "135"))
SMS_POLL_INTERVAL_SECONDS = int(os.getenv("UC_SIGNUP_SMS_POLL_INTERVAL_SECONDS", "10"))
PHONE_PASSWORD_PAGE_TIMEOUT = int(os.getenv("UC_SIGNUP_PHONE_PASSWORD_PAGE_TIMEOUT", "25"))

# 注册参数
PW   = os.getenv("SIGNUP_PASSWORD", "ChangeMe123456!")
NAME = os.getenv("SIGNUP_NAME", "Test User")
AGE  = os.getenv("SIGNUP_AGE", "18")
DISPLAY = os.getenv("UC_SIGNUP_DISPLAY", os.getenv("BROWSER_DISPLAY", ":1"))
TARGET_URL = os.getenv("UC_SIGNUP_TARGET_URL", "https://chatgpt.com/auth/login?intent=signup").strip()
MOCK_MODE = os.getenv("UC_SIGNUP_MOCK_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
def detect_chrome_binary():
    configured = os.getenv("UC_SIGNUP_CHROME_BINARY", os.getenv("CHROME_BINARY", "")).strip()
    if configured:
        return configured
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return "/usr/bin/google-chrome"

def detect_chrome_version(binary):
    configured = os.getenv("UC_SIGNUP_CHROME_VERSION", "").strip()
    if configured:
        try:
            return int(configured)
        except ValueError:
            pass
    try:
        out = subprocess.check_output([binary, "--version"], text=True, stderr=subprocess.STDOUT, timeout=5)
        m = re.search(r"(\d+)\.", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 149

def detect_chromedriver_binary():
    configured = os.getenv("UC_SIGNUP_CHROMEDRIVER", os.getenv("CHROMEDRIVER_PATH", "")).strip()
    if configured:
        return configured
    return shutil.which("chromedriver") or ""

CHROME_BINARY = detect_chrome_binary()
CHROME_VERSION = detect_chrome_version(CHROME_BINARY)
CHROMEDRIVER_BINARY = detect_chromedriver_binary()

# ── 工具函数 ────────────────────────────────────────────
def log(msg, level="info"):
    p = {"error":"❌","warn":"⚠️","info":"  "}.get(level,"  ")
    print(f"{p} [{datetime.now():%H:%M:%S}] {msg}", flush=True)

def api(method, path, body=None):
    url = f"{API}{path}"
    h = {"Accept": "application/json"}
    admin_password = os.getenv("UC_SIGNUP_ADMIN_PASSWORD", os.getenv("ADMIN_PASSWORD", "")).strip()
    if admin_password:
        h["X-Admin-Password"] = admin_password
    data = json.dumps(body).encode() if body else None
    if data: h["Content-Type"] = "application/json"
    try:
        resp = urlopen(Request(url, data=data, method=method, headers=h), timeout=30)
        return json.loads(resp.read().decode())
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace").strip()
        detail = raw
        if raw:
            try:
                payload = json.loads(raw)
                detail = payload.get("error") or payload.get("message") or raw
            except Exception:
                pass
        raise ApiError(f"{method} {path} HTTP {e.code}: {detail or e.reason}") from e
    except URLError as e:
        raise ApiError(f"{method} {path} 连接失败: {e.reason}") from e

# 加载 .env
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            key = k.strip()
            if key == "ADMIN_PASSWORD":
                os.environ.setdefault(key, v.strip())

API = os.getenv("UC_SIGNUP_API_BASE", os.getenv("API_BASE", API)).rstrip("/")
PROXY = os.getenv("UC_SIGNUP_PROXY", os.getenv("BROWSER_PROXY", os.getenv("PROXY", PROXY)))
PW = os.getenv("SIGNUP_PASSWORD", PW)
NAME = os.getenv("SIGNUP_NAME", NAME)
AGE = os.getenv("SIGNUP_AGE", AGE)
DISPLAY = os.getenv("UC_SIGNUP_DISPLAY", os.getenv("BROWSER_DISPLAY", DISPLAY))
TARGET_URL = os.getenv("UC_SIGNUP_TARGET_URL", TARGET_URL).strip()
MOCK_MODE = os.getenv("UC_SIGNUP_MOCK_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
CHROME_BINARY = detect_chrome_binary()
CHROME_VERSION = detect_chrome_version(CHROME_BINARY)
CHROMEDRIVER_BINARY = detect_chromedriver_binary()

class LocalAuthProxy:
    """Chrome-compatible local forwarder for authenticated upstream HTTP proxies."""

    def __init__(self, upstreamProxy):
        parsed = urlparse(upstreamProxy if "://" in upstreamProxy else f"http://{upstreamProxy}")
        if not parsed.hostname or not parsed.port:
            raise FatalError(f"代理地址无效: {upstreamProxy}")
        self.upHost = parsed.hostname
        self.upPort = int(parsed.port)
        self.authHeader = None
        if parsed.username is not None:
            token = base64.b64encode(f"{parsed.username}:{parsed.password or ''}".encode()).decode()
            self.authHeader = f"Basic {token}"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.stopFlag = False
        self.thread = threading.Thread(target=self._serve, name="local-auth-proxy", daemon=True)
        self.thread.start()

    @property
    def chromeProxy(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.stopFlag = True
        try:
            self.sock.close()
        except Exception:
            pass

    def _serve(self):
        while not self.stopFlag:
            try:
                self.sock.settimeout(1.0)
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        upstream = None
        try:
            client.settimeout(45)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > 65536:
                    return
            header, rest = data.split(b"\r\n\r\n", 1)
            requestLine = header.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
            parts = requestLine.split(" ")
            if len(parts) < 2:
                return
            method, target = parts[0].upper(), parts[1]
            upstream = socket.create_connection((self.upHost, self.upPort), timeout=45)
            upstream.settimeout(45)
            if method == "CONNECT":
                req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                if self.authHeader:
                    req += f"Proxy-Authorization: {self.authHeader}\r\n"
                req += "Proxy-Connection: Keep-Alive\r\n\r\n"
                upstream.sendall(req.encode())
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        return
                    resp += chunk
                status = resp.split(b"\r\n", 1)[0]
                if b" 200 " not in status:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                    return
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
                self._pipe(client, upstream)
                return

            lines = header.split(b"\r\n")
            outLines = [lines[0]]
            if self.authHeader:
                outLines.append(f"Proxy-Authorization: {self.authHeader}".encode())
            for line in lines[1:]:
                lower = line.lower()
                if lower.startswith(b"proxy-authorization:") or lower.startswith(b"proxy-connection:"):
                    continue
                outLines.append(line)
            upstream.sendall(b"\r\n".join(outLines) + b"\r\n\r\n" + rest)
            self._pipe(client, upstream)
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass
            if upstream is not None:
                try:
                    upstream.close()
                except Exception:
                    pass

    def _pipe(self, left, right):
        sockets = [left, right]
        while True:
            readable, _, broken = select.select(sockets, [], sockets, 60)
            if broken or not readable:
                return
            for sock in readable:
                other = right if sock is left else left
                data = sock.recv(16384)
                if not data:
                    return
                other.sendall(data)

# ── 异常类 ──────────────────────────────────────────────
class StepError(Exception):
    """可重试的步骤错误"""
    pass

class FatalError(Exception):
    """不可恢复的错误"""
    pass

class ApiError(Exception):
    """内部 API 调用失败"""
    pass

class PhoneRetry(Exception):
    """当前手机号不可用，需要同一邮箱换号重试"""
    def __init__(self, message, *, cancel_phone=False):
        super().__init__(message)
        self.cancel_phone = cancel_phone

# ── 主类 ────────────────────────────────────────────────
class SignupBot:
    def __init__(self, email=""):
        self.d = None
        self.requested_email = str(email or "").strip()
        self.localProxy = None
        self.phone = ""
        self.fullPhone = ""
        self.usedEmailCodes = set()

    def resolve_chrome_proxy(self):
        proxy = str(PROXY or "").strip()
        if not proxy:
            return ""
        parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
        if parsed.username is None:
            return proxy if "://" in proxy else f"http://{proxy}"
        self.localProxy = LocalAuthProxy(proxy if "://" in proxy else f"http://{proxy}")
        log(f"  localProxy={self.localProxy.chromeProxy} -> {parsed.hostname}:{parsed.port}")
        return self.localProxy.chromeProxy

    def launch(self):
        os.environ["DISPLAY"] = DISPLAY
        opts = uc.ChromeOptions()
        opts.binary_location = CHROME_BINARY
        args = ["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                "--lang=zh-CN","--window-size=1440,900"]
        chromeProxy = self.resolve_chrome_proxy()
        if chromeProxy:
            args.append(f"--proxy-server={chromeProxy}")
            log(f"  chromeProxy={chromeProxy}")
        for a in args:
            opts.add_argument(a)
        chromeKwargs = {
            "options": opts,
            "version_main": CHROME_VERSION,
            "browser_executable_path": CHROME_BINARY,
        }
        if CHROMEDRIVER_BINARY:
            chromeKwargs["driver_executable_path"] = CHROMEDRIVER_BINARY
            log(f"  chromedriver={CHROMEDRIVER_BINARY}")
        else:
            log("  chromedriver=auto-download (may fail on aarch64)", "warn")
        self.d = uc.Chrome(**chromeKwargs)
        log(f"  webdriver={self.d.execute_script('return navigator.webdriver')}")

    # ── 页面等待 ────────────────────────────────────────
    def wait_ready(self, timeout=10):
        """等页面完全加载"""
        try:
            WebDriverWait(self.d, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException: pass
        time.sleep(1)

    def wait_url_contains(self, keyword, timeout=30):
        """等 URL 包含关键字"""
        for _ in range(timeout):
            if keyword in self.d.current_url: return
            time.sleep(1)
        raise StepError(f"URL等待超时: {keyword} | url={self.d.current_url[:160]} | title={self.d.title}")

    def page_text(self):
        try:
            return str(self.d.execute_script("return document.body ? document.body.innerText : ''") or "")
        except Exception:
            return ""

    def wait_after_password(self, timeout=45):
        """密码提交后等待离开创建密码页；超时仍停在密码页则判定失败。"""
        deadline = time.time() + timeout
        last_url = ""
        while time.time() < deadline:
            self.wait_ready(timeout=2)
            last_url = self.d.current_url
            urlLower = last_url.lower()

            if "failed to create account" in self.page_text().lower():
                raise FatalError(
                    f"OpenAI 拒绝创建账号: title={self.d.title} url={last_url[:160]} text={self.page_text()[:220]}"
                )

            leftPasswordPage = "create-account/password" not in urlLower and not self.d.find_elements(
                By.CSS_SELECTOR, "input[name=new-password], input[autocomplete='new-password']"
            )
            if any(k in urlLower for k in (
                "contact-verification",
                "phone-verification",
                "email-verification",
                "about-you",
                "email-otp",
                "otp",
            )) or leftPasswordPage:
                if self.d.find_elements(By.CSS_SELECTOR, "input[name=name], input[name=age]"):
                    return "profile"
                if self.d.find_elements(By.CSS_SELECTOR, "input[name=code], input[autocomplete='one-time-code'], input[inputmode=numeric]"):
                    return "code-input"
                if leftPasswordPage or any(k in urlLower for k in (
                    "contact-verification",
                    "phone-verification",
                    "email-verification",
                    "otp",
                )):
                    return "url"

            if self.d.find_elements(By.CSS_SELECTOR, "input[name=code], input[autocomplete='one-time-code'], input[inputmode=numeric]"):
                return "code-input"

            if self.d.find_elements(By.CSS_SELECTOR, "input[name=name], input[name=age]"):
                return "profile"

            time.sleep(1)

        textLower = self.page_text().lower()
        if "create-account/password" in last_url.lower() or self.d.find_elements(
            By.CSS_SELECTOR, "input[name=new-password], input[autocomplete='new-password']"
        ):
            raise FatalError(
                f"密码提交后仍停留在创建密码页（请检查密码规则/网络挑战）: title={self.d.title} url={last_url[:160]} text={textLower[:180]}"
            )
        raise StepError(f"密码后页面等待超时 | url={last_url[:160]} | title={self.d.title}")

    def is_error_page(self):
        """检测是否错误页"""
        t = self.d.title
        return any(k in t for k in ("Oops","error","出错了","Something went wrong"))

    # ── 元素操作（带重试）────────────────────────────────
    def _find_button(self, text):
        """找到匹配的按钮元素"""
        # 精确匹配
        for b in self.d.find_elements(By.TAG_NAME, "button"):
            try:
                bt = (b.text or "").strip()
                if bt == text: return b
            except StaleElementReferenceException: continue
        # 包含匹配（排除 "Continue with xxx"）
        for b in self.d.find_elements(By.TAG_NAME, "button"):
            try:
                bt = (b.text or "").strip()
                if text in bt and not bt.startswith("Continue with"): return b
            except StaleElementReferenceException: continue
        return None

    def click(self, text, retries=MAX_RETRIES, refresh_on_fail=True):
        """点击按钮，带重试和刷新"""
        for attempt in range(retries):
            self.wait_ready()
            btn = self._find_button(text)
            if btn:
                try:
                    label = (btn.text or text or "").strip()[:50] or text
                    log(f"  点击: {label}")
                    try:
                        ActionChains(self.d).move_to_element(btn).click().perform()
                    except Exception:
                        try:
                            btn.click()
                        except Exception:
                            self.d.execute_script("arguments[0].click();", btn)
                    time.sleep(3)
                    return
                except Exception as e:
                    log(f"  点击失败: {e}", "warn")
            else:
                log(f"  未找到按钮: {text}", "warn")

            if attempt < retries - 1:
                if refresh_on_fail and attempt >= 1:
                    log(f"  刷新页面重试...", "warn")
                    self.d.refresh(); time.sleep(8)
                else:
                    time.sleep(2)
        raise StepError(f"点击失败(已重试{retries}次): {text}")

    def click_optional(self, text, wait_seconds=5):
        """点击可选按钮；不存在或点不了时跳过，不阻断流程。"""
        deadline = time.time() + wait_seconds
        last_error = None
        while True:
            self.wait_ready(timeout=2)
            btn = self._find_button(text)
            if btn:
                try:
                    log(f"  点击可选按钮: {btn.text.strip()[:50]}")
                    ActionChains(self.d).move_to_element(btn).click().perform()
                    time.sleep(3)
                    return True
                except Exception as e:
                    last_error = e

            if time.time() >= deadline:
                break
            time.sleep(1)

        if last_error:
            log(f"  可选按钮点击失败，跳过: {text} ({last_error})", "warn")
        else:
            log(f"  可选按钮不存在，跳过: {text}")
        return False

    def fill(self, selector, value, retries=MAX_RETRIES):
        """填输入框"""
        for attempt in range(retries):
            self.wait_ready()
            try:
                el = self.d.find_element(By.CSS_SELECTOR, selector)
                ActionChains(self.d).move_to_element(el).click().perform()
                time.sleep(0.2)
                try: el.clear()
                except: pass
                for ch in value: el.send_keys(ch); time.sleep(0.03)
                shown = "***" if ("password" in selector.lower() or value == PW) else value
                log(f"  填入: {shown}")
                return
            except Exception as e:
                if attempt == retries - 1:
                    raise StepError(f"填框失败: {selector}")
                time.sleep(2)

    def fill_any(self, selectors, value):
        """尝试多个选择器"""
        for sel in selectors:
            try: self.fill(sel, value); return
            except StepError: continue
        # 兜底：找任意 input
        for inp in self.d.find_elements(By.CSS_SELECTOR, "input:not([type=hidden]):not([type=submit])"):
            try:
                inputType = (inp.get_attribute("type") or "").lower()
                inputName = (inp.get_attribute("name") or "").lower()
                ActionChains(self.d).move_to_element(inp).click().perform()
                time.sleep(0.2)
                try: inp.clear()
                except: pass
                for ch in value: inp.send_keys(ch); time.sleep(0.03)
                shown = "***" if (inputType == "password" or "password" in inputName or value == PW) else value
                log(f"  填入(fb): {shown}")
                return
            except: pass
        raise StepError("找不到任何输入框")

    # ── SMS/邮箱轮询 ─────────────────────────────────────
    def poll_sms(self, phone):
        deadline = time.time() + SMS_TIMEOUT_SECONDS
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                r = api("GET", f"/api/phones/{phone}/code")
                code = r.get("status", {}).get("code")
                if code: return str(code)
            except: pass
            remaining = max(0, int(deadline - time.time()))
            if attempt == 1 or attempt % 3 == 0:
                log(f"  SMS 等待中，剩余约 {remaining}s")
            time.sleep(min(SMS_POLL_INTERVAL_SECONDS, max(1, deadline - time.time())))
        return None

    def poll_email(self, addr, excludeCodes=None):
        exclude = {str(c) for c in (excludeCodes or ())}
        exclude |= {str(c) for c in getattr(self, "usedEmailCodes", set())}
        for i in range(18):
            try:
                r = api("GET", f"/api/gmail/mail/latest?address={quote(str(addr), safe='')}")
                item = r.get("item", {}) or r.get("mail", {})
                txt = str(item.get("decodedText","")) + " " + str(item.get("decodedSubject",""))
                candidates = []
                if item.get("verificationCode"):
                    candidates.append(str(item["verificationCode"]))
                candidates.extend(re.findall(r"\b(\d{6})\b", txt))
                for code in candidates:
                    if code and code not in exclude:
                        if not hasattr(self, "usedEmailCodes"):
                            self.usedEmailCodes = set()
                        self.usedEmailCodes.add(code)
                        return code
            except: pass
            if i % 3 == 0: log(f"  邮箱 {i+1}/18")
            time.sleep(10)
        return None

    def prepare_email(self):
        if not self.requested_email:
            raise FatalError("未提供邮箱地址；Temp Mail 已移除，请先导入邮箱列表")
        if "@" not in self.requested_email:
            raise FatalError(f"邮箱格式无效: {self.requested_email}")
        return self.requested_email

    def close_browser(self):
        if self.d:
            try: self.d.quit()
            except: pass
            self.d = None
        if self.localProxy:
            try: self.localProxy.close()
            except: pass
            self.localProxy = None

    def cancel_phone(self, phone, reason=""):
        if not phone:
            return False
        try:
            result = api("POST", f"/api/phones/{phone}/cancel")
            warning = str(result.get("warning") or "").strip() if isinstance(result, dict) else ""
            if warning:
                log(f"  手机号 {phone} 取消已提交但上游暂不允许立即取消: {warning}", "warn")
            else:
                log(f"  已取消手机号 {phone}{'：' + reason if reason else ''}", "warn")
            return True
        except Exception as e:
            log(f"  取消手机号失败 {phone}: {e}", "warn")
            return False

    def is_signup_landing(self):
        textLower = self.page_text().lower()
        titleLower = str(self.d.title or "").lower()
        hasEmail = bool(
            self.d.find_elements(
                By.CSS_SELECTOR,
                "input[type=email], input[name=email], input[autocomplete=email], input[name=username]",
            )
        )
        looksLikeLanding = (
            "log in or sign up" in textLower
            or "get started" in titleLower
            or ("continue with google" in textLower and "continue with phone" in textLower)
        )
        return hasEmail and looksLikeLanding

    def is_continue_loading(self):
        """Detect Continue button stuck in spinner/loading state after email submit."""
        try:
            return bool(
                self.d.execute_script(
                    """
                    const isOauth = (t) => /^continue with\\b/i.test(t || '');
                    const buttons = Array.from(document.querySelectorAll('button'));
                    for (const b of buttons) {
                      const t = (b.innerText || b.textContent || '').trim();
                      if (isOauth(t)) continue;
                      const busy = (b.getAttribute('aria-busy') || '').toLowerCase() === 'true';
                      const disabled = !!(b.disabled || (b.getAttribute('aria-disabled') || '').toLowerCase() === 'true');
                      const hasSvg = !!b.querySelector('svg, [class*=\"spinner\" i], [class*=\"loading\" i], [role=\"progressbar\"]');
                      const looksContinue = !t || /^continue$/i.test(t) || hasSvg;
                      if (looksContinue && (busy || (disabled && hasSvg) || (hasSvg && !t))) return true;
                    }
                    return false;
                    """
                )
            )
        except Exception:
            return False

    def wait_after_email_submit(self, timeout=60):
        deadline = time.time() + timeout
        last_url = ""
        sawLoading = False
        loadingStartedAt = None
        while time.time() < deadline:
            self.wait_ready(timeout=2)
            last_url = self.d.current_url
            urlLower = last_url.lower()
            titleLower = str(self.d.title or "").lower()
            textLower = self.page_text().lower()

            if self.has_profile_inputs() or "about-you" in urlLower or "how old are you" in titleLower:
                return "profile"
            if self.d.find_elements(By.CSS_SELECTOR, "input[name=new-password], input[autocomplete='new-password']"):
                return "password"
            if (
                "email-verification" in urlLower
                or "check your inbox" in titleLower
                or "check your inbox" in textLower
                or self.has_code_input()
            ):
                return "email-code"
            if self.needs_phone_verification():
                return "phone"

            loading = self.is_continue_loading()
            if loading:
                if not sawLoading:
                    log("  Continue 提交中（加载中）...")
                    loadingStartedAt = time.time()
                sawLoading = True
                # Hang protection: spinner longer than 35s on landing is almost always proxy/auth stall.
                if self.is_signup_landing() and loadingStartedAt and (time.time() - loadingStartedAt) >= 35:
                    raise FatalError(
                        f"邮箱提交卡住: Continue 一直加载 url={last_url[:180]} title={self.d.title}"
                    )
            elif sawLoading and self.is_signup_landing():
                # Loading ended but still on landing — likely soft failure; keep waiting a bit.
                pass

            if any(k in textLower for k in ("failed to create account", "something went wrong", "too many requests")):
                raise FatalError(
                    f"邮箱提交被拒绝: url={last_url[:180]} title={self.d.title} text={textLower[:220]}"
                )
            time.sleep(1)

        if sawLoading and self.is_signup_landing():
            raise FatalError(
                f"邮箱提交卡住: Continue 超时仍停在登录页 url={last_url[:180]} title={self.d.title}"
            )
        raise FatalError(
            f"邮箱提交后页面未知: url={last_url[:180]} title={self.d.title} text={self.page_text()[:220]}"
        )

    def wait_password_page(self, timeout=None):
        deadline = time.time() + (timeout or PHONE_PASSWORD_PAGE_TIMEOUT)
        last_url = ""
        while time.time() < deadline:
            self.wait_ready(timeout=2)
            last_url = self.d.current_url
            try:
                if self.d.find_elements(By.CSS_SELECTOR, "input[name=new-password], input[autocomplete='new-password']"):
                    return
            except Exception:
                pass
            time.sleep(1)
        raise FatalError(f"未进入创建密码页: {last_url[:160]} | title={self.d.title}")

    def has_code_input(self):
        return bool(self.d.find_elements(
            By.CSS_SELECTOR,
            "input[name=code], input[autocomplete='one-time-code'], input[name=otp]",
        ))

    def has_profile_inputs(self):
        if self.d.find_elements(By.CSS_SELECTOR, "input[name=name], input[name=age], input[name=birthday], input[autocomplete='name']"):
            return True
        titleLower = str(self.d.title or "").lower()
        textLower = self.page_text().lower()
        return "how old are you" in titleLower or "full name" in textLower or "about-you" in self.d.current_url.lower()

    def needs_phone_verification(self):
        urlLower = self.d.current_url.lower()
        textLower = self.page_text().lower()
        if self.d.find_elements(By.CSS_SELECTOR, "input[name=phoneNumberInput], input[type=tel]"):
            if any(k in urlLower for k in ("phone", "contact-verification", "add-phone", "verify")):
                return True
            if any(k in textLower for k in ("phone number", "verify your phone", "add a phone", "手机号", "验证手机")):
                return True
        if any(k in urlLower for k in ("phone-verification", "add-phone", "about-you/phone")):
            return True
        return False

    def looks_like_phone_otp(self):
        urlLower = self.d.current_url.lower()
        textLower = self.page_text().lower()
        if "email-verification" in urlLower or "check your inbox" in textLower:
            return False
        return any(k in urlLower for k in ("phone", "sms", "contact-verification")) or any(
            k in textLower for k in ("text message", "sent a code to +", "sms", "手机验证码")
        )

    def find_email_input(self):
        selectors = [
            "input[type=email]",
            "input[name=email]",
            "input[autocomplete=email]",
            "input[id=email]",
            "input[name=username]",
        ]
        for sel in selectors:
            els = self.d.find_elements(By.CSS_SELECTOR, sel)
            if els:
                return els[0]
        return None

    def enter_email_and_continue(self, email):
        selectors = [
            "input[type=email]",
            "input[name=email]",
            "input[autocomplete=email]",
            "input[id=email]",
            "input[name=username]",
        ]
        filled = False
        for sel in selectors:
            try:
                self.fill(sel, email)
                filled = True
                break
            except StepError:
                continue
        if not filled:
            self.fill_any(selectors, email)

        emailEl = self.find_email_input()
        if emailEl is not None:
            try:
                self.d.execute_script(
                    """
                    const el = arguments[0];
                    const value = arguments[1];
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                    if (setter) setter.call(el, value);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    """,
                    emailEl,
                    email,
                )
            except Exception:
                pass

        # Prefer plain Continue; never click Continue with phone here.
        self.click("Continue")
        time.sleep(2)
        if self.is_signup_landing() and not self.is_continue_loading() and emailEl is not None:
            log("  Continue 未进入加载，改用 Enter 提交", "warn")
            try:
                emailEl.send_keys(Keys.ENTER)
                time.sleep(2)
            except Exception as e:
                log(f"  Enter 提交失败: {e}", "warn")
        if self.is_signup_landing() and not self.is_continue_loading():
            btn = self._find_button("Continue")
            if btn is not None:
                log("  Continue 仍未加载，改用 JS click", "warn")
                try:
                    self.d.execute_script("arguments[0].click();", btn)
                    time.sleep(2)
                except Exception as e:
                    log(f"  JS click 失败: {e}", "warn")

    def fill_profile(self):
        def _fill():
            nameFilled = False
            for sel in ("input[name=name]", "input[autocomplete='name']", "input[placeholder*='name' i]"):
                try:
                    self.fill(sel, NAME)
                    nameFilled = True
                    break
                except StepError:
                    continue
            if not nameFilled:
                self.fill_any(["input[type=text]"], NAME)

            ageFilled = False
            for sel in ("input[name=age]", "input[name=birthday]", "input[autocomplete='bday']", "input[placeholder*='age' i]"):
                try:
                    self.fill(sel, AGE)
                    ageFilled = True
                    break
                except StepError:
                    continue
            if not ageFilled:
                self.fill_any(["input[type=number]", "input[inputmode=numeric]"], AGE)

            if self._find_button("Finish creating account"):
                self.click("Finish creating account")
            else:
                self.click("Continue")

        self._step("姓名年龄", _fill)
        time.sleep(8)
        log(f"✅ 注册完成: {self.d.title}")

    def purchase_phone(self):
        item = api("POST", "/api/purchase", {})["item"]
        phone = str(item["phoneNumber"])
        fullPhone = "+" + re.sub(r"\D", "", phone)
        log(f"  页面强制手机验证，临时买号: {phone}")
        return phone, fullPhone

    def submit_phone_number(self, fullPhone):
        if self.d.find_elements(By.CSS_SELECTOR, "input[name=phoneNumberInput]"):
            self.fill("input[name=phoneNumberInput]", fullPhone)
        else:
            self.fill_any(["input[type=tel]", "input[name=phone]", "input[autocomplete=tel]"], fullPhone)
        self.click("Continue")
        time.sleep(4)

    def handle_forced_phone(self):
        phone, fullPhone = self.purchase_phone()
        self.phone = phone
        self.fullPhone = fullPhone
        self._step("填强制手机号", lambda: self.submit_phone_number(fullPhone))
        if self.has_code_input() or self.looks_like_phone_otp():
            code = self.poll_sms(phone)
            if not code:
                raise PhoneRetry(f"短信验证码 {SMS_TIMEOUT_SECONDS}s 超时", cancel_phone=True)
            log(f"  SMS: {code}")
            self._step("短信验证", lambda: (
                self.fill_any(
                    ["input[name=code]", "input[autocomplete='one-time-code']", "input[inputmode=numeric]"],
                    code,
                ),
                self.click("Continue"),
            ))
            time.sleep(3)
        return phone, fullPhone

    def submit_verification_code(self, email):
        if self.looks_like_phone_otp():
            if not self.phone:
                self.handle_forced_phone()
                return
            code = self.poll_sms(self.phone)
            if not code:
                raise PhoneRetry(f"短信验证码 {SMS_TIMEOUT_SECONDS}s 超时", cancel_phone=True)
            log(f"  SMS: {code}")
        else:
            code = self.poll_email(email)
            if not code:
                raise FatalError("邮箱验证码超时")
            log(f"  邮箱码: {code}")
        self._step("提交验证码", lambda: (
            self.fill_any(
                ["input[name=code]", "input[autocomplete='one-time-code']", "input[inputmode=numeric]"],
                code,
            ),
            self.click("Continue"),
        ))
        time.sleep(3)

    def register_with_email(self, email):
        self.phone = ""
        self.fullPhone = ""
        log(f"📧 邮箱注册: {email}")

        self.launch()
        self.d.get(TARGET_URL)
        time.sleep(12)
        log(f"注册: {self.d.title}")

        self._step("Cookie", lambda: self.click_optional("Accept all"))

        stage = None
        lastSubmitError = None
        for attempt in range(1, 4):
            try:
                if attempt > 1:
                    log(f"  邮箱提交重试 {attempt}/3：重新打开注册页", "warn")
                    self.d.get(TARGET_URL)
                    time.sleep(10)
                    self.click_optional("Accept all")
                self._step("填邮箱", lambda: self.enter_email_and_continue(email))
                stage = self.wait_after_email_submit()
                break
            except FatalError as e:
                lastSubmitError = e
                msg = str(e)
                retryable = ("邮箱提交卡住" in msg) or ("邮箱提交后页面未知" in msg)
                if not retryable or attempt >= 3:
                    raise
                log(f"  {msg}", "warn")
        if stage is None:
            raise lastSubmitError or FatalError("邮箱提交失败")

        log(f"→ 邮箱后阶段: {stage} | {self.d.title}")
        if stage == "email-code":
            self.submit_verification_code(email)
            stage = self.wait_after_email_submit()
            log(f"→ 邮箱码后阶段: {stage} | {self.d.title}")
        if stage == "phone":
            self.handle_forced_phone()
            stage = self.wait_after_email_submit()
            log(f"→ 手机后阶段: {stage} | {self.d.title}")
        if stage == "profile":
            self.fill_profile()
            return self.phone, self.fullPhone
        if stage == "password":
            log(f"→ {self.d.title}")
            self._step("填密码", lambda: (
                self.fill("input[name=new-password]", PW),
                self.click("Continue"),
            ))
            afterPassword = self.wait_after_password()
            log(f"→ {self.d.title} ({afterPassword})")
        else:
            raise FatalError(
                f"邮箱流程未知阶段: {stage} url={self.d.current_url[:180]} title={self.d.title}"
            )

        for _ in range(6):
            if self.has_profile_inputs():
                self.fill_profile()
                return self.phone, self.fullPhone

            if self.needs_phone_verification():
                self.handle_forced_phone()
                continue

            if self.has_code_input():
                self.submit_verification_code(email)
                continue

            if "about-you" in self.d.current_url.lower():
                time.sleep(2)
                if self.has_profile_inputs():
                    self.fill_profile()
                    return self.phone, self.fullPhone

            break

        if self.has_profile_inputs():
            self.fill_profile()
            return self.phone, self.fullPhone

        raise FatalError(
            f"邮箱注册后未完成资料页: url={self.d.current_url[:180]} title={self.d.title} text={self.page_text()[:220]}"
        )

    # ── 步骤执行器（带错误恢复）──────────────────────────
    def _step(self, name, fn):
        """执行一个步骤，出错时刷新并从当前页重试"""
        for attempt in range(MAX_RETRIES):
            try:
                self.wait_ready()
                # 检查是否错误页
                if self.is_error_page():
                    log(f"  [{name}] 检测到错误页，刷新...", "warn")
                    self.d.refresh(); time.sleep(8)
                    continue
                fn()
                return
            except StepError as e:
                log(f"  [{name}] {e} (attempt {attempt+1}/{MAX_RETRIES})", "warn")
                if attempt < MAX_RETRIES - 1:
                    self.d.refresh(); time.sleep(8)
            except Exception as e:
                log(f"  [{name}] {e}", "error")
                raise
        raise FatalError(f"步骤 [{name}] 失败，已重试{MAX_RETRIES}次")

    def run_mock(self):
        email = self.prepare_email()
        log(f"本地 Mock 目标: {TARGET_URL}")
        code = self.poll_email(email)
        if not code:
            raise FatalError("Gmail 邮件码超时")
        log("✅ Gmail 邮件轮询成功（Mock 模式，不提交验证码、不创建账号）")
        return True

    # ── 主流程 ───────────────────────────────────────────
    def run(self):
        log("=" * 55)
        log("ChatGPT 注册 → OAuth → CPA 回调")
        log("=" * 55)

        phone = email = full_phone = ""
        completed_success = False
        try:
            if MOCK_MODE:
                return self.run_mock()
            if len(str(PW or "")) < 12:
                raise FatalError(f"SIGNUP_PASSWORD 长度不足12位（当前 {len(str(PW or ''))}），OpenAI 会拒绝并停留在创建密码页")
            # ═══ 准备 ═══
            email = self.prepare_email()
            phone = ""
            full_phone = ""
            try:
                phone, full_phone = self.register_with_email(email)
            except PhoneRetry as e:
                log(f"  强制手机验证失败: {e}", "warn")
                if e.cancel_phone and self.phone:
                    self.cancel_phone(self.phone, str(e))
                raise FatalError(str(e)) from e
            phone = self.phone or phone
            full_phone = self.fullPhone or full_phone

            # ═══ Part 2: OAuth / CPA 授权（同一浏览器，保持登录态）═══
            oa = api("GET", "/api/codex-oauth/url")
            oa_url = oa.get("url", "")
            oa_state = oa.get("state", "")
            log(f"🔗 OAuth: {oa_state}")

            self.d.get(oa_url)
            time.sleep(8)
            log(f"OAuth: {self.d.title} | {self.d.current_url[:80]}")

            url = self.d.current_url

            # 已登录 → 可能直接到 choose-account 或 consent
            if "choose-an-account" in url:
                self._step("选账户", lambda: self._click_account_button())

            elif "log-in" in url:
                # Prefer staying on current login page; only force email kind when needed.
                if full_phone and "phone" in url.lower():
                    self._step("OAuth手机号", lambda: (
                        self.fill_any(["input[type=tel]"], full_phone),
                        self.click("Continue"), time.sleep(5)
                    ))
                else:
                    if not self.d.find_elements(By.CSS_SELECTOR, "input[type=email], input[name=email], input[name=username]"):
                        self.d.get("https://auth.openai.com/log-in?usernameKind=email")
                        time.sleep(5)
                    self._step("OAuth邮箱", lambda: (
                        self.fill_any(["input[type=email]", "input[name=email]", "input[name=username]"], email),
                        self.click("Continue"), time.sleep(5)
                    ))
                log(f"  → {self.d.title}")

                if self.d.find_elements(By.CSS_SELECTOR, "input[type=password], input[name=current-password]"):
                    self._step("OAuth密码", lambda: (
                        self.fill_any(["input[type=password]", "input[name=current-password]"], PW),
                        self.click("Continue"), time.sleep(5)
                    ))
                    log(f"  → {self.d.title}")

            # 绑定邮箱（手机号账号场景）
            if "add-email" in self.d.current_url.lower():
                self._step("绑定邮箱", lambda: (
                    self.fill_any(["input[type=email]", "input[name=email]"], email),
                    self.click("Continue"), time.sleep(5)
                ))
                log(f"  → {self.d.title}")
                self.ensure_oauth_email_code_if_required(email)

            # 统一推进：邮箱OTP / 强制手机 / 授权同意 / 回调
            log("推进 OAuth 授权门禁...")
            callback_url = self.progress_oauth_until_callback(email)
            phone = self.phone or phone
            full_phone = self.fullPhone or full_phone

            if not callback_url:
                raise FatalError(
                    f"OAuth回调超时: url={self.d.current_url[:180]} title={self.d.title} text={self.page_text()[:220]}"
                )

            # CPA 回填
            log("📤 回填CPA...")
            result = api("POST", "/api/codex-oauth/callback",
                         {"provider": "codex", "redirect_url": callback_url})
            log(f"  回填: {json.dumps(result, ensure_ascii=False)[:200]}")

            status = api("GET", f"/api/codex-oauth/status?state={oa_state}")
            log(f"  状态: {json.dumps(status, ensure_ascii=False)[:200]}")

            files = api("GET", "/api/codex-oauth/files")
            log(f"  凭证: {json.dumps(files, ensure_ascii=False)[:500]}")

            # 清理
            if phone:
                try: api("POST", f"/api/phones/{phone}/finish")
                except: pass

            log("=" * 55)
            log(f"✅ 全部完成! {email}")
            completed_success = True
            return True

        except FatalError as e:
            log(f"💀 {e}", "error")
        except Exception as e:
            log(f"❌ {e}", "error")
        finally:
            if phone and not completed_success:
                self.cancel_phone(phone, "任务未完成")
            if self.d:
                try: self.d.save_screenshot("/tmp/uc_error.png")
                except: pass
            self.close_browser()
        return False

    def looks_like_email_otp(self):
        urlLower = self.d.current_url.lower()
        titleLower = str(self.d.title or "").lower()
        textLower = self.page_text().lower()
        if self.looks_like_phone_otp():
            return False
        return (
            "email-verification" in urlLower
            or "email-otp" in urlLower
            or "check your inbox" in titleLower
            or "check your inbox" in textLower
            or (
                self.has_code_input()
                and any(k in (urlLower + " " + textLower) for k in ("email", "inbox", "we sent", "sent a code"))
            )
        )

    def ensure_oauth_email_code_if_required(self, email):
        if not (self.looks_like_email_otp() or (self.has_code_input() and not self.looks_like_phone_otp() and "check your inbox" in str(self.d.title or "").lower())):
            return False
        log("OAuth 需要邮箱验证码，开始收信")
        self.submit_verification_code(email)
        time.sleep(3)
        return True

    def progress_oauth_until_callback(self, email, maxRounds=24):
        """Advance OAuth gates until localhost callback appears."""
        for roundIdx in range(1, maxRounds + 1):
            self.wait_ready(timeout=2)
            url = self.d.current_url
            titleLower = str(self.d.title or "").lower()
            urlLower = url.lower()

            if "localhost:1455" in url or ("code=" in url and "state=" in url):
                log(f"  ✅ 回调: {url[:120]}")
                return url

            if "choose-an-account" in urlLower:
                log(f"  OAuth 选账户 ({roundIdx})")
                self._click_account_button()
                continue

            if self.ensure_oauth_email_code_if_required(email):
                continue

            if self.ensure_oauth_phone_if_required():
                continue

            if self.d.find_elements(By.CSS_SELECTOR, "input[type=password], input[name=current-password]"):
                log(f"  OAuth 补填密码 ({roundIdx})")
                self.fill_any(["input[type=password]", "input[name=current-password]"], PW)
                self.click("Continue")
                time.sleep(3)
                continue

            if self.looks_like_email_otp() or self.has_code_input():
                # Avoid blind Continue on OTP pages.
                time.sleep(2)
                continue

            if self._find_button("Continue") and "phone number required" not in titleLower:
                log(f"授权页: {self.d.title}")
                self.click_optional("Continue", wait_seconds=4)
                time.sleep(2)
                continue

            time.sleep(2)
        return ""

    def ensure_oauth_phone_if_required(self):
        titleLower = str(self.d.title or "").lower()
        textLower = self.page_text().lower()
        urlLower = self.d.current_url.lower()
        if not (
            "phone number required" in titleLower
            or "phone number required" in textLower
            or "add-phone" in urlLower
            or self.needs_phone_verification()
        ):
            return False
        log("OAuth/CPA 强制要求手机号，开始临时买号")
        if not self.d.find_elements(By.CSS_SELECTOR, "input[name=phoneNumberInput], input[type=tel]"):
            self.click_optional("Continue", wait_seconds=5)
            time.sleep(2)
        self.handle_forced_phone()
        time.sleep(3)
        return True

    def _click_account_button(self):
        """choose-account 页面：点第一个账户"""
        for b in self.d.find_elements(By.TAG_NAME, "button"):
            try:
                bt = (b.text or "").strip()
                if "Select account" in bt or ("+" in bt and len(bt) > 10) or "@" in bt:
                    log(f"  点击: {bt[:60]}")
                    ActionChains(self.d).move_to_element(b).click().perform()
                    time.sleep(5)
                    return
            except: pass
        raise StepError("找不到账户按钮")

# ── 入口 ────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChatGPT 注册 + OAuth CPA 回调")
    parser.add_argument("--email", default="", help="指定本次注册使用的邮箱")
    parser.add_argument("--api-base", default="", help="本地 fuckoai API 地址")
    parser.add_argument("--proxy", default="", help="Chrome 代理地址")
    parser.add_argument("--display", default="", help="X11 DISPLAY")
    parser.add_argument("--chrome-binary", default="", help="Chrome 可执行文件路径")
    parser.add_argument("--chrome-version", type=int, default=0, help="Chrome 主版本号")
    args = parser.parse_args()

    if args.api_base:
        API = args.api_base.rstrip("/")
    if args.proxy:
        PROXY = args.proxy
    if args.display:
        DISPLAY = args.display
    if args.chrome_binary:
        CHROME_BINARY = args.chrome_binary
    if args.chrome_version:
        CHROME_VERSION = args.chrome_version

    signal.signal(signal.SIGINT, lambda s, f: sys.exit(1))
    bot = SignupBot(email=args.email)
    ok = bot.run()
    sys.exit(0 if ok else 1)
