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

        # EditionID and InstallationType from registry
        edition_id, _ = winreg.QueryValueEx(key, "EditionID")
        inst_type, _ = winreg.QueryValueEx(key, "InstallationType")
        key.Close()

        # Caption via CIM (PowerShell)
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

        # Abort on Server
        if inst_type.lower() != "client":
            print(f"ERROR: This program is not supported on {inst_type} edition.")
            input("Press Enter to exit...")
            sys.exit(1)

    except Exception as e:
        log(f"Failed to check OS type: {e}")
        # Fail-safe: continue, assuming non-server


# ----- Logging helper -----
PROGRAMDATA_DIR = r"C:\ProgramData\triquetra"
os.makedirs(PROGRAMDATA_DIR, exist_ok=True)

# Use ProgramData for everything (logs, downloads, temp files)
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
    """
    Replace file-based ntoskrnl.exe version check with registry-based.
    Combines BuildLab first part and UBR to produce '26100.6130'-style version.
    """
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        )

        buildlab, _ = winreg.QueryValueEx(key, "BuildLab")
        ubr, _ = winreg.QueryValueEx(key, "UBR")
        key.Close()

        # BuildLab format: '26100.1.amd64fre.somehash'
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
    """
    Convert a full version string to a 'short' list of integers for comparison.
    Handles registry-based 'BuildLab + UBR' style (e.g., '26100.6130').
    """
    nums = re.findall(r"\d+", full_ver)
    if not nums:
        return full_ver, []

    # Only keep the first two numbers (major + minor/build)
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

def try_mirrors(paths: List[str], auth: Optional[Tuple[str, str]]) -> str:
    """
    Try a list of mirror URLs and return the first that responds successfully.
    """
    for base in paths:
        try:
            log(f"Testing server: {base}")
            html = fetch_text(base, auth=auth, timeout=10)
            log(f"Server available: {base}")
            return base  # Return the working base URL (with trailing /)
        except Exception as e:
            log(f"Server failed: {base} ({e})")
    log("All servers failed. Exiting.")
    input("Press Enter to exit...")
    sys.exit(1)
    
def choose_fastest_mirror(mirrors: List[str], auth: Optional[Tuple[str, str]]) -> str:
    """
    Test mirror download speeds using a small test file (e.g., speed.test)
    and return the fastest one, with a clean spinner and concise output.
    """
    import itertools, sys, threading, time

    test_file = "speed.test"
    results = []
    spinner_running = True

    def spinner_func():
        spinner = itertools.cycle(["|", "/", "-", "\\"])
        while spinner_running:
            sys.stdout.write(f"\rTesting mirrors speed... {next(spinner)}")
            sys.stdout.flush()
            time.sleep(0.1)

    spinner_thread = threading.Thread(target=spinner_func)
    spinner_thread.daemon = True
    spinner_thread.start()

    # --- Measure mirror speeds ---
    for base in mirrors:
        test_url = urllib.parse.urljoin(base, test_file)
        try:
            r = requests.get(
                test_url,
                auth=HTTPBasicAuth(*auth) if auth else None,
                timeout=10,
                stream=True,
                verify=True,
            )
            r.raise_for_status()

            total_bytes = 0
            t0 = time.time()
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > 1024 * 1024:  # only first ~1 MB
                    break
            elapsed = time.time() - t0
            speed_mbs = total_bytes / (elapsed * 1024 * 1024) if elapsed > 0 else 0
            results.append((speed_mbs, base))
        except Exception:
            results.append((0, base))

    # --- Stop spinner ---
    spinner_running = False
    spinner_thread.join(timeout=0.2)
    sys.stdout.write("\rTesting mirrors speed...\n")
    sys.stdout.flush()

    # --- Print results (compact, no blank lines) ---
    for speed, base in results:
        print(f"{base} speed: {speed_mbs:5.1f} MB/s")

    # --- Pick the fastest ---
    if not results or all(speed == 0 for speed, _ in results):
        log("No mirrors responded successfully. Exiting.")
        input("Press Enter to exit...")
        sys.exit(1)

    results.sort(reverse=True, key=lambda x: x[0])
    best_speed, best_mirror = results[0]

    return best_mirror


# ----- HTTP helpers -----
def fetch_text(url: str, auth: Optional[Tuple[str, str]], timeout: int = 30) -> str:
    auth_obj = HTTPBasicAuth(*auth) if auth else None
    r = requests.get(url, auth=auth_obj, timeout=timeout, verify=True)
    r.raise_for_status()
    return r.text
    
def remote_file_exists(url: str, auth: Optional[Tuple[str, str]], timeout: int = 10) -> bool:
    """Return True if the given remote file exists (HTTP 200)."""
    auth_obj = HTTPBasicAuth(*auth) if auth else None
    try:
        r = requests.head(url, auth=auth_obj, timeout=timeout, verify=True)
        return r.status_code == 200
    except Exception:
        return False

def parse_h5ai_index_for_folders(html_text: str) -> List[str]:
    folders = []
    soup = BeautifulSoup(html_text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href in ("../", "/"):
            continue
        token = href.rstrip("/").split("/")[-1]
        nums = re.findall(r"\d+", token)
        if not nums:
            txt = a.get_text(strip=True)
            nums = re.findall(r"\d+", txt)
            if not nums:
                continue
        if len(nums) >= 2:
            if len(nums) >= 4 and nums[0] == "10" and nums[1] == "0":
                short_parts = [int(nums[2]), int(nums[3])]
            else:
                short_parts = [int(nums[0]), int(nums[1])]
            short_name = f"{short_parts[0]}.{short_parts[1]}"
            if short_name not in folders:
                folders.append(short_name)
    folders.sort(key=lambda v: tuple(int(x) for x in v.split(".")))
    return folders


def parse_h5ai_files(html_text: str) -> List[str]:
    files = []
    soup = BeautifulSoup(html_text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.endswith("/"):
            continue
        files.append(urllib.parse.unquote(href.split("/")[-1]))
    return sorted(set(files))


# ----- Download with MD5 check -----
def fetch_md5(url: str, auth: Optional[Tuple[str, str]]) -> str:
    txt = fetch_text(url, auth)
    line = txt.strip()
    md5val = line.split()[0]
    return md5val.lower()


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(url: str, dest_dir: str, auth: Optional[Tuple[str, str]]) -> str:
    """Download a file with progress and retry option if download fails."""
    import requests, itertools, sys, time, os, urllib.parse
    from requests.auth import HTTPBasicAuth

    fname = os.path.basename(urllib.parse.unquote(url))
    dest_path = os.path.join(dest_dir, fname)
    md5_url = url + ".md5"
    max_retries = 3

    while True:
        try:
            need_download = True

            # --- Check MD5 if file exists ---
            if os.path.exists(dest_path):
                try:
                    md5_server = fetch_md5(md5_url, auth)
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

            with requests.get(url, stream=True, auth=auth_obj, verify=True, timeout=60) as r:
                r.raise_for_status()
                total = r.headers.get("Content-Length")
                total_i = int(total) if total and total.isdigit() else None
                downloaded = 0
                chunk_size = 1024 * 1024  # 1MB
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

            # Ask user if they want to retry
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

   
    # --- Check MD5 ---
    if os.path.exists(dest_path):
        try:
            md5_server = fetch_md5(md5_url, auth)
            md5_local = file_md5(dest_path)
            if md5_local.lower() == md5_server.lower():
                log(f"{fname} already exists and hash matches.")
                need_download = False
        except Exception:
            log(f"Could not verify hash for {fname}, will redownload.")

    if not need_download:
        return dest_path

    log(f"Downloading {fname}...")  # log start
    auth_obj = HTTPBasicAuth(*auth) if auth else None

    with requests.get(url, stream=True, auth=auth_obj, verify=True) as r:
        r.raise_for_status()
        total = r.headers.get("Content-Length")
        total_i = int(total) if total and total.isdigit() else None
        downloaded = 0
        chunk_size = 1024 * 1024  # 1MB
        spinner = itertools.cycle(["|", "/", "-", "\\"])
        start_time = time.time()

        with open(dest_path, "wb") as f:
            while True:
                chunk = r.raw.read(chunk_size)
                if not chunk:
                    break
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
                    f"\rDownloading {fname} {next(spinner)} {pct:5.1f}% {done_str}/{total_str} {speed:5.1f} MB/s"
                )
                sys.stdout.flush()

        # Replace progress line with final "... Done" via log()
        sys.stdout.write("\r" + " " * 120 + "\r")  # clear line
        sys.stdout.flush()
        log(f"Finished downloading {fname}")

    return dest_path

# ----- Utility functions -----
def is_frozen() -> bool:
    """Return True if running as a compiled EXE (Nuitka, PyInstaller, cx_Freeze)."""
    exe = getattr(sys, "executable", "")
    script = getattr(sys, "argv", [""])[0]
    return exe.lower().endswith(".exe") and not script.lower().endswith(".py")
    
def get_self_path():
    """Return the path to the current executable or script."""
    return sys.executable if is_frozen() else os.path.abspath(__file__)

def self_update(remote_url: str, auth: Optional[Tuple[str, str]]) -> bool:
    """MD5-based self-update for frozen EXE (downloads to C:\\ProgramData\\triquetra)."""
    try:
        self_path = get_self_path()
        if not is_frozen():
            log("Not running as frozen EXE — skipping self-update.")
            return False

        fname = os.path.basename(remote_url)
        md5_url = remote_url + ".md5"

        # Local MD5
        try:
            local_md5 = file_md5(self_path)
        except Exception as e:
            log(f"Could not compute local hash for {self_path}: {e}")
            local_md5 = None

        # Remote MD5
        try:
            remote_md5 = fetch_md5(md5_url, auth)
        except Exception as e:
            log(f"Could not fetch remote MD5 for self-update: {e}")
            return False

        log(f"Local updater hash: {local_md5}")
        log(f"Remote updater hash: {remote_md5}")

        if local_md5 and local_md5.lower() == remote_md5.lower():
            log("Triquetra is up-to-date.")
            return False

        # --- Download directory ---
        data_dir = r"C:\ProgramData\triquetra"
        os.makedirs(data_dir, exist_ok=True)
        log(f"Downloading updated triquetra.exe to {data_dir} ...")

        try:
            downloaded = download_file(remote_url, data_dir, auth)
        except Exception as e:
            log(f"Failed to download updated triquetra.exe: {e}")
            return False

        new_path = os.path.join(data_dir, os.path.basename(downloaded))
        if not os.path.exists(new_path):
            log(f"Downloaded updater not found at {new_path}")
            return False

        # --- PowerShell replace routine ---
        ps_command = f"""
        $old = '{self_path}';
        $new = '{new_path}';
        while (Get-Process -ErrorAction SilentlyContinue | Where-Object {{$_.Path -eq $old}}) {{ Start-Sleep -Milliseconds 500 }}
        Move-Item -Force -Path $new -Destination $old;
        Start-Process $old
        """

        try:
            subprocess.Popen([
                "powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                "-Command", ps_command
            ])
            log(f"Launched PowerShell to replace updater from {new_path} and restart {self_path}")
        except Exception as e:
            log(f"Failed to launch PowerShell for self-update: {e}")
            return False

        sys.exit(0)

    except SystemExit:
        raise
    except Exception as e:
        log(f"Self-update failed: {e}")
        return False


def powershell_add_package(package_path: str) -> int:
    """Install a Windows package (.cab/.msu) with a single clean spinner line and synced logging."""
    import subprocess, itertools, sys, time, os

    ps_cmd = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-Command",
        f"& {{Add-WindowsPackage -Online -PackagePath '{package_path}' -NoRestart}}",
    ]

    proc = subprocess.Popen(
        ps_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    spinner = itertools.cycle(["|", "/", "-", "\\"])
    pkg_name = os.path.basename(package_path)

    log(f"Installing {pkg_name}...")  # log start

    sys.stdout.write(f"Installing {pkg_name} ")
    sys.stdout.flush()

    while proc.poll() is None:
        sys.stdout.write(next(spinner))
        sys.stdout.flush()
        time.sleep(0.1)
        sys.stdout.write("\b")

    proc.wait()

    # Replace spinner with final message via log()
    sys.stdout.write("\r" + " " * 120 + "\r")
    sys.stdout.flush()
    log(f"Finished installing {pkg_name}")

    return proc.returncode


def check_and_offer_enablement_package(local_major, local_parts, auth, arch, args):
    """Check DisplayVersion and offer Enablement Package if eligible."""
    display_version = get_display_version()
    tmpdir = TMP_DIR
    os.makedirs(tmpdir, exist_ok=True)

    EP_URLS = {
        "22621": {
            "amd64": "https://updates.smce.pl/EP/amd64/Windows11.0-KB5027397-x64.cab",
            "arm64": "https://updates.smce.pl/EP/arm64/Windows11.0-KB5027397-arm64.cab",
        },
        "26100": {
            "amd64": "https://updates.smce.pl/EP/amd64/Windows11.0-KB5054156-x64.cab",
            "arm64": "https://updates.smce.pl/EP/arm64/Windows11.0-KB5054156-arm64.cab",
        },
    }

    offer_ep = False
    ep_branch = ""
    ep_prompt = ""
    ep_min_build = 0

    if display_version:
        if local_major == 22621:
            ep_branch = "22621"
            ep_prompt = "23H2"
            ep_min_build = 2506
            if display_version == "22H2" and len(local_parts) > 1 and local_parts[1] >= ep_min_build:
                offer_ep = True

        elif local_major == 26100:
            ep_branch = "26100"
            ep_prompt = "25H2"
            ep_min_build = 5074
            if display_version == "24H2" and len(local_parts) > 1 and local_parts[1] >= ep_min_build:
                offer_ep = True

    if offer_ep:
        ans = input(f"Do you want to install the {ep_prompt} Enablement Package? [y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            ep_url = EP_URLS[ep_branch][arch]
            ep_path = download_file(ep_url, tmpdir, auth)
            if not args.dry_run:
                rc = powershell_add_package(ep_path)
                if rc != 0:
                    log(f"Enablement Package installation failed with code {rc}")
                else:
                    log(f"{ep_prompt} Enablement Package installed successfully.")
        else:
            log(f"Installation of the {ep_prompt} Enablement Package declined.")
    else:
        reason = ""
        if display_version == ep_prompt:
            reason = f"already being on {display_version}"
        elif len(local_parts) > 1 and local_parts[1] < ep_min_build:
            reason = f"currently installed build below {ep_branch}.{ep_min_build}"
        else:
            reason = "unknown reason"
        log(f"Not offering Enablement Package due to {reason}")


# ----- Main program -----
def main():
    parser = argparse.ArgumentParser(description="Windows 11 updater using h5ai-hosted files")
    parser.add_argument("--failsafe",action="store_true",help="Bypass self-update, skip mirror tests, and use http://updates.smce.pl for updates")
    parser.add_argument("--base-url", default="https://updates.smce.pl/", help="Base URL of h5ai index")
    parser.add_argument("--user", default="w11updater", help="HTTP Basic Auth username")
    parser.add_argument("--password", default="w11updater", help="HTTP Basic Auth password")
    parser.add_argument("--dry-run",action="store_true",help="Show actions but do not download/install")
    parser.add_argument("--build", "-b",help="Override and install a specific build (e.g. 26100.6899).")
    args = parser.parse_args()

    # --- Add separator in log only ---
    log("=" * 80, console=False)
    
    ## Optional cleanup: silently remove any leftover updater files
    #try:
    #    shutil.rmtree(r"C:\ProgramData\triquetra", ignore_errors=True)
    #except Exception:
    #    pass

    # --- Set window title ---
    ctypes.windll.kernel32.SetConsoleTitleW("Triquetra Updater")

    # --- Show version info ---
    log("Triquetra Updater 1.8.5")

    # Elevation
#    if not is_admin():
#        log("Not elevated. Relaunching elevated to continue...")
#        elevate_and_exit()
    if not is_admin():
        print("ERROR: This program must be run as Administrator.")
        print("Please right-click the executable and select 'Run as administrator'.")
        input("Press Enter to exit...")
        sys.exit(1)
        
    check_not_server_os()

    auth = (args.user, args.password)

    # Self-update
    try:
        if is_frozen() and not args.failsafe:
            self_update("https://updates.smce.pl/triquetra.exe", auth)
        elif args.failsafe:
            log("Failsafe mode: skipping self-update check.")
    except Exception as e:
        log(f"Self-update check failed (continuing): {e}")


    proceed = input("Proceed with checking for updates? [y/N]: ").strip().lower()
    if proceed not in ("y", "yes"):
        log("Update cancelled by user.")
        input("Press Enter to exit...")
        sys.exit(0)

    full_ver = get_ntoskrnl_file_version()
    if not full_ver:
        log("ERROR: could not read ntoskrnl.exe version")
        input("Press Enter to exit...")
        sys.exit(1)

    short_local, local_parts = normalize_local_to_short(full_ver)
    log(f"Local build: {short_local}")

    # --- Mirror selection ---
    if args.failsafe:
        base_url = "http://109.196.126.21:8042/"
        log(f"Failsafe mode: using base URL {base_url}")
    else:
        mirror_candidates = [
            args.base_url.rstrip("/") + "/",     # primary
            "https://updates2.smce.pl/",         # mirror
        ]
        # Automatically choose the fastest mirror
        base_url = choose_fastest_mirror(mirror_candidates, auth)
        
    html = None
    try:
        html = fetch_text(base_url, auth=auth)
        log(f"Using update server: {base_url}")
    except Exception as e:
        log(f"Failed to fetch from chosen mirror {base_url}: {e}")
        input("Press Enter to exit...")
        sys.exit(1)

    # Parse folder list safely
    folders = []
    try:
        folders = parse_h5ai_index_for_folders(html)
    except Exception as e:
        log(f"Failed to parse index page from {base_url}: {e}")
        input("Press Enter to exit...")
        sys.exit(1)

    if not folders:
        log(f"No build-like folders found at {base_url}")
        input("Press Enter to exit...")
        sys.exit(1)


    # Determine local major build
    local_major = local_parts[0]

    # Filter for display only: same branch, but hide special build 26100.1742
    display_folders = [
        f for f in folders
        if int(f.split(".")[0]) == local_major and f != "26100.1742"
    ]

    log(f"Remote candidate builds: {', '.join(display_folders)}")

    # Restrict to same major build
    local_major = local_parts[0]
    same_branch = [f for f in folders if int(f.split(".")[0]) == local_major]
    if not same_branch:
        log(f"No updates found in the same branch as local build {local_major}.")
        input("Press Enter to exit...")
        sys.exit(1)
        
    # --- Filter out incomplete builds ---
    complete_builds = []
    for f in same_branch:
        build_url = urllib.parse.urljoin(base_url, f"{f}/")
        marker_url = urllib.parse.urljoin(build_url, "non_complete")
        if remote_file_exists(marker_url, auth):
            log(f"Skipping build {f}: It is currently being uploaded to the server.")
            continue
        complete_builds.append(f)

    if not complete_builds:
        log("No builds found that have been completely uploaded to the server.")
        input("Press Enter to exit...")
        sys.exit(0)

    same_branch = complete_builds  
    
    # ----- Determine target build -----
    best = None
    best_parts = []

    if args.build:
        override = args.build.strip()
        if override in same_branch:
            best = override
            best_parts = [int(x) for x in best.split(".")]
            log(f"Build override active: forcing installation of {best}")
        elif override in folders:
            log(f"ERROR: Specified build {override} exists but is incomplete or wrong branch.")
            log(f"Available fully uploaded builds in branch: {', '.join(same_branch)}")
            input("Press Enter to exit...")
            sys.exit(1)
        else:
            log(f"ERROR: Specified build {override} not found on server.")
            log(f"Available builds: {', '.join(folders)}")
            input("Press Enter to exit...")
            sys.exit(1)
    else:
        # Custom logic for baseline build if needed (e.g., 26100.1742)
        if local_major == 26100:
            baseline_build = "26100.1742"
            target_major, target_minor = [int(x) for x in baseline_build.split(".")]
            if len(local_parts) > 1 and local_parts[1] < target_minor:
                if baseline_build in same_branch:
                    best = baseline_build
                    best_parts = [target_major, target_minor]
                    log(f"Forcing update to baseline build {baseline_build}")
                else:
                    log(f"ERROR: Required build {baseline_build} not found on server.")
                    input("Press Enter to exit...")
                    sys.exit(1)
        if not best:
            # pick the latest build in same_branch
            best = max(same_branch, key=lambda v: [int(x) for x in v.split(".")])
            best_parts = [int(x) for x in best.split(".")]

    log(f"Selected remote build: {best}")

    # ----- Version comparison -----
    cmpres = compare_version_lists(best_parts, local_parts)
    if cmpres == 0 and not args.build:
        ans = input(f"Local build equals remote build. Reinstall anyway? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            log("Checking for Enablement Package applicability...")
            arch = get_arch_from_registry()
            check_and_offer_enablement_package(local_major, local_parts, auth, arch, args)
            input("Press Enter to exit...")
            sys.exit(0)
        log("User chose to reinstall the same build.")
    elif cmpres < 0 and not args.build:
        log("Local build newer than remote, exiting.")
        input("Press Enter to exit...")
        sys.exit(0)


    # --- Confirm before downloading/installing updates ---
    proceed_download = input(
        f"Do you want to download {best} updates? [y/N]: "
    ).strip().lower()
    if proceed_download not in ("y", "yes"):
        log("Update cancelled before downloading files.")
        input("Press Enter to exit...")
        sys.exit(0)

    # Architecture detection
    arch = get_arch_from_registry()
    log(f"Detected architecture: {arch}")

    # EP URLs
    EP_URLS = {
        "22621": {
            "amd64": "https://updates.smce.pl/EP/amd64/Windows11.0-KB5027397-x64.cab",
            "arm64": "https://updates.smce.pl/EP/arm64/Windows11.0-KB5027397-arm64.cab",
        },
        "26100": {
            "amd64": "https://updates.smce.pl/EP/amd64/Windows11.0-KB5054156-x64.cab",
            "arm64": "https://updates.smce.pl/EP/arm64/Windows11.0-KB5054156-arm64.cab",
        },
    }

    folder_url = urllib.parse.urljoin(base_url, f"{best}/{arch}/")
    log(f"Accesing {folder_url}")

    try:
        folder_html = fetch_text(folder_url, auth=auth)
    except Exception as e:
        log(f"Failed to fetch architecture folder: {e}")
        input("Press Enter to exit...")
        sys.exit(1)

    files = parse_h5ai_files(folder_html)
    if not files:
        log("No files found in architecture folder.")
        input("Press Enter to exit...")
        sys.exit(1)

    cab_candidates = [f for f in files if re.search(r"(?i)\bssu.*\.cab$", f)]
    esd_candidates = [f for f in files if re.search(r"(?i)\b(?:windows|kb).*\.esd$", f)]
    msu_candidates = [f for f in files if re.search(r"(?i)\.msu$", f)]
    ndp_candidates = [
        f for f in files if re.search(r"(?i)NDP.*\.cab$|.*-NDP.*\.cab$", f)
    ]

    selected_cab = cab_candidates[0] if cab_candidates else None
    selected_esd = esd_candidates[0] if esd_candidates else None
    selected_msu = msu_candidates[0] if msu_candidates else None
    selected_ndp = ndp_candidates[0] if ndp_candidates else None

    tmpdir = TMP_DIR
    os.makedirs(tmpdir, exist_ok=True)

    # Install MSU if present
    if selected_msu:
        log(f"MSU detected: {selected_msu}, will install MSU only.")

        # Download MSU first
        msu_path = download_file(
            urllib.parse.urljoin(folder_url, urllib.parse.quote(selected_msu)),
            tmpdir,
            auth,
        )

        # Download NDP next (if present)
        if selected_ndp:
            ndp_path = download_file(
                urllib.parse.urljoin(folder_url, urllib.parse.quote(selected_ndp)),
                tmpdir,
                auth,
            )

        if not args.dry_run:
            confirm_install = input("Do you want to install the updates now? [y/N]: ").strip().lower()
            if confirm_install not in ("y", "yes"):
                log("Installation of downloaded updates cancelled by user.")
                input("Press Enter to exit...")
                sys.exit(0)

            # Install MSU
            rc = powershell_add_package(msu_path)
            if rc != 0:
                log(f"MSU installation failed with code {rc}")
                input("Press Enter to exit...")
                sys.exit(1)

            # Install NDP AFTER MSU
            if selected_ndp:
                rc_ndp = powershell_add_package(ndp_path)
                if rc_ndp != 0:
                    log(f"NDP installation failed with code {rc_ndp}")

    else:
        if not selected_cab or not selected_esd:
            log("Missing CAB or ESD, cannot continue.")
            input("Press Enter to exit...")
            sys.exit(1)

        # Download CAB first
        cab_path = download_file(
            urllib.parse.urljoin(folder_url, urllib.parse.quote(selected_cab)),
            tmpdir,
            auth,
        )

        # Download ESD next
        esd_path = download_file(
            urllib.parse.urljoin(folder_url, urllib.parse.quote(selected_esd)),
            tmpdir,
            auth,
        )

        # Download NPD next
        if selected_ndp:
            ndp_path = download_file(
                urllib.parse.urljoin(folder_url, urllib.parse.quote(selected_ndp)),
                tmpdir,
                auth,
            )

        if not args.dry_run:
            confirm_install = input("Do you want to install the updates now? [y/N]: ").strip().lower()
            if confirm_install not in ("y", "yes"):
                log("Installation cancelled by user.")
                input("Press Enter to exit...")
                sys.exit(0)

            # Install CAB
            rc1 = powershell_add_package(cab_path)
            if rc1 != 0:
                log(f"CAB installation failed with code {rc1}")
                input("Press Enter to exit...")
                sys.exit(1)

            time.sleep(5)
            subprocess.run(
                ["dism", "/Online", "/Get-Packages"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Install ESD
            rc2 = powershell_add_package(esd_path)
            if rc2 != 0:
                log(f"ESD installation failed with code {rc2}")
                input("Press Enter to exit...")
                sys.exit(1)

            # --- NDP installation AFTER ESD ---
            if selected_ndp:
                rc_ndp = powershell_add_package(ndp_path)
                if rc_ndp != 0:
                    log(f"NDP installation failed with code {rc_ndp}")

    # ----- Enablement Package check -----
    arch = get_arch_from_registry()
    check_and_offer_enablement_package(local_major, local_parts, auth, arch, args)

    log("Update finished successfully. A reboot is required.")
    
    clean_ans = input("Do you want to remove downloaded update files? [y/N]: ").strip().lower()
    if clean_ans in ("y", "yes"):
        for item in os.listdir(tmpdir):
            item_path = os.path.join(tmpdir, item)
            # Skip the log file
            if os.path.isfile(item_path) and item.lower() == "triquetra.log":
                continue
            try:
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    log(f"Removed folder: {item_path}")
                else:
                    os.remove(item_path)
                    log(f"Removed file: {item_path}")
            except Exception as e:
                log(f"Failed to remove {item_path}: {e}")
        log("Downloaded files removed.")

    reboot_ans = input("Reboot now? [y/N]: ").strip().lower()
    if reboot_ans in ("y", "yes"):
        log("Rebooting...")
        subprocess.run(["shutdown", "/r", "/t", "5"])
    else:
        log("Reboot postponed.")

    input("Press Enter to exit...")


if __name__ == "__main__":
    if sys.platform != "win32":
        print("This program is intended to run on Windows.")
        input("Press Enter to exit...")
        sys.exit(1)
    main()
