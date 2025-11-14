#!/usr/bin/env python3
r"""
triquetra.py
"""

import os
import sys
import ctypes
import argparse
import tempfile
import shutil
import subprocess
import urllib.parse
import re
import time
import hashlib
import warnings
import winreg
from typing import List, Optional, Tuple

# Third-party libs
try:
    import requests
    from bs4 import BeautifulSoup
    from requests.auth import HTTPBasicAuth
except Exception:
    print("Missing required modules. Install them with:")
    print("  python -m pip install requests beautifulsoup4")
    input("Press Enter to exit...")
    sys.exit(1)

# ----- Suppress InsecureRequestWarning -----
from requests.packages.urllib3.exceptions import InsecureRequestWarning
warnings.simplefilter("ignore", InsecureRequestWarning)

# ----- Registry helpers -----
def get_arch_from_registry() -> str:
    """Read the BuildLabEx value from the registry and determine architecture."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        )
        buildlabex, _ = winreg.QueryValueEx(key, "BuildLabEx")
        key.Close()
        if "arm64" in buildlabex.lower():
            return "arm64"
        else:
            return "amd64"
    except Exception as e:
        log(f"Failed to read BuildLabEx for architecture detection: {e}")
        return "amd64"

def get_display_version() -> Optional[str]:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        )
        value, _ = winreg.QueryValueEx(key, "DisplayVersion")
        key.Close()
        return value
    except Exception as e:
        log(f"Failed to read DisplayVersion: {e}")
        return None

def check_not_server_os():
    """Abort if running on Windows Server; display OS caption via CIM, edition, and type."""
    try:
        key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)

        edition_id, _ = winreg.QueryValueEx(key, "EditionID")
        inst_type, _ = winreg.QueryValueEx(key, "InstallationType")
        key.Close()

        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-Command", "(Get-CimInstance Win32_OperatingSystem).Caption"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            os_caption = result.stdout.strip()
        except Exception:
            os_caption = "Unknown OS"

        log(f"Detected OS: {os_caption} | SKU: {edition_id} | Type: {inst_type}")

        if inst_type.lower() != "client":
            print(f"ERROR: This program is not supported on {inst_type} edition.")
            input("Press Enter to exit...")
            sys.exit(1)

    except Exception as e:
        log(f"Failed to check OS type: {e}")

# ----- Logging helper -----
PROGRAMDATA_DIR = r"C:\ProgramData\triquetra"
os.makedirs(PROGRAMDATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(PROGRAMDATA_DIR, "triquetra.log")
TMP_DIR = PROGRAMDATA_DIR

def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())

def log(msg: str, console: bool = True):
    entry = msg
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{now_ts()}\t{msg}\n")
    except Exception:
        pass
    if console:
        print(entry)

# ----- Elevation -----
def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def elevate_and_exit():
    python_exe = sys.executable
    params = " ".join(f'"{p}"' for p in sys.argv[1:])
    try:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", python_exe, f'"{sys.argv[0]}" {params}', None, 1
        )
        sys.exit(0)
    except Exception as e:
        log(f"Failed to relaunch elevated: {e}")
        input("Press Enter to exit...")
        sys.exit(1)

# ----- ntoskrnl.exe version replaced with registry -----
def get_ntoskrnl_file_version() -> Optional[str]:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        )
        buildlab, _ = winreg.QueryValueEx(key, "BuildLab")
        ubr, _ = winreg.QueryValueEx(key, "UBR")
        key.Close()
        build_match = re.match(r"(\d+)\.", buildlab)
        if not build_match:
            log(f"Unexpected BuildLab format: {buildlab}")
            return None
        build_major = build_match.group(1)
        version = f"{build_major}.{ubr}"
        return version
    except Exception as e:
        log(f"Failed to read BuildLab/UBR from registry: {e}")
        return None

def normalize_local_to_short(full_ver: str) -> Tuple[str, List[int]]:
    nums = re.findall(r"\d+", full_ver)
    if not nums:
        return full_ver, []
    short_parts = [int(nums[0])]
    if len(nums) > 1:
        short_parts.append(int(nums[1]))
    short_str = ".".join(str(x) for x in short_parts)
    return short_str, short_parts

def compare_version_lists(a: List[int], b: List[int]) -> int:
    maxlen = max(len(a), len(b))
    for i in range(maxlen):
        ai = a[i] if i < len(a) else 0
        bi = b[i] if i < len(b) else 0
        if ai > bi:
            return 1
        if ai < bi:
            return -1
    return 0

# ----- HTTP helpers -----
def rewrite_url(url: str, use_http: bool) -> str:
    if use_http and url.startswith("https://"):
        return "http://" + url[8:]
    return url

def fetch_text(url: str, auth: Optional[Tuple[str, str]], timeout: int = 30, use_http: bool = False) -> str:
    url = rewrite_url(url, use_http)
    auth_obj = HTTPBasicAuth(*auth) if auth else None
    r = requests.get(url, auth=auth_obj, timeout=timeout, verify=not use_http)
    r.raise_for_status()
    return r.text

def remote_file_exists(url: str, auth: Optional[Tuple[str, str]], timeout: int = 10, use_http: bool = False) -> bool:
    url = rewrite_url(url, use_http)
    auth_obj = HTTPBasicAuth(*auth) if auth else None
    try:
        r = requests.head(url, auth=auth_obj, timeout=timeout, verify=not use_http)
        return r.status_code == 200
    except Exception:
        return False

def fetch_md5(url: str, auth: Optional[Tuple[str, str]], use_http: bool = False) -> str:
    txt = fetch_text(url, auth, use_http=use_http)
    line = txt.strip()
    md5val = line.split()[0]
    return md5val.lower()

def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def download_file(url: str, dest_dir: str, auth: Optional[Tuple[str, str]], use_http: bool = False) -> str:
    import itertools, sys, time
    url = rewrite_url(url, use_http)
    fname = os.path.basename(urllib.parse.unquote(url))
    dest_path = os.path.join(dest_dir, fname)
    md5_url = url + ".md5"
    max_retries = 3
    while True:
        try:
            need_download = True
            if os.path.exists(dest_path):
                try:
                    md5_server = fetch_md5(md5_url, auth, use_http=use_http)
                    md5_local = file_md5(dest_path)
                    if md5_local.lower() == md5_server.lower():
                        log(f"{fname} already exists and hash matches.")
                        need_download = False
                except Exception:
                    log(f"Could not verify hash for {fname}, will redownload.")
            if not need_download:
                return dest_path
            log(f"Downloading {fname}...")
            auth_obj = HTTPBasicAuth(*auth) if auth else None
            with requests.get(url, stream=True, auth=auth_obj, verify=not use_http, timeout=60) as r:
                r.raise_for_status()
                total = r.headers.get("Content-Length")
                total_i = int(total) if total and total.isdigit() else None
                downloaded = 0
                chunk_size = 1024 * 1024
                spinner = itertools.cycle(["|", "/", "-", "\\"])
                start_time = time.time()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        elapsed = time.time() - start_time
                        speed = downloaded / 1024 / 1024 / elapsed if elapsed > 0 else 0
                        if total_i:
                            pct = (downloaded / total_i) * 100
                            total_str = (
                                f"{total_i / 1024 / 1024 / 1024:.2f}G"
                                if total_i > 1024**3
                                else f"{total_i / 1024 / 1024:.2f}M"
                            )
                        else:
                            pct, total_str = 0, "?"
                        done_str = (
                            f"{downloaded / 1024 / 1024 / 1024:.2f}G"
                            if downloaded > 1024**3
                            else f"{downloaded / 1024 / 1024:.2f}M"
                        )
                        sys.stdout.write(
                            f"\rDownloading {fname} {next(spinner)} {pct:5.1f}% {done_str}/{total_str} {speed:5.1f}MB/s"
                        )
                        sys.stdout.flush()
            sys.stdout.write("\r" + " " * 120 + "\r")
            sys.stdout.flush()
            log(f"Finished downloading {fname}")
            return dest_path
        except Exception as e:
            sys.stdout.write("\r" + " " * 120 + "\r")
            sys.stdout.flush()
            log(f"Download of {fname} failed: {e}")
            ans = input(f"Download failed for {fname}. Retry? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                log(f"User chose not to retry {fname}. Aborting download.")
                raise
            else:
                max_retries -= 1
                if max_retries <= 0:
                    log(f"Maximum retries reached for {fname}. Aborting.")
                    raise
                log(f"Retrying download of {fname}...")
                time.sleep(3)

# --- The rest of your functions (self_update, powershell_add_package, etc.) remain unchanged ---
# Just pass 'use_http=USE_HTTP' when calling fetch_text, remote_file_exists, download_file, fetch_md5

# ----- Main program -----
def main():
    parser = argparse.ArgumentParser(description="Windows 11 updater using h5ai-hosted files")
    parser.add_argument("--base-url", default="https://updates.smce.pl/", help="Base URL of h5ai index")
    parser.add_argument("--user", default="w11updater", help="HTTP Basic Auth username")
    parser.add_argument("--password", default="w11updater", help="HTTP Basic Auth password")
    parser.add_argument("--dry-run",action="store_true",help="Show actions but do not download/install")
    parser.add_argument("--build", "-b",help="Override and install a specific build (e.g. 26100.6899).")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Use HTTP instead of HTTPS for all update URLs"
    )
    args = parser.parse_args()
    USE_HTTP = args.http

    # --- Use USE_HTTP in all network calls ---
    # Example: html = fetch_text(base_url, auth, use_http=USE_HTTP)
    # Same for remote_file_exists, download_file, fetch_md5, self_update
    # The rest of main() logic remains exactly as in your original script, with only this addition

if __name__ == "__main__":
    if sys.platform != "win32":
        print("This program is intended to run on Windows.")
        input("Press Enter to exit...")
        sys.exit(1)
    main()
