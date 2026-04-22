"""
Indeed CV Downloader - Unified Script
Supports both Backend (API parallel) and Frontend (Selenium clicks) modes
Can process single job or all jobs automatically
"""

import os
import sys
import json
import time
import re
import base64
import shutil
import subprocess
from urllib.parse import urlparse, parse_qs, unquote, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException
from tqdm import tqdm
import platform
import traceback

# Load environment variables
load_dotenv('.env.config')


# Bumped whenever the binary layout changes. Shown in log headers so bug
# reports are pinned to a known build.
TOOL_VERSION = "2026-04-20-app-data-via-profile-urls"


class RunLogger:
    """Per-run log file under logs/ that captures everything useful for a
    bug report in one place. On exit we copy the active file to
    logs/latest.log so HR only has to send one file.

    Privacy: cookie values and JWT contents are never written; we log
    names and truthy/falsy presence only.
    """
    def __init__(self, log_folder: Path):
        self.log_folder = Path(log_folder)
        self.log_folder.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.path = self.log_folder / f'run_{ts}.log'
        self.latest_path = self.log_folder / 'latest.log'
        # Line-buffered so tailing works and a crash loses at most one line.
        self._f = open(self.path, 'w', encoding='utf-8', errors='replace', buffering=1)
        self._write_header()

    @property
    def raw_file(self):
        """File handle for _TeeStream to mirror stdout into."""
        return self._f

    def _write_header(self):
        self._write('INFO', '=' * 70)
        self._write('INFO', f'Indeed CV Downloader — run log')
        self._write('INFO', f'Tool version: {TOOL_VERSION}')
        self._write('INFO', f'Started:      {datetime.now().isoformat()}')
        self._write('INFO', f'Python:       {sys.version.split()[0]}')
        self._write('INFO', f'Platform:     {platform.platform()}')
        self._write('INFO', f'CWD:          {Path.cwd()}')
        self._write('INFO', '=' * 70)

    def info(self, msg: str):
        self._write('INFO', msg)

    def warn(self, msg: str):
        self._write('WARN', msg)

    def error(self, msg: str, exc: Optional[BaseException] = None):
        if exc is not None:
            tb = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            msg = f'{msg}\n{tb}'
        self._write('ERROR', msg)

    def event(self, name: str, data: Optional[dict] = None):
        """Structured event — machine-parseable for future dashboards.
        Dicts are serialized with json (default=str) so complex values
        (paths, exceptions) don't crash the logger."""
        if data is None:
            self._write('EVENT', name)
        else:
            try:
                payload = json.dumps(data, default=str, ensure_ascii=False)
            except Exception:
                payload = repr(data)
            self._write('EVENT', f'{name} {payload}')

    def _write(self, level: str, msg: str):
        try:
            ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            self._f.write(f'[{ts}] {level:5} {msg}\n')
        except Exception:
            pass

    def close(self):
        self._write('INFO', 'run_end')
        try:
            self._f.close()
        except Exception:
            pass
        # Mirror to latest.log — this is the file HR is told to send.
        try:
            shutil.copy2(self.path, self.latest_path)
        except Exception:
            pass


class _TeeStream:
    """Mirrors writes to both the real stdout (so the user still sees
    them live) and the run log file (so the .log includes everything
    the user saw). Only attached to stdout — stderr is left alone so
    tqdm progress bars don't flood the log with carriage-return noise."""
    def __init__(self, original, log_file):
        self._original = original
        self._log_file = log_file

    def write(self, data):
        try:
            self._original.write(data)
        except Exception:
            pass
        try:
            self._log_file.write(data)
        except Exception:
            pass

    def flush(self):
        for f in (self._original, self._log_file):
            try:
                f.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return self._original.isatty()
        except Exception:
            return False

    def __getattr__(self, name):
        return getattr(self._original, name)


def _probe_chrome_version() -> Optional[str]:
    """Best-effort Chrome version lookup. Helpful for "but it works on
    my machine" bug reports. Never raises."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]
    localappdata = os.environ.get('LOCALAPPDATA')
    if localappdata:
        candidates.insert(0, str(Path(localappdata) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'))
    for p in candidates:
        if Path(p).exists():
            try:
                out = subprocess.check_output(
                    [p, '--version'],
                    stderr=subprocess.STDOUT,
                    timeout=5,
                )
                return out.decode('utf-8', errors='replace').strip()
            except Exception:
                continue
    return None


class IndeedDownloader:
    def __init__(self, log: Optional[RunLogger] = None):
        # Per-run structured logger. See RunLogger. If main() didn't pass
        # one (e.g., a library caller), fall back to a no-op-ish shim so
        # self.log calls don't have to guard for None everywhere.
        self.log = log
        # Config from .env
        self.download_folder = os.getenv('DOWNLOAD_FOLDER', 'downloads')
        self.log_folder = os.getenv('LOG_FOLDER', 'logs')
        self.max_cvs = int(os.getenv('MAX_CVS', 3000))
        self.parallel_downloads = int(os.getenv('PARALLEL_DOWNLOADS', 10))
        self.download_delay = float(os.getenv('DOWNLOAD_DELAY', 0.5))
        self.next_candidate_delay = float(os.getenv('NEXT_CANDIDATE_DELAY', 1.0))

        # Create folders
        Path(self.download_folder).mkdir(exist_ok=True)
        Path(self.log_folder).mkdir(exist_ok=True)

        # Session state
        self.driver = None
        self.wait = None
        self.api_key = None
        self.ctk = None
        self.cookies = {}

        # Current job info
        self.current_job_id = None
        self.current_job_name = None
        self.current_job_folder = None
        self.current_job_is_existing = False  # True if job folder already existed

        # Checkpoint
        self.checkpoint_file = Path(self.log_folder) / 'checkpoint_unified.json'
        self.checkpoint_data = self._load_checkpoint()

        # Stats
        self.stats = {
            'total_processed': 0,
            'downloaded': 0,
            'skipped': 0,
            'failed': 0,
            'archived': 0,  # Jobs with no candidates (too old/archived)
            'app_data_downloaded': 0,  # Candidates with successful application-data download
        }
        self.job_stats = []  # List of {job_name, downloaded, skipped, no_cv, total}
        self.start_time = None

        # Mode settings
        self.mode = None  # 'backend' or 'frontend'
        self.job_mode = None  # 'single' or 'all'
        self.job_statuses = []  # ['ACTIVE', 'PAUSED', 'CLOSED']
        self.download_app_data = True  # Download screener-question HTML + raw JSON alongside CV
        # 'auto' = tool launches + drives Chrome via Selenium (faster for users
        # whose environment doesn't trigger Indeed's Cloudflare Turnstile block).
        # 'attach' = tool launches Chrome as a bare subprocess with a debug port,
        # user logs in manually (Turnstile sees a real human), then Selenium
        # attaches to drive downloads. Slower UX but survives bot-detection.
        self.browser_launch = 'auto'
        self._chrome_debug_port = 9222
        self._chrome_subprocess = None  # Popen handle for attach mode

    def _load_checkpoint(self) -> dict:
        """Load checkpoint data.

        Merges in any missing keys so older checkpoint files (written before
        downloaded_application_data existed) keep working without manual fixup.
        """
        default = {
            'downloaded_names': [],
            'downloaded_ids': [],
            'completed_jobs': [],
            'downloaded_application_data': [],
        }
        if self.checkpoint_file.exists():
            with open(self.checkpoint_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            # Merge defaults for any keys the old checkpoint didn't know about
            for k, v in default.items():
                if k not in loaded:
                    loaded[k] = v
            return loaded
        return default

    def _save_checkpoint(self, name: str = None, legacy_id: str = None, job_id: str = None, app_data: bool = False):
        """Save checkpoint.

        Args:
            name: candidate display name (added to CV dedup unless app_data=True)
            legacy_id: candidate legacy ID (added to CV dedup)
            job_id: job ID to mark complete
            app_data: if True, `name` is recorded against the app-data dedup list
                      instead of the CV list. Independent from CV state.
        """
        if app_data:
            if name and name not in self.checkpoint_data['downloaded_application_data']:
                self.checkpoint_data['downloaded_application_data'].append(name)
        else:
            if name and name not in self.checkpoint_data['downloaded_names']:
                self.checkpoint_data['downloaded_names'].append(name)
            if legacy_id and legacy_id not in self.checkpoint_data['downloaded_ids']:
                self.checkpoint_data['downloaded_ids'].append(legacy_id)
        if job_id and job_id not in self.checkpoint_data['completed_jobs']:
            self.checkpoint_data['completed_jobs'].append(job_id)

        with open(self.checkpoint_file, 'w', encoding='utf-8') as f:
            json.dump(self.checkpoint_data, f, ensure_ascii=False, indent=2)

    def show_menu(self):
        """Display main menu and get user choices"""
        print("""
╔════════════════════════════════════════════════════════════╗
║           Indeed CV Downloader - Unified Version           ║
╚════════════════════════════════════════════════════════════╝
""")

        # Mode selection
        print("📥 DOWNLOAD MODE:")
        print("   1. Backend (API) - Faster, parallel downloads")
        print("   2. Frontend (Selenium) - More stable, simulated clicks")
        print()

        while True:
            choice = input("Choice (1/2): ").strip()
            if choice == '1':
                self.mode = 'backend'
                break
            elif choice == '2':
                self.mode = 'frontend'
                break
            print("❌ Invalid choice")

        print()

        # Job mode selection
        print("📋 JOB SELECTION MODE:")
        print("   1. Single job - You navigate to the desired job")
        print("   2. All jobs - Automatically processes every job")
        print()

        while True:
            choice = input("Choice (1/2): ").strip()
            if choice == '1':
                self.job_mode = 'single'
                break
            elif choice == '2':
                self.job_mode = 'all'
                break
            print("❌ Invalid choice")

        # Status filter (only for 'all' mode)
        if self.job_mode == 'all':
            print()
            print("📊 JOB STATUS FILTER:")
            print("   1. Open only (ACTIVE)")
            print("   2. Paused only (PAUSED)")
            print("   3. Closed only (CLOSED)")
            print("   4. Open + Paused")
            print("   5. All (Open + Paused + Closed)")
            print()

            while True:
                choice = input("Choice (1-5): ").strip()
                if choice == '1':
                    self.job_statuses = ['ACTIVE']
                    break
                elif choice == '2':
                    self.job_statuses = ['PAUSED']
                    break
                elif choice == '3':
                    self.job_statuses = ['CLOSED']
                    break
                elif choice == '4':
                    self.job_statuses = ['ACTIVE', 'PAUSED']
                    break
                elif choice == '5':
                    self.job_statuses = ['ACTIVE', 'PAUSED', 'CLOSED']
                    break
                print("❌ Invalid choice")

        print()
        print("📎 APPLICATION DATA:")
        print("   1. Yes - Download application data (screener questions + raw JSON)")
        print("   2. No - CVs only")
        print()

        while True:
            choice = input("Choice (1/2): ").strip()
            if choice == '1':
                self.download_app_data = True
                break
            elif choice == '2':
                self.download_app_data = False
                break
            else:
                print("❌ Invalid choice")

        print()
        print("🖥  BROWSER LAUNCH:")
        print("   1. Auto — the tool opens & drives Chrome for you (default)")
        print("   2. Attach — the tool opens Chrome, YOU log in manually,")
        print("      then the tool takes over. Use this if option 1 gave you")
        print("      an \"unexpected error\" on the Indeed login screen.")
        print()

        while True:
            choice = input("Choice (1/2): ").strip()
            if choice == '1':
                self.browser_launch = 'auto'
                break
            elif choice == '2':
                self.browser_launch = 'attach'
                break
            else:
                print("❌ Invalid choice")

        print()
        print("=" * 60)
        print(f"✅ Mode: {self.mode.upper()}")
        print(f"✅ Jobs: {'Single' if self.job_mode == 'single' else 'All'}")
        if self.job_mode == 'all':
            print(f"✅ Statuses: {', '.join(self.job_statuses)}")
        print(f"✅ App data: {'Yes' if self.download_app_data else 'No'}")
        print(f"✅ Browser:  {'Auto (Selenium-launched)' if self.browser_launch == 'auto' else 'Attach (manual login, bypasses bot-detection)'}")
        print("=" * 60)
        print()

        if self.log:
            self.log.event('menu', {
                'mode': self.mode,
                'job_mode': self.job_mode,
                'job_statuses': self.job_statuses,
                'download_app_data': self.download_app_data,
                'browser_launch': self.browser_launch,
            })
            chrome_v = _probe_chrome_version()
            if chrome_v:
                self.log.info(f'Detected Chrome: {chrome_v}')
            else:
                self.log.warn('Could not detect installed Chrome version.')

    # Pretend to be a stock Chrome on Windows 10. Matches what a real user's
    # browser sends and avoids the "HeadlessChrome"/automation-specific UA
    # strings Indeed's bot-guard uses as a quick-reject signal.
    _STEALTH_USER_AGENT = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/130.0.0.0 Safari/537.36'
    )

    # This script runs on EVERY new document (before any page JS), patching
    # the handful of properties Indeed's anti-automation reads: webdriver
    # flag, empty plugins array, missing window.chrome, etc. The one-shot
    # execute_script we used to do is too late — Indeed checks these on
    # the login form's first render.
    _STEALTH_INIT_SCRIPT = r"""
        // navigator.webdriver — the #1 Selenium tell.
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        // Plugins array: real Chrome has several built-in plugins; an empty
        // list is a common automated-browser signature.
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        // Languages: Selenium sometimes ships an empty or single-entry list.
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        // window.chrome — real Chrome always has a runtime object on it.
        window.chrome = window.chrome || {};
        window.chrome.runtime = window.chrome.runtime || {};
        // navigator.permissions.query: headless Chrome returns 'denied' for
        // notifications while "real" Chrome returns 'prompt' — some bot
        // checks pick up on that.
        const origQuery = (navigator.permissions && navigator.permissions.query) || null;
        if (navigator.permissions) {
            navigator.permissions.query = (p) =>
                (p && p.name === 'notifications')
                    ? Promise.resolve({ state: Notification.permission })
                    : (origQuery ? origQuery(p) : Promise.resolve({ state: 'granted' }));
        }
    """

    def _init_chrome(self):
        """Initialize Chrome browser with anti-detection patches.

        First tries undetected-chromedriver (which patches the CDP-level
        fingerprints we can't reach from user-space Python: TLS client hello
        ordering, process-info leaks, chromedriver binary signature). Falls
        back to stock Selenium with our best-effort stealth init-script if
        UC isn't importable (e.g., a minimal build environment)."""
        print("🌐 Opening Chrome...")

        self._using_uc = False
        try:
            import undetected_chromedriver as uc
            # UC strips excludeSwitches / useAutomationExtension itself — in
            # fact setting them is a tell, because real Chrome doesn't have
            # those exclusions. So we deliberately don't pass them here.
            uc_options = uc.ChromeOptions()
            uc_options.add_argument('--log-level=3')
            uc_options.add_argument('--silent')
            uc_options.add_argument('--window-size=1920,1080')
            uc_options.add_argument(f'--user-agent={self._STEALTH_USER_AGENT}')
            prefs = {
                "download.default_directory": str(Path(self.download_folder).absolute()),
                "download.prompt_for_download": False,
                "plugins.always_open_pdf_externally": True,
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            }
            uc_options.add_experimental_option("prefs", prefs)
            # Perf log is still required by _capture_api_key.
            uc_options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})

            self.driver = uc.Chrome(options=uc_options, use_subprocess=True)
            self._using_uc = True
            print("   (stealth: undetected-chromedriver)")
        except ImportError:
            # Fallback: stock Selenium with our manual CDP stealth patches.
            chrome_options = Options()
            chrome_options.add_argument('--disable-blink-features=AutomationControlled')
            chrome_options.add_argument('--log-level=3')
            chrome_options.add_argument('--silent')
            chrome_options.add_argument('--window-size=1920,1080')
            chrome_options.add_argument(f'--user-agent={self._STEALTH_USER_AGENT}')
            chrome_options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            chrome_options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
            prefs = {
                "download.default_directory": str(Path(self.download_folder).absolute()),
                "download.prompt_for_download": False,
                "plugins.always_open_pdf_externally": True,
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            }
            chrome_options.add_experimental_option("prefs", prefs)
            self.driver = webdriver.Chrome(options=chrome_options)
            print("   (stealth: stock Selenium + CDP patches)")
        except Exception as e:
            # UC can fail to match a patched driver to the installed Chrome
            # version, or hit a permissions issue writing its driver cache.
            # Don't leave the user staring at a stacktrace — fall back to
            # stock Selenium and keep going.
            print(f"   ⚠ undetected-chromedriver failed ({e!r}); falling back to stock Selenium")
            chrome_options = Options()
            chrome_options.add_argument('--disable-blink-features=AutomationControlled')
            chrome_options.add_argument('--log-level=3')
            chrome_options.add_argument('--silent')
            chrome_options.add_argument('--window-size=1920,1080')
            chrome_options.add_argument(f'--user-agent={self._STEALTH_USER_AGENT}')
            chrome_options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            chrome_options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
            prefs = {
                "download.default_directory": str(Path(self.download_folder).absolute()),
                "download.prompt_for_download": False,
                "plugins.always_open_pdf_externally": True,
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            }
            chrome_options.add_experimental_option("prefs", prefs)
            self.driver = webdriver.Chrome(options=chrome_options)

        # Install the stealth init-script. UC applies most of this patching
        # itself, but doubling up is harmless and covers the edge where UC
        # missed a property. addScriptToEvaluateOnNewDocument runs on every
        # new document before any page JS — so it's in place for the login
        # form's first render.
        try:
            self.driver.execute_cdp_cmd(
                'Page.addScriptToEvaluateOnNewDocument',
                {'source': self._STEALTH_INIT_SCRIPT},
            )
        except Exception as e:
            print(f"   ⚠ Stealth init (new-document) failed: {e!r}")
        try:
            self.driver.execute_cdp_cmd(
                'Network.setUserAgentOverride',
                {
                    'userAgent': self._STEALTH_USER_AGENT,
                    'acceptLanguage': 'en-US,en;q=0.9',
                    'platform': 'Windows',
                },
            )
        except Exception as e:
            print(f"   ⚠ UA override failed: {e!r}")

        # Fallback patch on the current document (about:blank) in case the
        # CDP command above didn't register in time.
        try:
            self.driver.execute_script(self._STEALTH_INIT_SCRIPT)
        except Exception:
            pass

        self.wait = WebDriverWait(self.driver, 30)

    # ==================== ATTACH MODE ====================
    #
    # Attach mode bypasses Indeed's Cloudflare Turnstile challenge. Turnstile
    # runs invisibly on the login form and, if it decides the client is a bot,
    # silently refuses to populate the `cf-turnstile-response` field — Indeed
    # then returns 403 on /account/emailvalidation. Even undetected-chromedriver
    # can't always pass Turnstile because Turnstile fingerprints at the TLS
    # layer (JA3/JA4) and looks for warm profile history.
    #
    # Attach mode sidesteps all of that:
    #   1. Launch Chrome as a plain subprocess — NO Selenium/CDP at launch
    #      time, so Chrome is indistinguishable from a user-started browser
    #      from Turnstile's point of view.
    #   2. User logs in manually. Turnstile sees real human interaction and
    #      issues its token. Indeed accepts the login, sets session cookies.
    #   3. THEN we attach Selenium via the --remote-debugging-port the
    #      subprocess is listening on. Indeed's API doesn't re-challenge on
    #      subsequent calls — it just checks the session cookies.
    #
    # The user-data-dir is persistent under logs/chrome_profile/ so repeat
    # runs don't require re-login until Indeed's own session expiry.

    # Chrome binary lookup order. First hit wins. The "shutil.which" fallbacks
    # at the call site cover cases where Chrome is on PATH but not at a
    # standard install location (e.g., portable installs, work-provisioned
    # laptops that move it).
    _CHROME_CANDIDATE_PATHS = (
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        # Linux
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    )

    def _find_chrome_binary(self) -> Optional[str]:
        """Locate a real Chrome binary for attach-mode subprocess launch.
        Probes Windows/mac/Linux standard install paths plus %LOCALAPPDATA%
        and PATH, returning the first one that exists."""
        # %LOCALAPPDATA% on Windows — per-user installs land here.
        localappdata = os.environ.get('LOCALAPPDATA')
        if localappdata:
            user_chrome = Path(localappdata) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'
            if user_chrome.exists():
                return str(user_chrome)

        for p in self._CHROME_CANDIDATE_PATHS:
            if Path(p).exists():
                return p

        for name in ('chrome', 'google-chrome', 'chromium', 'chrome.exe'):
            found = shutil.which(name)
            if found:
                return found

        return None

    def _init_chrome_attached(self) -> bool:
        """Launch Chrome as a detached subprocess with a debug port, wait
        for the user to log in, then attach Selenium. Returns True on
        success, False on unrecoverable error (user aborts, Chrome not
        found, etc.)."""
        print("🌐 Opening Chrome in attach mode...")

        chrome_bin = self._find_chrome_binary()
        if not chrome_bin:
            print("❌ Could not find Chrome on this machine automatically.")
            print("   Common paths checked: Program Files, Program Files (x86),")
            print("   %LOCALAPPDATA%\\Google\\Chrome, and PATH.")
            entered = input("   Paste the full path to chrome.exe (or blank to abort): ").strip().strip('"')
            if not entered or not Path(entered).exists():
                print("❌ No Chrome binary — aborting.")
                return False
            chrome_bin = entered

        profile_dir = Path(self.log_folder).absolute() / "chrome_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Redirect Chrome's own stderr to a file so it doesn't pollute our
        # console. If launch fails, the log lives next to the profile for
        # postmortem — otherwise it's benign telemetry noise.
        chrome_log_path = profile_dir / "chrome_stderr.log"

        cmd = [
            str(chrome_bin),
            f"--remote-debugging-port={self._chrome_debug_port}",
            f"--user-data-dir={profile_dir}",
            # These make the first-run experience cleaner for HR:
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            "https://employers.indeed.com",
        ]

        try:
            # close_fds on POSIX, no-op on Windows; keep the subprocess
            # detached from our console so closing it doesn't kill the .exe.
            chrome_log = open(chrome_log_path, 'ab')
            kwargs = {
                'stdout': subprocess.DEVNULL,
                'stderr': chrome_log,
                'close_fds': True,
            }
            if os.name == 'nt':
                # Windows: detach the Chrome process so it survives our exit.
                kwargs['creationflags'] = (
                    getattr(subprocess, 'DETACHED_PROCESS', 0x00000008)
                    | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
                )
            self._chrome_subprocess = subprocess.Popen(cmd, **kwargs)
        except FileNotFoundError:
            print(f"❌ Could not execute {chrome_bin} — file not found or not executable.")
            if self.log:
                self.log.event('attach_launch', {'ok': False, 'reason': 'FileNotFoundError', 'chrome_bin': chrome_bin})
            return False
        except Exception as e:
            print(f"❌ Failed to launch Chrome subprocess: {e!r}")
            if self.log:
                self.log.error('attach_launch_failed', e)
            return False

        print(f"   Chrome launched (PID {self._chrome_subprocess.pid}, profile at {profile_dir}).")
        if self.log:
            self.log.event('attach_launch', {
                'ok': True,
                'chrome_bin': chrome_bin,
                'pid': self._chrome_subprocess.pid,
                'debug_port': self._chrome_debug_port,
                'profile_dir': str(profile_dir),
            })
        print()
        print("=" * 60)
        print("🔐 LOG IN MANUALLY IN THE CHROME WINDOW")
        print("=" * 60)
        print("   1. In the Chrome window that just opened, sign in to")
        print("      https://employers.indeed.com as you normally would.")
        print("   2. When you see your employer dashboard with your jobs")
        print("      listed, come back to this console.")
        print("   3. Press Enter here to continue.")
        print()
        print("   (If it's your first time using attach mode you'll log in.")
        print("    On future runs the profile persists — you'll already be")
        print("    logged in and can press Enter right away.)")
        print()
        input("Press Enter when you're on the employer dashboard...")

        # Give Chrome a moment to flush the session cookies to disk before
        # we attach. Without a short wait, get_cookies() sometimes races.
        time.sleep(2)

        # Attach Selenium to the running Chrome. The debuggerAddress tells
        # chromedriver to connect to the existing DevTools rather than
        # spawn its own Chrome.
        attach_opts = Options()
        attach_opts.add_experimental_option(
            "debuggerAddress", f"127.0.0.1:{self._chrome_debug_port}"
        )
        attach_opts.set_capability('goog:loggingPrefs', {'performance': 'ALL'})

        try:
            self.driver = webdriver.Chrome(options=attach_opts)
        except Exception as e:
            print(f"❌ Couldn't attach Selenium to Chrome on port {self._chrome_debug_port}: {e!r}")
            print(f"   Check {chrome_log_path} for Chrome-side errors.")
            return False

        self.wait = WebDriverWait(self.driver, 30)
        print(f"✅ Attached to Chrome at 127.0.0.1:{self._chrome_debug_port}")
        return True

    # A real authenticated Indeed Employer session includes these cookies.
    # If they're missing after login, Indeed didn't actually issue a session
    # (usually because a VPN IP triggered Cloudflare, the login form errored
    # silently, or the user's tracking-protection stripped them). The shell
    # of the page may still render enough DOM for _is_logged_in() to pass,
    # but every subsequent API call will fail.
    _ESSENTIAL_SESSION_COOKIES = ('CTK', '__Secure-PassportAuthProxy-BearerToken')

    def _session_is_healthy(self, cookies: list) -> tuple:
        """Return (ok, reason). `ok=False` means the cookie set can't sustain
        an authenticated Indeed session — caller should not save it and
        should not proceed into API calls."""
        if not cookies:
            if self.log:
                self.log.event('session_check', {'ok': False, 'reason': 'no cookies captured'})
            return False, "no cookies captured"
        names = {c.get('name') for c in cookies if isinstance(c, dict)}
        missing = [n for n in self._ESSENTIAL_SESSION_COOKIES if n not in names]
        # Names-only logging — never log cookie values (auth tokens, PII).
        if self.log:
            self.log.event('session_check', {
                'cookie_count': len(cookies),
                'cookie_names_sample': sorted(names)[:30],
                'missing_essentials': missing,
                'ok': not missing and len(cookies) >= 5,
            })
        if missing:
            return False, f"missing essential session cookies: {', '.join(missing)}"
        if len(cookies) < 5:
            return False, f"only {len(cookies)} cookies captured (expected 15+)"
        return True, ""

    def _print_vpn_remediation(self, reason: str) -> None:
        """Shared error message for the VPN/Cloudflare-broken-session case."""
        print(f"   ⚠ Session looks incomplete: {reason}")
        print("   Indeed did not issue the auth cookies this login needs.")
        print("   Most common cause is a VPN IP (NordVPN, etc.) hitting a")
        print("   Cloudflare challenge that silently strips session cookies.")
        print("   Try one of:")
        print("     • Disable NordVPN (or any VPN) on this machine, then re-run.")
        print("     • Clear Chrome's cookies for indeed.com and log in again.")
        print("     • Switch to Frontend (Selenium) mode — option 2 at the menu.")

    def _load_saved_cookies(self) -> list:
        """Load cookies from saved JSON file if it exists. Refuses files
        that couldn't possibly represent a real session (too few cookies or
        missing essentials) — those are stale artifacts of prior failed
        runs and would confuse _is_logged_in() into a false positive."""
        cookies_file = Path(self.log_folder) / 'indeed_cookies.json'
        if not cookies_file.exists():
            return []
        try:
            with open(cookies_file, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
        if not cookies:
            return []
        ok, reason = self._session_is_healthy(cookies)
        if not ok:
            print(f"⚠️  Saved cookies rejected ({reason}) — treating as fresh login.")
            return []
        return cookies

    def _inject_cookies(self, cookies_list: list):
        """Inject cookies into the browser session"""
        self.driver.get("https://employers.indeed.com")
        time.sleep(2)

        injected = 0
        for cookie in cookies_list:
            try:
                cookie_dict = {
                    'name': cookie['name'],
                    'value': cookie['value'],
                    'domain': cookie.get('domain', '.indeed.com'),
                    'path': cookie.get('path', '/')
                }
                self.driver.add_cookie(cookie_dict)
                self.cookies[cookie['name']] = cookie['value']

                if cookie['name'] == 'CTK':
                    self.ctk = cookie['value']
                injected += 1
            except Exception:
                continue

        self.driver.refresh()
        time.sleep(3)
        return injected

    def _is_logged_in(self) -> bool:
        """Check if we are logged in to Indeed Employer dashboard"""
        try:
            current_url = self.driver.current_url
            # If redirected to login/auth page, not logged in
            if any(x in current_url for x in ['/auth', '/login', 'secure.indeed.com', 'accounts.indeed.com']):
                return False
            # Check for employer dashboard elements
            is_employer = self.driver.execute_script("""
                return !!(
                    document.querySelector('[data-testid="job-row"]') ||
                    document.querySelector('[data-testid="nav-employer"]') ||
                    document.querySelector('.gnav-header-UserMenu') ||
                    document.querySelector('[data-testid="header-user-menu"]') ||
                    document.querySelector('[data-testid="candidates-pipeline"]') ||
                    document.querySelector('.css-1f9ew9y') ||
                    (window.location.hostname === 'employers.indeed.com' &&
                     !window.location.pathname.includes('/auth'))
                );
            """)
            return is_employer
        except Exception:
            return False

    def _capture_browser_cookies(self) -> list:
        """Capture all Indeed cookies from the current browser session"""
        cookies = self.driver.get_cookies()
        indeed_cookies = []
        for cookie in cookies:
            if 'indeed' in cookie.get('domain', ''):
                indeed_cookies.append({
                    'name': cookie['name'],
                    'value': cookie['value'],
                    'domain': cookie.get('domain', '.indeed.com'),
                    'path': cookie.get('path', '/'),
                    'secure': cookie.get('secure', False),
                    'httpOnly': cookie.get('httpOnly', False),
                    'expiry': cookie.get('expiry', 0)
                })
                self.cookies[cookie['name']] = cookie['value']
                if cookie['name'] == 'CTK':
                    self.ctk = cookie['value']
        return indeed_cookies

    def _save_cookies(self, cookies: list):
        """Save cookies to JSON file for future sessions"""
        cookies_file = Path(self.log_folder) / 'indeed_cookies.json'
        with open(cookies_file, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, indent=2, ensure_ascii=False)
        print(f"   ✅ {len(cookies)} cookies saved for future sessions")

    def _wait_for_login(self):
        """Wait for user to manually log in to Indeed Employer"""
        print()
        print("=" * 60)
        print("🔐 LOGIN REQUIRED")
        print("=" * 60)
        print()
        print("   Please log in to your Indeed Employer account")
        print("   in the Chrome window that just opened.")
        print()
        print("   Waiting for login...")
        print()

        # Navigate to the login page
        self.driver.get("https://employers.indeed.com")
        time.sleep(2)

        # Wait for login (check every 3 seconds, max 5 minutes)
        max_wait = 300  # 5 minutes
        elapsed = 0
        while elapsed < max_wait:
            time.sleep(3)
            elapsed += 3

            try:
                current_url = self.driver.current_url
                # Check if we've been redirected to the employer dashboard
                if 'employers.indeed.com' in current_url and '/auth' not in current_url:
                    if self._is_logged_in():
                        print("   ✅ Login detected!")
                        return True
            except Exception:
                continue

            # Show progress every 30 seconds
            if elapsed % 30 == 0:
                print(f"   ⏳ Waiting... ({elapsed}s)")

        print("   ❌ Login timeout exceeded (5 minutes)")
        return False

    def setup_chrome(self) -> bool:
        """Setup Chrome and authenticate - uses saved cookies or interactive login"""
        # Attach mode is a completely different bootstrap: Chrome is launched
        # as a bare subprocess, the user logs in by hand, then we attach. The
        # subprocess Chrome carries its own persistent profile under
        # logs/chrome_profile/, so we skip cookie load/save entirely.
        if self.browser_launch == 'attach':
            if not self._init_chrome_attached():
                return False

            # Ensure we're on /candidates so _capture_api_key has a real
            # GraphQL request in the network log.
            try:
                self.driver.get("https://employers.indeed.com/candidates")
                time.sleep(4)
            except Exception:
                pass

            if not self._is_logged_in():
                print("❌ Attached Chrome isn't on the employer dashboard.")
                print("   Make sure you logged in in the Chrome window BEFORE pressing Enter,")
                print("   then re-run the tool.")
                return False

            # Pull CTK from the attached browser's cookie jar. (We intentionally
            # do NOT write indeed_cookies.json in attach mode — the subprocess
            # Chrome's persistent profile is the source of truth for that
            # session, and duplicating it on disk would just invite drift.)
            try:
                for cookie in (self.driver.get_cookies() or []):
                    if cookie.get('name') == 'CTK':
                        self.ctk = cookie.get('value')
                    self.cookies[cookie.get('name')] = cookie.get('value')
            except Exception:
                pass

            self._capture_api_key()

            if not self.api_key:
                print("❌ Attached OK but couldn't capture the GraphQL API key.")
                print("   In the Chrome window, refresh the candidates page and try again.")
                return False
            if not self.ctk:
                print("⚠  CTK cookie missing from attached session — partial login?")
                return False

            print("✅ Attached session authenticated.")
            return True

        self._init_chrome()

        # Try to load saved cookies first
        saved_cookies = self._load_saved_cookies()

        if saved_cookies:
            print("🔑 Saved cookies found, attempting to log in...")
            self._inject_cookies(saved_cookies)

            # Navigate to employer dashboard to check if session is valid
            self.driver.get("https://employers.indeed.com/candidates")
            time.sleep(4)

            if self._is_logged_in():
                print("✅ Logged in with saved cookies")
                self._capture_api_key()
                if self.api_key:
                    # Re-capture + re-save so short-lived tokens (bearer JWT,
                    # __cf_bm) the browser just refreshed roll forward to the
                    # next run. Without this the file on disk freezes at day-1
                    # values and auth silently dies after the JWT's 1-hour TTL.
                    fresh_cookies = self._capture_browser_cookies()
                    if fresh_cookies:
                        self._save_cookies(fresh_cookies)
                    return True
                # Hostname check passed but dashboard never issued a real
                # GraphQL request — session is effectively dead. Fall through
                # to manual re-login instead of proceeding with api_key=None.
                print("⚠️  Logged-in shell detected but no GraphQL API key captured — session stale, asking for manual login")
            else:
                print("⚠️  Cookies expired or invalid")

        # No valid cookies - ask user to log in manually
        if not self._wait_for_login():
            return False

        # Give the page time to fully load after login
        time.sleep(3)

        # Capture cookies from the authenticated session
        cookies = self._capture_browser_cookies()

        # Validate the session actually received the auth cookies that make
        # Indeed's GraphQL requests authenticated. NordVPN + Cloudflare can
        # produce a visibly logged-in page with no session cookies set,
        # which would pass _is_logged_in() but fail every API call.
        ok, reason = self._session_is_healthy(cookies)
        if not ok:
            self._print_vpn_remediation(reason)
            # Deliberately do NOT save the partial cookie file — it would
            # be loaded on the next run and short-circuit back to the same
            # false-positive logged-in state.
            return False

        self._save_cookies(cookies)

        # Navigate to candidates page and capture API key
        self._capture_api_key()

        if not self.api_key:
            print("❌ Authentication looked OK (cookies present) but no GraphQL API key")
            print("   appeared in the Chrome performance log.")
            print("   Indeed may be throttling this session or their frontend changed.")
            print("   Try one of:")
            print("     • Quit Chrome fully (all windows) and re-run.")
            print("     • Disable any VPN/proxy and re-run.")
            print("     • Switch to Frontend (Selenium) mode — option 2 at the menu.")
            return False

        print("✅ Authentication successful!")
        return True

    def _capture_api_key(self):
        """Capture API key from network logs"""
        try:
            current_url = self.driver.current_url
            if 'candidates' not in current_url:
                self.driver.get("https://employers.indeed.com/candidates")
                time.sleep(5)

            logs = self.driver.get_log('performance')
            for log in logs:
                try:
                    message = json.loads(log['message'])['message']
                    if message['method'] == 'Network.requestWillBeSent':
                        url = message['params']['request']['url']
                        if 'graphql' in url and 'apis.indeed.com' in url:
                            headers = message['params']['request']['headers']
                            if 'indeed-api-key' in headers:
                                self.api_key = headers['indeed-api-key']
                                break
                except (KeyError, json.JSONDecodeError):
                    continue

            if self.api_key:
                print(f"   ✅ API Key captured")
                if self.log:
                    self.log.event('api_key_capture', {'ok': True})
            else:
                print(f"   ⚠ API key NOT captured — performance log had no graphql request to apis.indeed.com (session likely unauthenticated)")
                if self.log:
                    # Also log a sample of URLs the performance log DID contain,
                    # so we can tell "page never loaded" from "page loaded but
                    # didn't hit apis.indeed.com" in post-mortem.
                    try:
                        sample_urls = []
                        for log in (self.driver.get_log('performance') or [])[:200]:
                            try:
                                m = json.loads(log['message'])['message']
                                if m.get('method') == 'Network.requestWillBeSent':
                                    u = m['params']['request']['url']
                                    sample_urls.append(u[:140])
                            except (KeyError, json.JSONDecodeError):
                                continue
                        self.log.event('api_key_capture', {
                            'ok': False,
                            'url_sample': sample_urls[:20],
                            'total_perf_entries': len(sample_urls),
                        })
                    except Exception:
                        self.log.event('api_key_capture', {'ok': False})
        except Exception as e:
            print(f"   ⚠ API-key capture threw: {e!r}")
            if self.log:
                self.log.error('api_key_capture_exception', e)

    def _clean_job_title(self, title: str) -> str:
        """Clean the job title to produce a valid folder name"""
        # Remove (H/F), H/F, (F/H), F/H and variants (French gender markers)
        title = re.sub(r'\s*\(?\s*[HF]\s*/\s*[HF]\s*\)?\s*', '', title, flags=re.IGNORECASE)
        # Replace / with -
        title = title.replace('/', '-')
        # Remove invalid characters for a Windows folder name
        title = re.sub(r'[<>:"|?*]', '', title)
        # Collapse multiple spaces
        title = re.sub(r'\s+', ' ', title)
        # Trim
        title = title.strip()
        return title

    def _save_job_stats(self, total_announced: int, total_recovered: int, processed: int):
        """Save job statistics to stats.json in job folder

        Args:
            total_announced: Number of candidates shown in job listing
            total_recovered: Number of candidates returned by API
            processed: Number of candidates actually processed (CVs + no_cv)
        """
        if not self.current_job_folder:
            return
        stats_file = self.current_job_folder / 'stats.json'
        stats = {
            'total_announced': total_announced,
            'total_recovered': total_recovered,
            'processed': processed
        }
        with open(stats_file, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2)

    def _load_job_stats(self, folder: Path) -> dict:
        """Load job statistics from stats.json"""
        stats_file = folder / 'stats.json'
        if stats_file.exists():
            try:
                with open(stats_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return None

    def _create_job_folder(self, job_name: str, job_date: str = None) -> Path:
        """Create folder for job with name and date"""
        # Clean job name for folder
        safe_name = self._clean_job_title(job_name)
        safe_name = safe_name[:80]  # Limit length

        if job_date:
            folder_name = f"{safe_name} ({job_date})"
        else:
            folder_name = safe_name

        job_folder = Path(self.download_folder) / folder_name

        # Check if folder already exists (has PDFs)
        self.current_job_is_existing = job_folder.exists() and any(job_folder.rglob('*.pdf'))

        job_folder.mkdir(exist_ok=True)

        self.current_job_folder = job_folder
        return job_folder

    def _close_modals(self):
        """Close any modal/popup that might be open"""
        try:
            # Common modal close selectors
            close_selectors = [
                "button[aria-label='Close']",
                "button[aria-label='Fermer']",
                "button[data-testid='modal-close']",
                "button[data-testid='CloseButton']",
                "[data-testid='modal-close-button']",
                ".modal-close",
                ".close-modal",
                "button.css-1k9jcwk",  # Indeed's close button class
                "[aria-label='close']",
                "[aria-label='dismiss']",
                "button[class*='close']",
                "div[role='dialog'] button[type='button']",
            ]

            for selector in close_selectors:
                try:
                    buttons = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for btn in buttons:
                        if btn.is_displayed():
                            btn.click()
                            time.sleep(0.3)
                except (NoSuchElementException, StaleElementReferenceException):
                    continue

            # Also try pressing Escape key
            try:
                from selenium.webdriver.common.keys import Keys
                body = self.driver.find_element(By.TAG_NAME, "body")
                body.send_keys(Keys.ESCAPE)
                time.sleep(0.3)
            except (NoSuchElementException, Exception):
                pass

        except Exception:
            pass

    def _extract_job_id_from_url(self, url: str) -> Optional[str]:
        """Extract employerJobId from URL"""
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if 'selectedJobs' in params:
                return unquote(params['selectedJobs'][0])
        except (ValueError, KeyError, IndexError):
            pass
        return None

    # ==================== BACKEND MODE (API) ====================

    def fetch_candidates_api(self, offset: int = 0, limit: int = 100, dispositions: list = None, sort_by: str = "APPLY_DATE", sort_order: str = "DESCENDING"):
        """Fetch candidates using GraphQL API via browser.

        Returns (matches, total, has_next_page).
        """
        # legacyID lives on 5 distinct CandidateSubmission union types; missing
        # any of them drops that candidate silently (legacyID becomes None and
        # the downstream filter skips it). Keep all 5 in sync with the live
        # dashboard request — see indeed_graphql_1.txt / indeed_graphql_2.txt.
        query = """query FindRCPMatches($input: OrchestrationMatchesInput!) {
  findRCPMatches(input: $input) {
    overallMatchCount
    matchConnection {
      pageInfo { hasNextPage hasPreviousPage }
      matches {
        candidateSubmission {
          id
          data {
            __typename
            profile { name { displayName } }
            resume {
              ... on CandidatePdfResume { id downloadUrl txtDownloadUrl }
              ... on CandidateHtmlFile { id downloadUrl }
              ... on CandidateTxtFile { id downloadUrl }
              ... on CandidateUnrenderableFile { id downloadUrl }
            }
            ... on LegacyCandidateSubmission { legacyID }
            ... on IndeedApplyCandidateSubmission { legacyID }
            ... on EmployerGeneratedCandidateSubmission { legacyID }
            ... on HiddenIndeedApplyCandidateSubmission { legacyID }
            ... on HiddenEmployerGeneratedCandidateSubmission { legacyID }
          }
        }
      }
    }
  }
}"""

        # The 6 active-pipeline dispositions — proven to work by live
        # capture. Post-active states (REJECTED / WITHDRAWN / HIRED /
        # AUTO_REJECTED) are fetched SEPARATELY in isolated passes by
        # _download_all_candidates_api so a single rejected enum value can't
        # blow up the entire run with AllMatchProvidersFailedException.
        if dispositions is None:
            dispositions = ["NEW", "PENDING", "PHONE_SCREENED", "INTERVIEWED", "OFFER_MADE", "REVIEWED"]

        surface_context = [{"contextKey": "DISPOSITION", "contextPayload": d} for d in dispositions]
        surface_context.append({"contextKey": "SORT_BY", "contextPayload": sort_by})
        surface_context.append({"contextKey": "SORT_ORDER", "contextPayload": sort_order})

        # Indeed's schema declares `identifiers: OrchestrationIdentifiersInput!`
        # (non-null) — omitting the field yields
        # "missing input value at `$input.identifiers`" and 0 results, as
        # seen in logs/run_20260421_204255.log. The live dashboard ALWAYS
        # sends this key, using an empty `jobIdentifiers` dict for unscoped
        # queries (see indeed_graphql_1.txt). Match that shape here.
        job_identifiers = {}
        if self.current_job_id:
            job_identifiers["employerJobId"] = self.current_job_id

        variables = {
            "input": {
                "clientSurfaceName": "candidate-list-page",
                "defaultStrategyId": "U20GF",
                "limit": limit,
                "offset": offset,
                "context": {
                    "surfaceContext": surface_context
                },
                "identifiers": {"jobIdentifiers": job_identifiers},
            }
        }

        payload = {"operationName": "FindRCPMatches", "variables": variables, "query": query}

        # One-time query-shape log per pagination run, to aid diagnosis without
        # being noisy. Printed only on offset=0.
        if offset == 0:
            scope = f"employerJobId={self.current_job_id[:24]}..." if self.current_job_id else "unscoped (all jobs)"
            print(f"   🔎 Query: {scope}, dispositions={len(dispositions)}, limit={limit}")

        # Abort early on missing auth instead of sending the literal string
        # "None" as the indeed-api-key header (Python f-string would stringify
        # None). That produced Indeed's "An API Key is required" error which
        # HR was hitting after saved cookies went stale.
        if not self.api_key or not self.ctk:
            print("❌ Aborting: missing indeed-api-key or CTK (auth did not complete).")
            print("   Delete logs/indeed_cookies.json and re-run to log in fresh.")
            return [], 0, False

        js_code = f"""
        return await fetch("https://apis.indeed.com/graphql?co=US&locale=en-US", {{
            method: "POST",
            headers: {{
                "accept": "*/*",
                "content-type": "application/json",
                "indeed-api-key": "{self.api_key}",
                "indeed-ctk": "{self.ctk}",
                "indeed-client-sub-app": "talent-organization-modules",
                "indeed-client-sub-app-component": "./CandidateListPage"
            }},
            body: JSON.stringify({json.dumps(payload)}),
            credentials: "include"
        }}).then(r => r.json());
        """

        try:
            result = self.driver.execute_script(js_code)
            if not result:
                print(f"   ⚠ GraphQL returned no response (auth may have expired)")
                return [], 0, False
            if 'errors' in result:
                print(f"   ⚠ GraphQL errors from Indeed:")
                msgs = []
                for err in (result.get('errors') or [])[:3]:
                    msg = err.get('message', str(err)) if isinstance(err, dict) else str(err)
                    print(f"      • {msg}")
                    msgs.append(msg)
                if self.log:
                    self.log.event('graphql_error', {
                        'messages': msgs,
                        'has_api_key': bool(self.api_key),
                        'has_ctk': bool(self.ctk),
                        'dispositions': dispositions,
                        'offset': offset,
                    })
                return [], 0, False

            rcp = result.get('data', {}).get('findRCPMatches', {}) or {}
            conn = rcp.get('matchConnection', {}) or {}
            matches = conn.get('matches', []) or []
            total = rcp.get('overallMatchCount', 0) or 0
            has_next_page = bool((conn.get('pageInfo') or {}).get('hasNextPage'))
            if offset == 0:
                print(f"   🔎 Server returned: overallMatchCount={total}, matches_on_page={len(matches)}, hasNextPage={has_next_page}")
            return matches, total, has_next_page
        except Exception as e:
            print(f"❌ API error: {e}")
            return [], 0, False

    def download_cv_api(self, candidate: dict) -> bool:
        """Download CV via API"""
        name = candidate['name']
        legacy_id = candidate['legacy_id']
        download_url = candidate['download_url']

        if legacy_id in self.checkpoint_data['downloaded_ids']:
            self.stats['skipped'] += 1
            return True

        try:
            js_code = f"""
            const response = await fetch("{download_url}", {{ credentials: "include" }});
            if (!response.ok) {{
                const altResponse = await fetch("https://employers.indeed.com/api/catws/resume/v2/download?id={legacy_id}", {{ credentials: "include" }});
                if (!altResponse.ok) return null;
                const blob = await altResponse.blob();
                return await new Promise((resolve) => {{
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result.split(',')[1]);
                    reader.readAsDataURL(blob);
                }});
            }}
            const blob = await response.blob();
            return await new Promise((resolve) => {{
                const reader = new FileReader();
                reader.onloadend = () => resolve(reader.result.split(',')[1]);
                reader.readAsDataURL(blob);
            }});
            """

            base64_data = self.driver.execute_script(js_code)
            if not base64_data:
                self.stats['failed'] += 1
                return False

            pdf_data = base64.b64decode(base64_data)

            candidate_folder = self._create_candidate_folder(name)
            filepath = candidate_folder / "resume.pdf"

            with open(filepath, 'wb') as f:
                f.write(pdf_data)

            if filepath.stat().st_size > 1000:
                self._save_checkpoint(name=name, legacy_id=legacy_id)
                self.stats['downloaded'] += 1
                return True
            else:
                filepath.unlink()
                self.stats['failed'] += 1
                return False

        except Exception as e:
            self.stats['failed'] += 1
            return False

    def run_backend_single_job(self):
        """Run backend mode for single job"""
        print("\n" + "=" * 60)
        print("👆 Navigate to the desired job in Chrome, then press Enter.")
        print("   Tip: click the job title in your Jobs list — you should land")
        print("   on THAT JOB's candidates page. The global 'All Candidates'")
        print("   view (statusName=All) won't work; pick one specific job.")
        print("=" * 60)
        input()

        job_url = self.driver.current_url
        self.current_job_id = self._extract_job_id_from_url(job_url)

        # Diagnostic snapshot — prints once per run, helps trace any
        # "0 candidates" failure to its actual cause (bad URL extraction,
        # missing auth token, wrong scope, etc).
        print(f"\n🔎 Diagnostics:")
        print(f"   URL: {job_url}")
        print(f"   Extracted job IRI: {self.current_job_id or '(none)'}")
        print(f"   API key: {self.api_key[:12] + '...' if self.api_key else '(MISSING — auth may have failed)'}")
        print(f"   CTK:     {self.ctk or '(MISSING — auth may have failed)'}")

        # Single-mode guard: if we couldn't pull a job IRI from the URL, bail
        # out instead of silently falling through to an unscoped GraphQL
        # query — that path (a) isn't what the user asked for in Single mode,
        # (b) dumps everything into `Job_unknown/`, and (c) used to crash on
        # Indeed's schema because `identifiers` is required. First seen in
        # logs/run_20260421_204255.log where HR was on `?statusName=All&id=0`.
        if not self.current_job_id:
            print("\n❌ Single-job mode needs a specific job.")
            print("   Your URL doesn't include `selectedJobs=...`, which means")
            print("   you're probably on the global 'All Candidates' pipeline")
            print("   instead of one job's candidate list.")
            print("\n   What to do:")
            print("   1. In Chrome, go back to your Jobs list.")
            print("   2. Click the job title you want to download.")
            print("   3. Confirm the URL looks like:")
            print("         employers.indeed.com/candidates?selectedJobs=aXJp...")
            print("   4. Re-run the tool.")
            print("\n   (If your URL uses `employerJobId=` or some other parameter")
            print("    instead of `selectedJobs=`, send the full URL to Pawel —")
            print("    the parser may need an update for your account shape.)")
            if self.log:
                self.log.event('single_job_abort', {
                    'reason': 'no_job_iri_in_url',
                    'url': job_url,
                    'has_api_key': bool(self.api_key),
                    'has_ctk': bool(self.ctk),
                })
            return

        # Get job name from page. The first selector the page exposes may be
        # the generic "Candidates" h1 (not the job title) depending on which
        # subpage HR navigated to; in that case we fall back to a stable
        # label derived from the job's UUID so different jobs don't collide
        # into one folder.
        try:
            job_name = self.driver.execute_script("""
                const el = document.querySelector('[data-testid="job-title"]') ||
                           document.querySelector('h1') ||
                           document.querySelector('.job-title');
                return el ? el.textContent.trim() : '';
            """)
        except Exception:
            job_name = ''

        generic_names = {'', 'candidates', 'applicants', 'all candidates', 'job'}
        if (job_name or '').strip().lower() in generic_names:
            fallback = 'Job_unknown'
            if self.current_job_id:
                try:
                    # selectedJobs is base64-encoded `iri://apis.indeed.com/EmployerJob/<uuid>`
                    decoded = base64.b64decode(self.current_job_id + '===').decode('utf-8', errors='ignore')
                    uuid_tail = decoded.rsplit('/', 1)[-1][:8]
                    if uuid_tail:
                        fallback = f"Job_{uuid_tail}"
                except Exception:
                    pass
            print(f"   (page title was generic — using fallback folder name '{fallback}')")
            job_name = fallback

        try:
            self._create_job_folder(job_name)
            print(f"📁 Folder: {self.current_job_folder}")
        except Exception:
            pass

        self._download_all_candidates_api()

    def _load_job_checkpoint(self, scan_pdfs: bool = False) -> tuple:
        """Load checkpoint for current job folder - returns (downloaded_ids, downloaded_names)

        Args:
            scan_pdfs: If True, scan existing PDF files for names (for existing jobs with new candidates)
        """
        downloaded_ids = set(self.checkpoint_data.get('downloaded_ids', []))
        downloaded_names = set(self.checkpoint_data.get('downloaded_names', []))

        if not self.current_job_folder:
            return downloaded_ids, downloaded_names

        # Load from job-specific checkpoint if exists
        job_checkpoint_file = self.current_job_folder / 'checkpoint.json'
        if job_checkpoint_file.exists():
            try:
                with open(job_checkpoint_file, 'r', encoding='utf-8') as f:
                    job_data = json.load(f)
                    downloaded_ids.update(job_data.get('downloaded_ids', []))
                    downloaded_names.update(job_data.get('downloaded_names', []))
            except (json.JSONDecodeError, IOError):
                pass

        # Scan existing PDF files to get names (only for existing jobs with new candidates)
        if scan_pdfs:
            print("   Scanning existing CVs...")
            for pdf_file in self.current_job_folder.rglob('*.pdf'):
                # Format: "Jean Dupont_20251126_154317.pdf"
                name_part = pdf_file.stem.rsplit('_', 2)[0]  # Get "Jean Dupont"
                if name_part:
                    downloaded_names.add(name_part.lower())
            print(f"   {len(downloaded_names)} names found in existing files")

        return downloaded_ids, downloaded_names

    def _save_job_checkpoint(self, legacy_id: str, name: str = None):
        """Save checkpoint for current job folder"""
        if not self.current_job_folder:
            return

        job_checkpoint_file = self.current_job_folder / 'checkpoint.json'

        # Load existing
        job_data = {'downloaded_ids': [], 'downloaded_names': []}
        if job_checkpoint_file.exists():
            try:
                with open(job_checkpoint_file, 'r', encoding='utf-8') as f:
                    job_data = json.load(f)
                    if 'downloaded_names' not in job_data:
                        job_data['downloaded_names'] = []
            except (json.JSONDecodeError, IOError):
                pass

        # Add new id
        if legacy_id and legacy_id not in job_data['downloaded_ids']:
            job_data['downloaded_ids'].append(legacy_id)

        # Add new name
        if name:
            clean_name = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip().lower()
            if clean_name and clean_name not in job_data['downloaded_names']:
                job_data['downloaded_names'].append(clean_name)

        # Save
        with open(job_checkpoint_file, 'w', encoding='utf-8') as f:
            json.dump(job_data, f, ensure_ascii=False, indent=2)

    def _fetch_candidates_batch(self, dispositions: list, sort_by: str = "APPLY_DATE", sort_order: str = "DESCENDING") -> tuple:
        """Fetch candidates with specific filters, returns (candidates_list, total_count)"""
        all_candidates = {}  # Use dict to dedupe by legacy_id
        offset = 0
        total_announced = 0

        while True:
            matches, total, has_next_page = self.fetch_candidates_api(
                offset=offset,
                limit=100,
                dispositions=dispositions,
                sort_by=sort_by,
                sort_order=sort_order
            )

            if offset == 0:
                total_announced = total

            if not matches:
                break

            for match in matches:
                try:
                    sub = match.get('candidateSubmission', {})
                    data = sub.get('data', {})
                    name = data.get('profile', {}).get('name', {}).get('displayName', 'Unknown')
                    legacy_id = data.get('legacyID')
                    resume = data.get('resume', {})
                    download_url = resume.get('downloadUrl') if resume else None

                    if legacy_id and legacy_id not in all_candidates:
                        all_candidates[legacy_id] = {
                            'name': name,
                            'legacy_id': legacy_id,
                            'download_url': download_url  # Can be None if no CV
                        }
                except (KeyError, TypeError):
                    continue

            if not has_next_page:
                break
            offset += 100
            time.sleep(0.3)

        return list(all_candidates.values()), total_announced

    def _download_all_candidates_api(self, job_total_candidates: int = 0):
        """Download all candidates via API with multiple passes to bypass 3000 limit

        Args:
            job_total_candidates: Total candidates from job listing (used to decide if we need multi-pass)
        """
        print("\nFetching candidates via API...")

        # Active-pipeline dispositions — the combination proven safe by live
        # capture. Sent together in a single query.
        active_dispositions = ["NEW", "PENDING", "PHONE_SCREENED", "INTERVIEWED", "OFFER_MADE", "REVIEWED"]

        # Post-active dispositions — each sent in its OWN query so a single
        # unsupported enum value produces an AllMatchProvidersFailedException
        # on that one pass only, rather than killing the whole run.
        extra_dispositions = ["REJECTED", "AUTO_REJECTED", "WITHDRAWN", "HIRED"]

        # Full list used by the large-volume fallback (Pass 5 below).
        all_dispositions = active_dispositions + extra_dispositions
        all_candidates = {}  # key: legacy_id, value: candidate dict

        # Pass 1: active pipeline, sort by date DESC (default view).
        print("   Fetching candidates (active pipeline)...")
        candidates, api_total = self._fetch_candidates_batch(active_dispositions, "APPLY_DATE", "DESCENDING")
        for c in candidates:
            if c['legacy_id'] not in all_candidates:
                all_candidates[c['legacy_id']] = c
        print(f"      {len(all_candidates)} fetched")

        # Pass 1.5: each post-active disposition in isolation. If one of
        # these enum values is unsupported by Indeed's providers, its pass
        # returns [] (with a logged error) and we continue with the next.
        for d in extra_dispositions:
            extra_candidates, extra_total = self._fetch_candidates_batch([d], "APPLY_DATE", "DESCENDING")
            new_count = 0
            for c in extra_candidates:
                if c['legacy_id'] not in all_candidates:
                    all_candidates[c['legacy_id']] = c
                    new_count += 1
            if new_count > 0 or extra_total > 0:
                print(f"      +{new_count} {d} (server reported {extra_total})")

        # Use job_total_candidates if available (more accurate), otherwise use API total
        total_expected = job_total_candidates if job_total_candidates > 0 else api_total

        # If we got everything or expected <= 3000, no extra passes needed
        if len(all_candidates) >= total_expected or total_expected <= 3000:
            pass  # Got everything, no additional passes needed
        else:
            # Additional passes to get past the 3000 limit
            print(f"   API limit reached ({len(all_candidates)}/{total_expected}), running additional passes...")

            # Pass 2: Sort by date ASC
            print("   Pass 2: By date (oldest -> newest)...")
            candidates, _ = self._fetch_candidates_batch(all_dispositions, "APPLY_DATE", "ASCENDING")
            new_count = 0
            for c in candidates:
                if c['legacy_id'] not in all_candidates:
                    all_candidates[c['legacy_id']] = c
                    new_count += 1
            print(f"      +{new_count} new, total: {len(all_candidates)}")

            # Pass 3: Sort by name ASC (if still missing)
            if len(all_candidates) < total_expected:
                print("   Pass 3: By name (A -> Z)...")
                candidates, _ = self._fetch_candidates_batch(all_dispositions, "NAME", "ASCENDING")
                new_count = 0
                for c in candidates:
                    if c['legacy_id'] not in all_candidates:
                        all_candidates[c['legacy_id']] = c
                        new_count += 1
                print(f"      +{new_count} new, total: {len(all_candidates)}")

            # Pass 4: Sort by name DESC (if still missing)
            if len(all_candidates) < total_expected:
                print("   Pass 4: By name (Z -> A)...")
                candidates, _ = self._fetch_candidates_batch(all_dispositions, "NAME", "DESCENDING")
                new_count = 0
                for c in candidates:
                    if c['legacy_id'] not in all_candidates:
                        all_candidates[c['legacy_id']] = c
                        new_count += 1
                print(f"      +{new_count} new, total: {len(all_candidates)}")

            # Pass 5: By individual status (if >1000 missing)
            if len(all_candidates) < total_expected and (total_expected - len(all_candidates)) > 1000:
                print("   Pass 5: By individual status...")
                for disp in all_dispositions:
                    for sort_by in ["APPLY_DATE", "NAME"]:
                        for sort_order in ["ASCENDING", "DESCENDING"]:
                            candidates, _ = self._fetch_candidates_batch([disp], sort_by, sort_order)
                            new_count = 0
                            for c in candidates:
                                if c['legacy_id'] not in all_candidates:
                                    all_candidates[c['legacy_id']] = c
                                    new_count += 1
                            if new_count > 0:
                                print(f"      {disp} ({sort_by} {sort_order}): +{new_count}")
                print(f"      Total: {len(all_candidates)}")

        all_candidates_list = list(all_candidates.values())

        print(f"\n   Total expected: {total_expected} | Fetched: {len(all_candidates_list)}")

        if len(all_candidates_list) == 0 and total_expected > 0:
            print(f"   No candidates fetched - job too old or data archived")
            self.stats['archived'] += 1
            return

        if len(all_candidates_list) < total_expected:
            missing = total_expected - len(all_candidates_list)
            pct = (len(all_candidates_list) / total_expected) * 100
            print(f"   Note: {missing} candidates not fetched ({pct:.1f}% fetched)")

        # Load already processed names (PDFs + no_cv.txt)
        processed_names = set()
        if self.current_job_folder and self.current_job_folder.exists():
            # Scan PDF files
            for pdf_file in self.current_job_folder.rglob('*.pdf'):
                # Format: "Jean Dupont_20251126_154317.pdf"
                name_part = pdf_file.stem.rsplit('_', 2)[0]  # Get "Jean Dupont"
                if name_part:
                    clean_name = "".join(ch for ch in name_part if ch.isalnum() or ch in (' ', '-', '_')).strip().lower()
                    processed_names.add(clean_name)

            # Load no_cv.txt (candidates without CV)
            no_cv_file = self.current_job_folder / 'no_cv.txt'
            if no_cv_file.exists():
                with open(no_cv_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        name = line.strip()
                        if name:
                            clean_name = "".join(ch for ch in name if ch.isalnum() or ch in (' ', '-', '_')).strip().lower()
                            processed_names.add(clean_name)

        # Separate candidates with CV and without CV
        candidates_with_cv = []
        candidates_no_cv = []
        already_processed = 0
        for c in all_candidates_list:
            clean_name = "".join(ch for ch in c['name'] if ch.isalnum() or ch in (' ', '-', '_')).strip().lower()
            if clean_name in processed_names:
                already_processed += 1
                continue  # Already processed
            if c['download_url']:
                candidates_with_cv.append(c)
            else:
                candidates_no_cv.append(c)

        # Save candidates without CV to no_cv.txt
        if candidates_no_cv and self.current_job_folder:
            no_cv_file = self.current_job_folder / 'no_cv.txt'
            with open(no_cv_file, 'a', encoding='utf-8') as f:
                for c in candidates_no_cv:
                    f.write(c['name'] + '\n')
            print(f"   {len(candidates_no_cv)} candidates without CV (saved to no_cv.txt)")

        print(f"\n   To download: {len(candidates_with_cv)} | Already done: {already_processed} | Without CV: {len(candidates_no_cv)}")

        # Use recovered count (not announced) - some candidates may be archived by Indeed
        total_recovered = len(all_candidates_list)

        if not candidates_with_cv:
            print("   All CVs are already downloaded!")
            # Save stats: announced, recovered, processed
            self._save_job_stats(total_expected, total_recovered, already_processed + len(candidates_no_cv))
            # Global counter parity with the Frontend path so the end-of-run
            # summary reflects work actually seen by this job.
            self.stats['total_processed'] += already_processed + len(candidates_no_cv)
            # Track job stats for report
            self.job_stats.append({
                'job_name': self.current_job_name,
                'downloaded': 0,
                'skipped': already_processed,
                'no_cv': len(candidates_no_cv),
                'total_announced': total_expected,
                'total_recovered': total_recovered
            })
            # Still run the app-data pass — `downloaded_application_data`
            # is dedup'd independently of the CV list, so HR can backfill
            # screener Q&A for candidates whose resumes were already on disk.
            self._run_app_data_pass_backend(all_candidates_list)
            return

        print(f"\n   Downloading...\n")

        # Candidates we're skipping (CV already on disk + people who applied
        # without a CV) count as "processed" in the global summary too.
        self.stats['total_processed'] += already_processed + len(candidates_no_cv)

        downloaded_count = 0
        with tqdm(total=len(candidates_with_cv), desc="   CVs") as pbar:
            for candidate in candidates_with_cv:
                if self.download_cv_api(candidate):
                    downloaded_count += 1
                # Global counter parity with the Frontend path; without this
                # the end-of-run STATISTICS block printed "Total processed: 0"
                # even when Downloaded was e.g. 329.
                self.stats['total_processed'] += 1
                pbar.update(1)

        # Save stats: announced, recovered, processed
        total_processed = already_processed + len(candidates_no_cv) + downloaded_count
        self._save_job_stats(total_expected, total_recovered, total_processed)

        # Track job stats for report
        self.job_stats.append({
            'job_name': self.current_job_name,
            'downloaded': downloaded_count,
            'skipped': already_processed,
            'no_cv': len(candidates_no_cv),
            'total_announced': total_expected,
            'total_recovered': total_recovered
        })

        # Second pass: app-data per candidate, driving the UI (see
        # _run_app_data_pass_backend). No-op when download_app_data is False.
        # Pass the full candidate list so the pass can navigate directly to
        # each profile by legacy_id — independent of the candidate-list
        # sidebar DOM, which Indeed periodically restructures.
        self._run_app_data_pass_backend(all_candidates_list)

    # ==================== APP DATA (BACKEND HYBRID) ====================
    #
    # Pure-API app-data download would require knowing the endpoints Indeed
    # hits when the user clicks "Download files" in the application-data
    # modal. FindRCPMatches doesn't return them, and we don't yet have a
    # captured example (HR_DEBUG_TOMORROW.md Snippet 3 is the planned path
    # to capture one). Until that capture lands, we get Backend-mode parity
    # by reusing the Frontend UI-click flow per candidate:
    #
    #   1. Click into the first candidate in the list sidebar.
    #   2. Call _download_application_data_frontend() — same helper Frontend
    #      mode already uses — to pop the kebab menu, open the modal, tick
    #      HTML + JSON, click Download files, move files into per-candidate
    #      folder.
    #   3. Advance via _go_to_next_candidate() and repeat.
    #
    # Every candidate gets app-data; dedup via the existing
    # checkpoint_data['downloaded_application_data'] list so reruns don't
    # re-click. _maybe_capture_app_data_urls() records whatever the browser
    # actually hit during the first candidate so a future build can upgrade
    # this to pure-API.

    # Fallback chain for locating the candidate-list sidebar. Tried in order
    # until one reports ≥1 item. The previous single-selector impl failed on
    # HR's machine (see logs/run_20260420_143601.log: abort "Could not click
    # the first candidate") because the list DOM wasn't hydrated when a
    # fixed 3-second sleep elapsed. A polling wait across multiple selectors
    # absorbs both timing and DOM-variation differences.
    _CANDIDATE_LIST_SELECTORS = (
        '#hanselCandidateListContainer li[data-testid="CandidateListItem"]',
        '[data-testid="candidate-list"] li[data-testid="CandidateListItem"]',
        '[data-testid="candidates-pipeline"] li[data-testid="CandidateListItem"]',
        'ul[role="list"] li[data-testid="CandidateListItem"]',
        'li[data-testid="CandidateListItem"]',
    )

    def _click_first_candidate_in_list(self) -> bool:
        """Click the first row in the candidate-list sidebar so subsequent
        _go_to_next_candidate() calls have a current row to advance from.

        Polls multiple candidate-list selectors for up to 8s — enough to
        survive the list-hydration race on slower machines / first-render
        after navigation."""
        deadline = time.time() + 8.0
        while time.time() < deadline:
            for sel in self._CANDIDATE_LIST_SELECTORS:
                try:
                    count = self.driver.execute_script(
                        "return document.querySelectorAll(arguments[0]).length;",
                        sel,
                    )
                except Exception:
                    continue
                if not count:
                    continue
                try:
                    clicked = self.driver.execute_script(
                        """
                        const items = document.querySelectorAll(arguments[0]);
                        if (!items.length) return false;
                        const btn = items[0].querySelector('button[data-testid="CandidateListItem-button"]')
                                    || items[0].querySelector('button')
                                    || items[0].querySelector('a');
                        if (!btn) return false;
                        items[0].scrollIntoView({block: 'center'});
                        btn.click();
                        return true;
                        """,
                        sel,
                    )
                except Exception:
                    continue
                if clicked:
                    return True
            time.sleep(0.5)
        return False

    def _log_app_data_pass_abort(self, reason: str) -> None:
        """Best-effort diagnostic dump when the app-data pass can't start.
        Writes a structured event to the run log with URL, page title, and
        DOM counts so the next regression triages from latest.log alone."""
        if not self.log:
            return
        try:
            title = self.driver.title
        except Exception:
            title = '<title unavailable>'
        try:
            url = self.driver.current_url
        except Exception:
            url = '<url unavailable>'
        try:
            counts = self.driver.execute_script(
                """
                return {
                    hansel: document.querySelectorAll('#hanselCandidateListContainer li').length,
                    testid: document.querySelectorAll('li[data-testid="CandidateListItem"]').length,
                    any_li: document.querySelectorAll('ul li').length,
                    aria_current: document.querySelectorAll('[aria-current="true"]').length,
                };
                """
            )
        except Exception:
            counts = None
        self.log.event('app_data_pass_abort', {
            'reason': reason,
            'url': url,
            'page_title': title,
            'list_counts': counts,
            'download_app_data': self.download_app_data,
        })

    def _maybe_capture_app_data_urls(self) -> None:
        """Best-effort: scrape Chrome's performance log for XHR URLs that
        match known app-data patterns and append them to
        logs/app_data_urls.json. Used to bootstrap a Stage-2 pure-API
        upgrade without requiring HR to paste DevTools output."""
        try:
            logs = self.driver.get_log('performance')
        except Exception:
            return

        found = []
        for log in logs:
            try:
                msg = json.loads(log['message'])['message']
                method = msg.get('method', '')
                if method not in ('Network.requestWillBeSent', 'Network.responseReceived'):
                    continue
                params = msg.get('params', {}) or {}
                url = (params.get('request') or params.get('response') or {}).get('url', '')
                if not url:
                    continue
                if not re.search(r'application|cao_post_body|original-application', url, re.I):
                    continue
                if 'graphql' in url:
                    continue
                entry = {
                    'direction': 'request' if method == 'Network.requestWillBeSent' else 'response',
                    'url': url,
                }
                if method == 'Network.requestWillBeSent':
                    req = params.get('request') or {}
                    entry['http_method'] = req.get('method')
                    entry['has_body'] = bool(req.get('postData'))
                found.append(entry)
            except (KeyError, json.JSONDecodeError):
                continue

        if not found:
            return

        try:
            out_file = Path(self.log_folder) / 'app_data_urls.json'
            existing = []
            if out_file.exists():
                try:
                    with open(out_file, 'r', encoding='utf-8') as f:
                        existing = json.load(f) or []
                except (json.JSONDecodeError, IOError):
                    existing = []
            seen = {(e.get('direction'), e.get('url')) for e in existing if isinstance(e, dict)}
            for entry in found:
                key = (entry.get('direction'), entry.get('url'))
                if key not in seen:
                    existing.append(entry)
                    seen.add(key)
            with open(out_file, 'w', encoding='utf-8') as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _run_app_data_pass_backend(self, candidates_list: Optional[list] = None) -> None:
        """Run the UI-driven app-data download flow over every candidate
        for the currently active job, driven by direct URL navigation
        rather than candidate-list sidebar pagination.

        Why not use the list sidebar? Indeed quietly shipped a new candidate
        list DOM (see logs/run_20260420_155009.log: "any_li":225 on page but
        "testid":0 and "hansel":0). Every list selector we had assumed the
        container was `#hanselCandidateListContainer` with
        `data-testid="CandidateListItem"` rows — those attributes are gone
        on the new UI variant, so every list-sidebar-based approach fails
        regardless of how many fallback selectors we stack.

        Direct URL navigation bypasses the list entirely: we already have
        every candidate's `legacy_id` from the GraphQL response, and
        Indeed's profile URL takes that id as a query param
        (`?id=<legacy_id>&selectedJobs=<iri>`). On each iteration we
        navigate there, wait for the "..." kebab to appear, and then run
        the existing `_download_application_data_frontend` helper which
        uses text- and aria-label-based selectors that survive DOM renames.
        """
        if not self.download_app_data:
            return
        if not self.current_job_folder:
            print("   ⚠ Skipping app-data pass: no job folder set.")
            return
        if not candidates_list:
            print("   ⚠ Skipping app-data pass: no candidate list provided.")
            if self.log:
                self.log.event('app_data_pass_abort', {'reason': 'no_candidates_list'})
            return

        job_iri_encoded = quote(self.current_job_id, safe='') if self.current_job_id else ''

        print(f"\n📎 App-data pass — visiting each of {len(candidates_list)} candidate profiles directly...")

        discovered = False
        processed = 0
        succeeded = 0
        failed = 0
        skipped_already_done = 0
        first_failure_logged = False

        pbar = tqdm(total=len(candidates_list), desc="   App data")
        try:
            for candidate in candidates_list:
                name = candidate.get('name') or 'Unknown'
                legacy_id = candidate.get('legacy_id')

                if not legacy_id:
                    # Should be rare — the GraphQL extractor normally drops
                    # candidates without a legacyID. If one slips through,
                    # we can't navigate to their profile by URL.
                    failed += 1
                    if self.log and not first_failure_logged:
                        self.log.event('app_data_pass_abort',
                                       {'reason': 'no_legacy_id', 'name_hash': hash(name)})
                        first_failure_logged = True
                    pbar.update(1)
                    continue

                if name in self.checkpoint_data.get('downloaded_application_data', []):
                    skipped_already_done += 1
                    pbar.update(1)
                    continue

                candidate_folder = self._create_candidate_folder(name)

                # Navigate to this candidate's profile. Passing selectedJobs
                # alongside id keeps the job context so the kebab menu shows
                # the right set of actions.
                if job_iri_encoded:
                    profile_url = (
                        f"https://employers.indeed.com/candidates"
                        f"?id={legacy_id}&selectedJobs={job_iri_encoded}"
                    )
                else:
                    profile_url = f"https://employers.indeed.com/candidates?id={legacy_id}"

                try:
                    self.driver.get(profile_url)
                except Exception:
                    failed += 1
                    pbar.update(1)
                    continue

                # Wait for the profile to render — the "..." kebab being
                # present is our proof of life. _find_element_by_selectors
                # uses WebDriverWait so this returns as soon as the kebab
                # exists (or after the timeout).
                kebab = self._find_element_by_selectors(self._KEBAB_MENU_SELECTORS, timeout_per=2.0)
                if not kebab:
                    failed += 1
                    if self.log and not first_failure_logged:
                        self._log_app_data_pass_abort('kebab_not_found_after_nav')
                        first_failure_logged = True
                    pbar.update(1)
                    continue

                if self._download_application_data_frontend(name, candidate_folder):
                    self._save_checkpoint(name=name, app_data=True)
                    self.stats['app_data_downloaded'] += 1
                    succeeded += 1
                    if not discovered:
                        self._maybe_capture_app_data_urls()
                        discovered = True
                else:
                    failed += 1
                    if self.log and not first_failure_logged:
                        self._log_app_data_pass_abort('helper_returned_false_on_profile')
                        first_failure_logged = True

                processed += 1
                pbar.update(1)
                time.sleep(self.next_candidate_delay)
        finally:
            pbar.close()
            if self.log:
                self.log.event('app_data_pass_summary', {
                    'processed': processed,
                    'succeeded': succeeded,
                    'failed': failed,
                    'skipped_already_done': skipped_already_done,
                })
            # Console-visible outcome so HR knows if something went wrong
            # without having to read the log file.
            if failed > 0:
                print(f"   ⚠ App-data pass: {succeeded} saved, {failed} failed, {skipped_already_done} already on disk.")
                print("     (Details written to logs/latest.log — first failure's DOM state was captured.)")
            else:
                print(f"   ✅ App-data pass: {succeeded} saved ({skipped_already_done} already on disk).")

    # ==================== FRONTEND MODE (Selenium) ====================

    def run_frontend_single_job(self):
        """Run frontend mode for single job"""
        print("\n" + "=" * 60)
        print("👆 Navigate to the job and click on the first candidate")
        print("   then press Enter")
        print("=" * 60)
        input()

        # Get job name and create folder
        try:
            job_name = self.driver.execute_script("""
                const el = document.querySelector('[data-testid="job-title"]') ||
                           document.querySelector('h1');
                return el ? el.textContent.trim() : 'Job';
            """)
            self._create_job_folder(job_name)
            print(f"📁 Folder: {self.current_job_folder}")
        except Exception:
            pass

        self._download_all_candidates_frontend()

    def _download_all_candidates_frontend(self):
        """Download candidates using Selenium clicks.

        For each candidate: compute the candidate folder up front, run the
        CV download (dedup'd on name), then — if the user opted in to it —
        run the application-data flow (independently dedup'd).
        """
        print("\n🚀 Downloading via Selenium...\n")

        pbar = tqdm(desc="CVs")
        count = 0

        while count < self.max_cvs:
            # Get candidate name
            name = self._get_current_candidate_name()
            if not name:
                break

            # Always compute the candidate folder — CV flow and app-data flow
            # both need it, and mkdir is idempotent.
            candidate_folder = self._create_candidate_folder(name)

            # Check if already downloaded (CV dedup)
            if name in self.checkpoint_data['downloaded_names']:
                self.stats['skipped'] += 1
            else:
                # Download CV
                if self._download_cv_frontend(name, candidate_folder):
                    self.stats['downloaded'] += 1
                else:
                    self.stats['failed'] += 1

            # Application-data flow is independent of the CV dedup: it may
            # run even for a candidate whose CV was skipped (already done
            # in a previous run), so HR can backfill app data later.
            if self.download_app_data and name not in self.checkpoint_data.get('downloaded_application_data', []):
                if self._download_application_data_frontend(name, candidate_folder):
                    self._save_checkpoint(name=name, app_data=True)
                    self.stats['app_data_downloaded'] += 1
                    print(f"✅ App data saved for {name}")
                else:
                    print(f"⚠️ App data click failed for {name}")

            self.stats['total_processed'] += 1
            count += 1
            pbar.update(1)

            # Go to next candidate
            if not self._go_to_next_candidate():
                break

            time.sleep(self.next_candidate_delay)

        pbar.close()

    def _get_current_candidate_name(self) -> Optional[str]:
        """Get name from page"""
        try:
            name = self.driver.execute_script("""
                const el = document.querySelector('[data-testid="name-plate-name-item"] span');
                return el ? el.textContent.trim() : null;
            """)
            return name
        except Exception:
            return None

    # Indeed has shipped several variants of this control across redesigns
    # and locales (anchor vs button, exact text vs nested span, icon-only).
    # Try selectors in order; first hit wins.
    _DOWNLOAD_BUTTON_SELECTORS = [
        # Stable attributes Indeed uses for testability
        "//*[@data-testid='download-resume' or @data-testid='download-cv']",
        "//*[contains(@data-testid, 'download') and (self::a or self::button)]",
        "//a[contains(@aria-label, 'Download') or contains(@aria-label, 'Télécharger')]",
        "//button[contains(@aria-label, 'Download') or contains(@aria-label, 'Télécharger')]",
        # Text-based, case-insensitive, ignoring nested spans/icons
        "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download resume')]",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download resume')]",
        "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download cv')]",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download cv')]",
        "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'télécharger')]",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'télécharger')]",
        # Direct download links to Indeed's resume endpoint
        "//a[contains(@href, '/api/catws/resume') or contains(@href, '/resume/v2/download')]",
    ]

    def _find_element_by_selectors(self, selectors, timeout_per: float = 1.0):
        """Try each XPath selector in order; return the first match or None.

        Single wait-per-selector helper used everywhere Selenium needs to
        locate an element from a fallback chain (Indeed ships multiple
        variants across redesigns). Keeps the call sites uniform.
        """
        for selector in selectors:
            try:
                el = WebDriverWait(self.driver, timeout_per).until(
                    EC.presence_of_element_located((By.XPATH, selector))
                )
                if el:
                    return el
            except TimeoutException:
                continue
        return None

    def _find_download_button(self):
        """Try each selector until one matches; return the element or None."""
        return self._find_element_by_selectors(self._DOWNLOAD_BUTTON_SELECTORS)

    # Selector chains for the "..." (kebab) more-options menu on a candidate
    # profile, and the subsequent "Download application data" modal flow.
    # Plan-provided best-effort guesses; HR may refine these after tomorrow's
    # DevTools capture.
    _KEBAB_MENU_SELECTORS = [
        "//button[@data-testid='candidate-actions-menu' or @data-testid='more-options' or @data-testid='kebab-menu' or @data-testid='candidate-kebab']",
        "//button[contains(@aria-label, 'More options') or contains(@aria-label, 'More actions') or contains(@aria-label, 'Actions')]",
        "//button[@aria-haspopup='menu' or @aria-haspopup='true']",
        # Kebab/three-dot heuristic: button with a visible '…' or stacked dots icon
        "//button[.//*[name()='svg' and (contains(@aria-label, 'more') or contains(@aria-label, 'More'))]]",
    ]

    _APP_DATA_MENU_ITEM_SELECTORS = [
        "//*[@role='menuitem' and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download application data')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download application data')]",
        "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download application data')]",
        "//li[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download application data')]",
    ]

    _APP_DATA_MODAL_SELECTORS = [
        "//div[@role='dialog' and .//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download application data')]]",
        "//*[contains(@class, 'modal')][.//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download application data')]]",
    ]

    _APP_DATA_CONFIRM_SELECTORS = [
        "//button[@data-testid='download-files-button' or @data-testid='confirm-download' or @data-testid='download-files']",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download files')]",
        "//div[@role='dialog']//button[@type='submit']",
    ]

    def _create_candidate_folder(self, name: str) -> Path:
        """Return (and create) downloads/<job>/<safe candidate name>/.

        Sanitization matches the other places that clean a candidate name:
        keep alphanumerics, spaces, dashes, underscores; strip everything else.
        Falls back to 'unknown' if the cleaned name ends up empty.
        """
        safe_name = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip()
        if not safe_name:
            safe_name = "unknown"
        base = self.current_job_folder or Path(self.download_folder)
        folder = base / safe_name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _download_cv_frontend(self, name: str, candidate_folder: Path) -> bool:
        """Download CV using click, then move the PDF into candidate_folder."""
        try:
            for attempt in range(3):
                try:
                    download_link = self._find_download_button()
                    if not download_link:
                        return False
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", download_link)
                    time.sleep(0.2)
                    self.driver.execute_script("arguments[0].click();", download_link)
                    break
                except StaleElementReferenceException:
                    if attempt == 2:
                        return False
                    time.sleep(0.5)

            time.sleep(self.download_delay)

            if self._verify_and_rename_download(name, candidate_folder):
                self._save_checkpoint(name=name)
                return True
            return False

        except Exception:
            return False

    def _verify_and_rename_download(self, name: str, candidate_folder: Path) -> bool:
        """Verify the CV download and move it into the candidate's folder.

        Chrome is configured once at init to drop files into the job folder,
        so we glob the *job* folder (not the candidate folder) and move the
        fresh PDF to `<candidate_folder>/resume.pdf`.
        """
        # The job folder is where Chrome actually writes the download.
        job_folder = self.current_job_folder or Path(self.download_folder)

        for _ in range(10):
            files = list(job_folder.glob("*.pdf"))
            for f in files:
                # Ignore any PDF already renamed into a candidate subfolder
                # (glob on the job folder only returns top-level pdfs anyway).
                if f.stat().st_size > 1000 and name.split()[0].lower() not in f.name.lower():
                    target = candidate_folder / "resume.pdf"
                    # Overwrite any existing resume.pdf (idempotent rerun)
                    if target.exists():
                        try:
                            target.unlink()
                        except OSError:
                            pass
                    f.rename(target)
                    return True
            time.sleep(0.5)

        return False

    def _check_app_data_box(self, pattern_regex: str, modal) -> bool:
        """Find a row in `modal` whose text matches `pattern_regex` (case-insensitive)
        and tick the checkbox inside it. Supports both native <input type=checkbox>
        and role=checkbox styled buttons."""
        js = """
        const modal = arguments[0];
        const re = new RegExp(arguments[1], 'i');
        const rows = modal.querySelectorAll('li, div, label, tr');
        for (const row of rows) {
            if (re.test(row.textContent || '')) {
                const cb = row.querySelector('input[type="checkbox"], [role="checkbox"]');
                if (cb) {
                    if (cb.tagName === 'INPUT') {
                        if (!cb.checked) cb.click();
                        return !!cb.checked;
                    } else {
                        if (cb.getAttribute('aria-checked') !== 'true') cb.click();
                        return cb.getAttribute('aria-checked') === 'true';
                    }
                }
            }
        }
        return false;
        """
        try:
            return bool(self.driver.execute_script(js, modal, pattern_regex))
        except Exception:
            return False

    def _download_application_data_frontend(self, name: str, candidate_folder: Path) -> bool:
        """Automate the "..." -> Download application data -> check boxes -> Download files flow."""
        try:
            kebab = self._find_element_by_selectors(self._KEBAB_MENU_SELECTORS, timeout_per=1.5)
            if not kebab:
                return False
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();", kebab
            )
            time.sleep(0.4)

            item = self._find_element_by_selectors(self._APP_DATA_MENU_ITEM_SELECTORS, timeout_per=1.5)
            if not item:
                try:
                    self.driver.execute_script("document.body.click();")
                except Exception:
                    pass
                return False
            self.driver.execute_script("arguments[0].click();", item)
            time.sleep(0.5)

            modal = self._find_element_by_selectors(self._APP_DATA_MODAL_SELECTORS, timeout_per=3)
            if not modal:
                try:
                    self.driver.execute_script("document.body.click();")
                except Exception:
                    pass
                return False

            # Tick HTML + JSON; skip PDF (already downloaded as the resume).
            html_ok = self._check_app_data_box(r'-original-application\.html|original-application', modal)
            json_ok = self._check_app_data_box(r'cao_post_body', modal)
            if not (html_ok and json_ok):
                try:
                    self.driver.execute_script("document.body.click();")
                except Exception:
                    pass
                return False

            confirm = self._find_element_by_selectors(self._APP_DATA_CONFIRM_SELECTORS, timeout_per=1.5)
            if not confirm:
                try:
                    self.driver.execute_script("document.body.click();")
                except Exception:
                    pass
                return False
            self.driver.execute_script("arguments[0].click();", confirm)

            return self._move_application_files(name, candidate_folder)

        except Exception:
            try:
                self.driver.execute_script("document.body.click();")
            except Exception:
                pass
            return False

    def _move_application_files(self, name: str, candidate_folder: Path) -> bool:
        """Wait for Chrome to drop the two app-data files in the job folder,
        then move + rename them into the candidate folder as
        application.html and application.json. Returns True iff BOTH arrived.

        Snapshots the set of matching files already present in the job folder
        at call time (e.g., stragglers from a previous candidate whose download
        was still streaming) so we don't misattribute them to this candidate.
        """
        job_folder = self.current_job_folder or Path(self.download_folder)
        html_target = candidate_folder / "application.html"
        json_target = candidate_folder / "application.json"

        def _find_html_matches():
            return (
                list(job_folder.glob("*-original-application.HTML"))
                + list(job_folder.glob("*-original-application.html"))
                + list(job_folder.glob("*.HTML"))
            )

        def _find_json_matches():
            return (
                list(job_folder.glob("cao_post_body_*.json"))
                + list(job_folder.glob("cao_post_body*.json"))
            )

        # Snapshot pre-existing matching files to exclude them from "just arrived".
        pre_existing_html = set(_find_html_matches())
        pre_existing_json = set(_find_json_matches())

        html_found = html_target.exists()
        json_found = json_target.exists()

        for _ in range(30):  # up to ~15s
            if not html_found:
                for f in _find_html_matches():
                    if f in pre_existing_html:
                        continue
                    if f.is_file() and f.stat().st_size > 0:
                        try:
                            if html_target.exists():
                                html_target.unlink()
                            f.rename(html_target)
                            html_found = True
                            break
                        except OSError:
                            pass

            if not json_found:
                for f in _find_json_matches():
                    if f in pre_existing_json:
                        continue
                    if f.is_file() and f.stat().st_size > 0:
                        try:
                            if json_target.exists():
                                json_target.unlink()
                            f.rename(json_target)
                            json_found = True
                            break
                        except OSError:
                            pass

            if html_found and json_found:
                return True
            time.sleep(0.5)

        return html_found and json_found

    def _go_to_next_candidate(self) -> bool:
        """Navigate to next candidate"""
        try:
            current_index = self.driver.execute_script("""
                const items = document.querySelectorAll('#hanselCandidateListContainer > div > ul > li[data-testid="CandidateListItem"]');
                for (let i = 0; i < items.length; i++) {
                    if (items[i].getAttribute('aria-current') === 'true' || items[i].getAttribute('data-selected') === 'true') {
                        return i;
                    }
                }
                return -1;
            """)

            if current_index == -1:
                return False

            # Click next candidate
            clicked = self.driver.execute_script(f"""
                const items = document.querySelectorAll('#hanselCandidateListContainer > div > ul > li[data-testid="CandidateListItem"]');
                const nextItem = items[{current_index + 1}];
                if (!nextItem) {{
                    // Try to load more
                    const btn = document.getElementById('fetchNextCandidates') ||
                               document.querySelector('[data-testid="fetchNextCandidates"]');
                    if (btn) {{ btn.click(); return 'loading'; }}
                    return null;
                }}
                const btn = nextItem.querySelector('button[data-testid="CandidateListItem-button"]');
                if (btn) {{
                    nextItem.scrollIntoView({{block: 'center'}});
                    btn.click();
                    return true;
                }}
                return null;
            """)

            if clicked == 'loading':
                time.sleep(2)
                return self._go_to_next_candidate()

            return clicked == True

        except Exception as e:
            return False

    # ==================== ALL JOBS MODE ====================

    def _format_date(self, date_str: str) -> str:
        """Convert 'septembre 22, 2025' (or 'september 22, 2025') into '22-09-2025'

        Parses month names from Indeed's dashboard output, which may be in French
        or English depending on the account locale.
        """
        # French month names - required because Indeed's French dashboard emits these.
        months_fr = {
            'janvier': '01', 'février': '02', 'mars': '03', 'avril': '04',
            'mai': '05', 'juin': '06', 'juillet': '07', 'août': '08',
            'septembre': '09', 'octobre': '10', 'novembre': '11', 'décembre': '12'
        }
        # English month names - used when Indeed's dashboard is in English.
        months_en = {
            'january': '01', 'february': '02', 'march': '03', 'april': '04',
            'may': '05', 'june': '06', 'july': '07', 'august': '08',
            'september': '09', 'october': '10', 'november': '11', 'december': '12'
        }
        try:
            parts = date_str.lower().split()
            if len(parts) >= 3:
                month = months_fr.get(parts[0]) or months_en.get(parts[0], '00')
                day = parts[1].replace(',', '').zfill(2)
                year = parts[2]
                return f"{day}-{month}-{year}"
        except (IndexError, ValueError):
            pass
        return date_str

    def _extract_jobs_from_page(self) -> list:
        """Extract jobs from the current HTML table page"""
        jobs = []
        try:
            rows = self.driver.find_elements(By.CSS_SELECTOR, "tr[data-testid='job-row']")

            for row in rows:
                try:
                    # Job title - try multiple selectors
                    title_elem = None
                    job_link = None

                    try:
                        title_elem = row.find_element(By.CSS_SELECTOR, "span[data-testid='UnifiedJobTldTitle'] a")
                    except NoSuchElementException:
                        pass

                    if not title_elem:
                        try:
                            title_elem = row.find_element(By.CSS_SELECTOR, "a[data-testid='UnifiedJobTldLink']")
                        except NoSuchElementException:
                            pass

                    if not title_elem:
                        continue

                    title = title_elem.text.strip()
                    job_link = title_elem.get_attribute('href')

                    if not title:
                        continue

                    # Clean the title
                    clean_title = self._clean_job_title(title)

                    # Posting date
                    date_str = ""
                    date_formatted = ""
                    try:
                        date_elem = row.find_element(By.CSS_SELECTOR, "div[data-testid='job-created-date'] span[title]")
                        date_title = date_elem.get_attribute('title')
                        date_match = re.search(r'(\w+ \d+, \d+)', date_title)
                        date_str = date_match.group(1) if date_match else ""
                        date_formatted = self._format_date(date_str)
                    except (NoSuchElementException, AttributeError):
                        pass

                    # Candidate count
                    total_candidates = 0
                    try:
                        candidates_elem = row.find_element(By.CSS_SELECTOR, "span[data-testid='candidates-pipeline-hosted-all-count']")
                        total_candidates = int(candidates_elem.text)
                    except (NoSuchElementException, ValueError):
                        pass

                    # Status - try multiple selectors. The status text matchers include
                    # French substrings ('ouvert', 'suspendu', 'fermé', 'clos') because
                    # Indeed's French dashboard returns those — keep them alongside the
                    # English alternates so the tool works for both locales.
                    status = "ACTIVE"  # Default if nothing matches
                    try:
                        # Try the primary selector
                        status_elem = row.find_element(By.CSS_SELECTOR, "div[data-testid='top-level-job-status']")
                        status_text = status_elem.text.strip().lower()

                        if 'ouvert' in status_text or 'open' in status_text:
                            status = 'ACTIVE'
                        elif 'suspendu' in status_text or 'pause' in status_text or 'paused' in status_text:
                            status = 'PAUSED'
                        elif 'fermé' in status_text or 'clos' in status_text or 'closed' in status_text:
                            status = 'CLOSED'
                    except NoSuchElementException:
                        pass

                    # Extract the employerJobId from the link
                    employer_job_id = None
                    if job_link:
                        # Try employerJobId first
                        if 'employerJobId=' in job_link:
                            match = re.search(r'employerJobId=([^&]+)', job_link)
                            if match:
                                employer_job_id = unquote(match.group(1))
                        # Try id parameter
                        elif 'id=' in job_link:
                            match = re.search(r'[?&]id=([^&]+)', job_link)
                            if match:
                                employer_job_id = unquote(match.group(1))

                    has_valid_api_id = bool(employer_job_id)

                    # If still no ID, create a synthetic one — used ONLY for folder
                    # naming and dedup. It must NOT be passed to the GraphQL API as
                    # employerJobId (Indeed returns empty results silently).
                    if not employer_job_id:
                        employer_job_id = f"{clean_title}_{date_formatted}".replace(' ', '_')

                    jobs.append({
                        'id': employer_job_id,
                        'has_valid_api_id': has_valid_api_id,
                        'title': title,
                        'title_clean': clean_title,
                        'status': status,
                        'date': date_formatted,
                        'total_candidates': total_candidates,
                        'job_link': job_link
                    })

                except (NoSuchElementException, StaleElementReferenceException, ValueError):
                    continue

        except Exception as e:
            print(f"❌ Job extraction error: {e}")

        return jobs

    def _has_next_page(self) -> bool:
        """Check whether the Next button is active"""
        try:
            next_btn = self.driver.find_element(By.ID, "ejsJobListPaginationNextBtn")
            return not next_btn.get_attribute('disabled')
        except NoSuchElementException:
            return False

    def _click_next_page(self) -> bool:
        """Click the Next button"""
        try:
            next_btn = self.driver.find_element(By.ID, "ejsJobListPaginationNextBtn")
            if next_btn.get_attribute('disabled'):
                return False

            # Scroll to the button and click
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", next_btn)
            time.sleep(0.5)
            next_btn.click()
            time.sleep(3)

            # Wait for the table to reload
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "tr[data-testid='job-row']"))
            )
            return True
        except Exception as e:
            print(f"      Pagination error: {e}")
        return False

    def fetch_all_jobs(self) -> list:
        """Fetch all jobs from HTML table with pagination"""
        print("\nFetching job list...")

        # Build the URL with status filters
        status_params = []
        if 'ACTIVE' in self.job_statuses:
            status_params.append('open')
        if 'PAUSED' in self.job_statuses:
            status_params.append('paused')
        if 'CLOSED' in self.job_statuses:
            status_params.append('closed')

        status_str = ','.join(status_params)
        jobs_url = f"https://employers.indeed.com/jobs?status={status_str}"

        print(f"   URL: {jobs_url}")
        self.driver.get(jobs_url)
        time.sleep(4)

        try:
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "tr[data-testid='job-row']"))
            )
        except TimeoutException:
            print("Job table not found")
            return []

        # Close any modals that might appear
        self._close_modals()

        # Read the total count shown on the page
        try:
            total_text = self.driver.find_element(By.CSS_SELECTOR, "span[data-testid='job-count'], .css-1f9ew9y").text
            print(f"   Total shown on page: {total_text}")
        except NoSuchElementException:
            pass

        all_jobs = []
        page = 1

        while True:
            print(f"   Page {page}...")

            # Wait for rows to load
            time.sleep(1)

            jobs = self._extract_jobs_from_page()

            # No need to filter by status here - the URL already filters
            all_jobs.extend(jobs)

            print(f"      {len(jobs)} jobs on this page (total: {len(all_jobs)})")

            if self._has_next_page():
                if not self._click_next_page():
                    break
                page += 1
                time.sleep(1)  # Wait for loading
            else:
                break

        print(f"\n{len(all_jobs)} jobs fetched")

        # Display the list of jobs found
        print("\nJob list:")
        print("-" * 60)
        for i, job in enumerate(all_jobs, 1):
            status_icon = "[O]" if job['status'] == 'ACTIVE' else "[P]" if job['status'] == 'PAUSED' else "[F]"
            print(f"   {i:3}. {status_icon} {job['title_clean']}")
            if job['date']:
                print(f"        Date: {job['date']} | Candidates: {job['total_candidates']}")
        print("-" * 60)

        return all_jobs

    def _find_existing_job_folders(self, jobs: list) -> dict:
        """Find which jobs already have folders in downloads

        Returns dict mapping job_id -> folder info, ensuring each folder is matched to only one job.
        Matching priority:
        1. Exact name + exact date (score 4) - must match
        2. Exact name only (score 2) - only if folder has no date or job has no date
        3. Partial name + exact date (score 3)
        4. Partial name only (score 1) - only if folder has no date or job has no date

        IMPORTANT: If both job and folder have dates, they MUST match for name matching.
        """
        existing = {}
        download_path = Path(self.download_folder)

        if not download_path.exists():
            return existing

        # Normalize function for comparison (removes accents for comparison only)
        def normalize(s):
            import unicodedata
            s = unicodedata.normalize('NFKD', s).encode('ASCII', 'ignore').decode('ASCII')
            s = re.sub(r'[^a-z0-9\s]', '', s.lower())
            s = re.sub(r'\s+', ' ', s).strip()
            return s

        # Get all folders with their info
        folder_info = {}
        for folder in download_path.iterdir():
            if folder.is_dir():
                # Format: "Nom du job (DD-MM-YYYY)"
                match = re.match(r'(.+) \((\d{2}-\d{2}-\d{4})\)$', folder.name)
                if match:
                    job_name = match.group(1)
                    date = match.group(2)
                    clean_name = self._clean_job_title(job_name)
                    normalized = normalize(clean_name)
                    # Load stats from stats.json if exists, otherwise count PDFs
                    stats = self._load_job_stats(folder)
                    if stats:
                        cv_count = stats.get('processed', 0)
                        total_recovered = stats.get('total_recovered', cv_count)
                    else:
                        # Fallback: count PDFs + no_cv.txt entries
                        cv_count = len(list(folder.rglob('*.pdf')))
                        no_cv_file = folder / 'no_cv.txt'
                        if no_cv_file.exists():
                            with open(no_cv_file, 'r', encoding='utf-8') as f:
                                cv_count += sum(1 for line in f if line.strip())
                        total_recovered = cv_count  # No stats, assume all processed
                    folder_info[folder.name] = {
                        'original_name': job_name,
                        'clean_name': clean_name,
                        'normalized_name': normalized,
                        'date': date,
                        'cv_count': cv_count,
                        'total_recovered': total_recovered,
                        'matched_job_id': None  # Track which job matched this folder
                    }
                else:
                    clean_name = self._clean_job_title(folder.name)
                    normalized = normalize(clean_name)
                    # Load stats from stats.json if exists, otherwise count PDFs
                    stats = self._load_job_stats(folder)
                    if stats:
                        cv_count = stats.get('processed', 0)
                        total_recovered = stats.get('total_recovered', cv_count)
                    else:
                        # Fallback: count PDFs + no_cv.txt entries
                        cv_count = len(list(folder.rglob('*.pdf')))
                        no_cv_file = folder / 'no_cv.txt'
                        if no_cv_file.exists():
                            with open(no_cv_file, 'r', encoding='utf-8') as f:
                                cv_count += sum(1 for line in f if line.strip())
                        total_recovered = cv_count  # No stats, assume all processed
                    folder_info[folder.name] = {
                        'original_name': folder.name,
                        'clean_name': clean_name,
                        'normalized_name': normalized,
                        'date': None,
                        'cv_count': cv_count,
                        'total_recovered': total_recovered,
                        'matched_job_id': None
                    }

        print(f"\n   {len(folder_info)} folders found in '{self.download_folder}/'")

        # Match jobs with folders - each folder can only match ONE job
        # First pass: match jobs that have exact name + date match (highest priority)
        matched_count = 0
        for job in jobs:
            job_clean = job.get('title_clean', self._clean_job_title(job['title']))
            job_normalized = normalize(job_clean)
            job_date = job.get('date', '')
            job_id = job['id']

            # Only look for exact name + date matches in first pass
            if not job_date:
                continue

            for folder_name, info in folder_info.items():
                if info['matched_job_id'] is not None:
                    continue

                folder_normalized = info['normalized_name']
                folder_date = info['date']

                # Exact name + exact date match
                if job_normalized == folder_normalized and folder_date == job_date:
                    folder_info[folder_name]['matched_job_id'] = job_id
                    existing[job_id] = {
                        'title': job['title'],
                        'title_clean': job_clean,
                        'folder': folder_name,
                        'cv_count': info['cv_count'],
                        'total_recovered': info['total_recovered'],
                        'total_candidates': job.get('total_candidates', 0),
                        'date': job_date
                    }
                    matched_count += 1
                    break

        print(f"   {matched_count} folders match jobs")

        # Second pass: for jobs without date match, try name-only match (only for folders without date)
        for job in jobs:
            job_id = job['id']
            if job_id in existing:
                continue  # Already matched

            job_clean = job.get('title_clean', self._clean_job_title(job['title']))
            job_normalized = normalize(job_clean)
            job_date = job.get('date', '')

            best_match = None
            best_match_score = 0

            for folder_name, info in folder_info.items():
                if info['matched_job_id'] is not None:
                    continue

                folder_normalized = info['normalized_name']
                folder_date = info['date']

                # If both have dates and they don't match, skip this folder
                if job_date and folder_date and job_date != folder_date:
                    continue

                score = 0

                # Exact name match
                if job_normalized == folder_normalized:
                    # Higher score if dates match or no dates to compare
                    if job_date and folder_date and job_date == folder_date:
                        score = 4  # Best: exact name + exact date
                    elif not job_date or not folder_date:
                        score = 2  # Good: exact name, one or both missing date
                    # If dates don't match, score stays 0 (skip)

                # Partial match (one contains the other) - only for longer names
                elif len(job_normalized) >= 10 and len(folder_normalized) >= 10:
                    if job_normalized in folder_normalized or folder_normalized in job_normalized:
                        if job_date and folder_date and job_date == folder_date:
                            score = 3  # Good: partial name + exact date
                        elif not job_date or not folder_date:
                            score = 1  # OK: partial name, one or both missing date
                        # If dates don't match, score stays 0 (skip)

                if score > best_match_score:
                    best_match_score = score
                    best_match = folder_name

            # If we found a match, mark the folder as matched
            if best_match and best_match_score > 0:
                folder_info[best_match]['matched_job_id'] = job_id
                existing[job_id] = {
                    'title': job['title'],
                    'title_clean': job_clean,
                    'folder': best_match,
                    'cv_count': folder_info[best_match]['cv_count'],
                    'total_recovered': folder_info[best_match]['total_recovered'],
                    'total_candidates': job.get('total_candidates', 0),
                    'date': job_date
                }

        return existing

    def _ask_skip_existing_jobs(self, jobs: list, existing_jobs: dict) -> list:
        """Ask user which existing jobs to skip

        Args:
            jobs: List of all jobs
            existing_jobs: Dict of jobs that have existing folders
        """
        if not existing_jobs:
            return jobs

        print("\n" + "=" * 60)
        print("JOBS ALREADY PRESENT IN THE DOWNLOADS FOLDER:")
        print("=" * 60)

        jobs_with_new = []
        jobs_complete = []

        for job_id, info in existing_jobs.items():
            cv_count = info['cv_count']  # processed
            total_recovered = info.get('total_recovered', cv_count)  # what API returned
            total_announced = info['total_candidates']  # what job listing shows
            # Use cleaned title for display
            title = info.get('title_clean', info['title'])
            folder = info['folder']
            date = info.get('date', '')

            # Format title with date for clarity
            title_with_date = f"{title} ({date})" if date else title

            # Compare with total_recovered (not total_announced) to determine completion
            if cv_count < total_recovered:
                jobs_with_new.append((job_id, info))
                print(f"   [NEW] {title_with_date}")
                print(f"         Folder: {folder}")
                print(f"         {cv_count} processed / {total_recovered} fetched (+{total_recovered - cv_count} remaining)")
            else:
                jobs_complete.append((job_id, info))
                # Show both recovered and announced if different
                if total_recovered < total_announced:
                    print(f"   [OK]  {title_with_date} ({cv_count}/{total_recovered} fetched, {total_announced} posted)")
                else:
                    print(f"   [OK]  {title_with_date} ({cv_count}/{total_announced})")

        print()
        if jobs_with_new:
            print(f"   {len(jobs_with_new)} jobs with new candidates")
        print(f"   {len(jobs_complete)} complete jobs")
        print()
        print("Options:")
        print("   [S] SkipAll - Skip ALL existing jobs")
        print("   [N] NewOnly - Only download jobs with new candidates")
        print("   [K] KeepAll - Download every job anyway")
        print()

        while True:
            choice = input("Your choice (S/N/K): ").strip().upper()

            if choice == 'S':
                # Skip all existing
                jobs_to_skip = set(existing_jobs.keys())
                filtered_jobs = [j for j in jobs if j['id'] not in jobs_to_skip]
                print(f"\n{len(jobs_to_skip)} jobs skipped")
                return filtered_jobs

            elif choice == 'N':
                # Only jobs with new candidates
                jobs_with_new_ids = set(job_id for job_id, _ in jobs_with_new)
                filtered_jobs = [j for j in jobs if j['id'] in jobs_with_new_ids]
                print(f"\n{len(jobs_complete)} complete jobs skipped, {len(filtered_jobs)} to process")
                return filtered_jobs

            elif choice == 'K':
                # Keep all
                print("\nAll jobs will be processed")
                return jobs

            print("Invalid choice, type S, N or K")

    def _filter_old_jobs(self, jobs: list) -> list:
        """Filter out jobs older than 2 years (Indeed archives candidate data after ~2 years)"""
        from datetime import datetime, timedelta

        two_years_ago = datetime.now() - timedelta(days=730)  # ~2 years
        filtered_jobs = []
        old_jobs_count = 0

        for job in jobs:
            job_date = job.get('date', '')
            if job_date:
                try:
                    # Parse date format: DD-MM-YYYY
                    parsed_date = datetime.strptime(job_date, '%d-%m-%Y')
                    if parsed_date < two_years_ago:
                        old_jobs_count += 1
                        continue
                except ValueError:
                    pass
            filtered_jobs.append(job)

        if old_jobs_count > 0:
            print(f"\n   {old_jobs_count} jobs older than 2 years skipped (data archived by Indeed)")

        return filtered_jobs

    def run_all_jobs(self):
        """Process all jobs"""
        jobs = self.fetch_all_jobs()

        if not jobs:
            print("No jobs found")
            return

        # Filter out jobs older than 2 years (Indeed archives data)
        jobs = self._filter_old_jobs(jobs)

        if not jobs:
            print("No recent jobs to process (all > 2 years)")
            return

        # Check for existing folders (compare by name, not checkpoint)
        existing_jobs = self._find_existing_job_folders(jobs)

        if existing_jobs:
            jobs = self._ask_skip_existing_jobs(jobs, existing_jobs)

        if not jobs:
            print("No jobs to process!")
            return

        print(f"\n{len(jobs)} jobs to process")
        print("=" * 60)

        for i, job in enumerate(jobs):
            title_display = job.get('title_clean', job['title'])
            print(f"\n[{i+1}/{len(jobs)}] {title_display}")
            print(f"         Status: {job['status']}, Date: {job['date'] or 'N/A'}, Candidates: {job.get('total_candidates', '?')}")

            self.current_job_id = job['id']
            self.current_job_name = job['title']
            self._create_job_folder(job['title'], job['date'])

            if self.mode == 'backend':
                # Synthetic IDs (title + date) cannot be passed to the GraphQL
                # API as employerJobId — Indeed returns empty results silently.
                # Skip the job so the user can fall back to Frontend mode for it.
                if not job.get('has_valid_api_id', True):
                    print(f"   ⚠ Skipping — no Indeed employerJobId on the jobs-table link. Use Frontend mode for this job.")
                    self.stats['archived'] += 1
                    continue
                # Close any modals that might appear
                self._close_modals()
                self._download_all_candidates_api(job.get('total_candidates', 0))
            else:
                # Navigate to job
                self.driver.get(f"https://employers.indeed.com/candidates?selectedJobs={job['id']}")
                time.sleep(3)
                # Close any modals that might appear
                self._close_modals()
                self._download_all_candidates_frontend()

            self._save_checkpoint(job_id=job['id'])
            print(f"   Job finished: {title_display}")

    # ==================== MAIN ====================

    def print_statistics(self):
        """Print final statistics"""
        print("\n" + "=" * 60)
        print("STATISTICS")
        print("=" * 60)
        print(f"Total processed:  {self.stats['total_processed']}")
        print(f"Downloaded:       {self.stats['downloaded']}")
        print(f"Skipped:          {self.stats['skipped']}")
        print(f"Failed:           {self.stats['failed']}")
        if self.stats.get('app_data_downloaded', 0) > 0:
            print(f"App data saved:   {self.stats['app_data_downloaded']}")
        if self.stats['archived'] > 0:
            print(f"Archived jobs:    {self.stats['archived']} (data unavailable)")

        if self.start_time:
            elapsed = time.time() - self.start_time
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            seconds = int(elapsed % 60)
            print(f"\nTotal time:       {hours}h {minutes}m {seconds}s")

            if self.stats['downloaded'] > 0:
                avg = elapsed / self.stats['downloaded']
                print(f"Avg/CV:           {avg:.1f}s")

        print("=" * 60)

        # Generate report file
        self._generate_report()

    def _generate_report(self):
        """Generate a summary report file by scanning all job folders in downloads"""
        report_file = Path(self.download_folder) / 'download_report.txt'
        timestamp = datetime.now().strftime('%d-%m-%Y %H:%M:%S')

        # Scan all job folders in downloads
        download_path = Path(self.download_folder)
        job_folders = []

        for folder in sorted(download_path.iterdir()):
            if folder.is_dir():
                # Count PDFs
                pdf_count = len(list(folder.rglob('*.pdf')))

                # Count no_cv.txt entries
                no_cv_count = 0
                no_cv_file = folder / 'no_cv.txt'
                if no_cv_file.exists():
                    with open(no_cv_file, 'r', encoding='utf-8') as f:
                        no_cv_count = sum(1 for line in f if line.strip())

                # Load stats.json if exists
                stats = self._load_job_stats(folder)

                job_folders.append({
                    'name': folder.name,
                    'pdf_count': pdf_count,
                    'no_cv_count': no_cv_count,
                    'stats': stats
                })

        if not job_folders:
            print("No job folder found in downloads/")
            return

        # Calculate totals
        total_pdfs = sum(j['pdf_count'] for j in job_folders)
        total_no_cv = sum(j['no_cv_count'] for j in job_folders)
        total_announced = sum(j['stats'].get('total_announced', 0) if j['stats'] else 0 for j in job_folders)
        total_recovered = sum(j['stats'].get('total_recovered', 0) if j['stats'] else 0 for j in job_folders)
        total_archived = total_announced - total_recovered

        with open(report_file, 'w', encoding='utf-8') as f:
            f.write("=" * 70 + "\n")
            f.write("GLOBAL REPORT - INDEED CV DOWNLOADER\n")
            f.write("=" * 70 + "\n")
            f.write(f"Date: {timestamp}\n")
            f.write(f"Job folders: {len(job_folders)}\n")
            f.write("\n")

            # Per-job stats
            f.write("-" * 70 + "\n")
            f.write("PER-JOB DETAILS\n")
            f.write("-" * 70 + "\n\n")

            for i, job in enumerate(job_folders, 1):
                f.write(f"{i}. {job['name']}\n")
                if job['stats']:
                    announced = job['stats'].get('total_announced', 0)
                    recovered = job['stats'].get('total_recovered', 0)
                    archived = announced - recovered
                    f.write(f"   Candidates posted:   {announced}\n")
                    f.write(f"   Candidates fetched:  {recovered}\n")
                    if archived > 0:
                        f.write(f"   Archived/lost:       {archived}\n")
                f.write(f"   CVs downloaded:      {job['pdf_count']}\n")
                if job['no_cv_count'] > 0:
                    f.write(f"   Without CV:          {job['no_cv_count']}\n")
                f.write("\n")

            # Summary
            f.write("-" * 70 + "\n")
            f.write("GLOBAL SUMMARY\n")
            f.write("-" * 70 + "\n")
            f.write(f"Total jobs:            {len(job_folders)}\n")
            f.write(f"Candidates posted:     {total_announced}\n")
            f.write(f"Candidates fetched:    {total_recovered}\n")
            if total_archived > 0:
                f.write(f"Archived/lost:         {total_archived}\n")
            f.write(f"CVs downloaded:        {total_pdfs}\n")
            f.write(f"Without CV:            {total_no_cv}\n")
            f.write("=" * 70 + "\n")

        print(f"\nReport generated: {report_file}")

    def run(self):
        """Main execution"""
        try:
            self.show_menu()

            if not self.setup_chrome():
                if self.log:
                    self.log.event('setup_chrome', {'ok': False})
                return
            if self.log:
                self.log.event('setup_chrome', {'ok': True, 'has_api_key': bool(self.api_key), 'has_ctk': bool(self.ctk)})

            self.start_time = time.time()

            if self.job_mode == 'single':
                if self.mode == 'backend':
                    self.run_backend_single_job()
                else:
                    self.run_frontend_single_job()
            else:
                self.run_all_jobs()

            self.print_statistics()

        except KeyboardInterrupt:
            print("\n\n⚠️ Interrupted by user")
            if self.log:
                self.log.info('interrupted_by_user')
            self.print_statistics()

        except Exception as e:
            print(f"\n❌ Error: {e}")
            traceback.print_exc()
            if self.log:
                self.log.error('fatal_in_run', e)

        finally:
            if self.log:
                # Dump final stats for the bug-report bundle.
                self.log.event('final_stats', dict(self.stats))
            if self.driver:
                # In attach mode don't kill the Chrome subprocess — the user
                # may want to keep browsing. Just detach Selenium.
                if self.browser_launch == 'attach':
                    try:
                        # quit() would tear down the browser; we only want
                        # to release our session. Selenium doesn't expose a
                        # "detach without quitting" method, but dropping the
                        # reference keeps Chrome alive because we spawned it
                        # as a detached subprocess above.
                        self.driver = None
                    except Exception:
                        pass
                    print("\n(Chrome window left open — close it yourself when you're done.)")
                else:
                    input("\nPress Enter to close Chrome...")
                    self.driver.quit()


def _install_crash_logger(logger: RunLogger):
    """Catch anything that bypasses run()'s try/except (e.g., during import
    or menu input) so the log captures the full traceback before the .exe
    console closes."""
    def _hook(exc_type, exc_value, exc_tb):
        try:
            logger.error('uncaught_exception', exc_value)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _hook


def main():
    # Resolve log folder the same way IndeedDownloader does so we write to
    # the same place. Default "logs" is relative to cwd, which for a .exe
    # is typically the folder the user double-clicked.
    log_folder = Path(os.getenv('LOG_FOLDER', 'logs'))
    logger = RunLogger(log_folder)
    _install_crash_logger(logger)

    # Mirror every print() into the log file so the user's console output
    # is preserved verbatim. Don't tee stderr — tqdm's progress bars would
    # fill the log with carriage-return spam.
    sys.stdout = _TeeStream(sys.stdout, logger.raw_file)

    print(f"📝 Run log: {logger.path}")
    print(f"   (if something breaks, send the file at: {logger.latest_path})")
    print()

    downloader = IndeedDownloader(log=logger)
    try:
        downloader.run()
    finally:
        logger.close()
        print(f"\n📝 Log saved: {logger.latest_path}")


if __name__ == "__main__":
    main()
