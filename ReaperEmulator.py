#!/usr/bin/env python3
"""
ctem_emu_reaper.py — single-file DEFANGED emulation of the SHub/Reaper macOS
infostealer, IoC-aligned to the SentinelOne writeup (Stokes, May 2026).

Runs the full kill chain and rolls everything back. No options:

    python3 ctem_emu_reaper.py

THIS IS NOT MALWARE:
  * credential-access generates the ACCESS PATTERN only (1-byte-discard open/
    stat); never reads, decrypts, copies, parses, or transmits real secrets
  * Filegrabber ENUMERATES the real Desktop/Documents tree (listing only) and
    archives ONLY harness-planted decoys — real user files are never opened
  * wallet "injection" runs xattr/codesign against a DECOY .app; it never
    touches a real wallet, downloads nothing, and kills no process
  * the backdoor binary + /tmp/.c.sh are inert echo stubs; NO network, NO
    download-and-execute
  * NO network I/O anywhere (C2 is a marker; pair with a network scenario)
  * artifacts use the real IoC paths/names so your FIM/behavioral rules fire,
    but contents are inert + EMU-tagged, any pre-existing real file is backed
    up, and everything is rolled back at the end of the run

Run on a macOS test VM for full fidelity. On non-macOS the chain logic runs but
macOS paths won't exist (opens fall through to a parent stat).
"""
from __future__ import annotations
import base64
import os
import platform
import shutil
import subprocess
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# framework
# ---------------------------------------------------------------------------
RUN_ID = uuid.uuid4().hex[:12]
EMU_TAG = "CTEM-EMU-REAPER"
ON_MACOS = platform.system() == "Darwin"
C2_DEFANGED = "hebsbsbzjsjshduxbs[.]xyz"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def home() -> Path:
    return Path(os.path.expanduser("~"))


@dataclass
class Signal:
    technique_id: str
    technique_name: str
    phase: str
    action: str
    target: str
    expected_telemetry: str
    run_id: str = RUN_ID
    ts: str = field(default_factory=utcnow)
    host: str = field(default_factory=platform.node)
    note: str = ""

    def emit(self) -> "Signal":
        print(f"  [signal] {self.technique_id:<16} {self.action} -> {self.target}")
        return self


class RollbackLedger:
    def __init__(self) -> None:
        self._ops: list[tuple[str, Callable[[], None]]] = []

    def push(self, description: str, undo: Callable[[], None]) -> None:
        self._ops.append((description, undo))

    def rollback(self) -> None:
        print("\n=== ROLLBACK (LIFO) ===")
        while self._ops:
            desc, undo = self._ops.pop()
            try:
                undo()
                print(f"  [restored] {desc}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [WARN] rollback failed for '{desc}': {exc}")

    def __len__(self) -> int:
        return len(self._ops)


@dataclass
class Context:
    ledger: RollbackLedger
    workdir: Path
    on_macos: bool = ON_MACOS

    def home(self) -> Path:
        return home()


# ---- guarded primitives ----------------------------------------------------
def touch_for_telemetry(ctx: Context, path: Path) -> bool:
    """Force a kernel open/stat event then discard. Never retains secret bytes."""
    try:
        if path.exists():
            with path.open("rb") as fh:
                fh.read(1)  # force open; discard. Never the full secret.
            return True
        path.parent.stat()
        return False
    except PermissionError:
        try:
            path.stat()
        except Exception:  # noqa: BLE001
            pass
        return False
    except Exception:  # noqa: BLE001
        return False


def enumerate_only(root: Path, exts: list[str]) -> int:
    """List (NOT open) files under root matching exts. Returns match count.

    Filegrabber discovery signal — directory traversal of the real Desktop/
    Documents tree. Reads names/metadata only; never opens a real file's content.
    """
    if not root.exists():
        return 0
    count = 0
    try:
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in exts:
                count += 1
                if count >= 5000:
                    break
    except Exception:  # noqa: BLE001
        pass
    return count


def plant_decoy(ctx: Context, dest: Path, content: str) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"# {EMU_TAG} decoy artifact — safe to delete\n{content}\n")
    ctx.ledger.push(f"remove decoy {dest}", lambda p=dest: p.unlink(missing_ok=True))
    return dest


def _run(argv: list[str]) -> None:
    try:
        subprocess.run(argv, capture_output=True, timeout=20, check=False)
    except FileNotFoundError:
        print(f"  [skip] binary not present on this host: {argv[0]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] exec failed: {exc}")


def _backup_or_remove(ctx: Context, path: Path) -> None:
    """Register rollback: restore a pre-existing real file, else remove ours."""
    if path.exists():
        backup = path.with_name(path.name + ".emu-backup")
        backup.write_bytes(path.read_bytes())
        ctx.ledger.push(
            f"restore original {path}",
            lambda p=path, b=backup: (p.write_bytes(b.read_bytes()), b.unlink(missing_ok=True)),
        )
    else:
        ctx.ledger.push(f"remove planted {path}", lambda p=path: p.unlink(missing_ok=True))


# ---------------------------------------------------------------------------
# IoC path constants (from the SentinelOne writeup)
# ---------------------------------------------------------------------------
GOOGLE_DIR = "Library/Application Support/Google/GoogleUpdate.app/Contents/MacOS"
GOOGLE_BIN = "GoogleUpdate"
PLIST_NAME = "com.google.keystone.agent.plist"
PLIST_LABEL = "com.google.keystone.agent"
STAGE_ZIP = Path("/tmp/shub_log.zip")
SPLIT_SH = Path("/tmp/shub_split.sh")
BACKDOOR_SH = Path("/tmp/.c.sh")


def _stage_root() -> Path:
    # random segment carries run_id so it stays attributable while matching shub_*
    return Path("/tmp") / f"shub_emu{RUN_ID}"


FILEGRABBER_EXTS = [".docx", ".doc", ".wallet", ".key", ".keys", ".txt",
                    ".rtf", ".csv", ".xls", ".xlsx", ".json", ".rdp"]
FILEGRABBER_IMG = [".png"]

BROWSERS = {
    "Chrome": "Google/Chrome/Default/Login Data",
    "Brave": "BraveSoftware/Brave-Browser/Default/Login Data",
    "Edge": "Microsoft Edge/Default/Login Data",
    "Opera": "com.operasoftware.Opera/Login Data",
    "Vivaldi": "Vivaldi/Default/Login Data",
    "Arc": "Arc/User Data/Default/Login Data",
}
DESKTOP_WALLETS = ["Exodus", "atomic", "Ledger Live", "electrum", "Trezor Suite"]


# ---------------------------------------------------------------------------
# inert artifact templates
# ---------------------------------------------------------------------------
_APP_INFO_PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- {EMU_TAG}: emulation artifact masquerading as XProtectRemediator -->
    <key>CFBundleName</key><string>XProtectRemediator</string>
    <key>CFBundleIdentifier</key><string>com.ctem.emu.reaper.xprotect</string>
    <key>CFBundleExecutable</key><string>run</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSUIElement</key><true/>
</dict>
</plist>
"""
_APP_RUN = (f'#!/bin/zsh\n# {EMU_TAG} inert app payload (T1036.005)\n'
            f'echo "{EMU_TAG}: XProtectRemediator.app launched (inert)"\nexit 0\n')


def build_app_bundle(ctx: Context) -> Path:
    app = ctx.workdir / "XProtectRemediator.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_text(_APP_INFO_PLIST)
    rf = macos / "run"
    rf.write_text(_APP_RUN)
    rf.chmod(0o755)
    ctx.ledger.push(f"remove app bundle {app}", lambda p=app: shutil.rmtree(p, ignore_errors=True))
    return app


# ---------------------------------------------------------------------------
# phases — execution
# ---------------------------------------------------------------------------
def p_uri(ctx: Context) -> None:
    inert = f'display notification "{EMU_TAG} {RUN_ID}" with title "XProtectRemediator"'
    uri = f"applescript://com.apple.scripteditor?action=new&script={inert}"
    build_app_bundle(ctx)
    if ctx.on_macos:
        _run(["open", uri])
    Signal("T1204.002", "User Execution: Malicious File", "execution",
           "invoke applescript:// URI via open(1)", uri[:80] + "...",
           "open(1) spawning Script Editor; applescript URL scheme handler in unified log").emit()


def p_osascript(ctx: Context) -> None:
    if ctx.on_macos:
        _run(["osascript", "-e", f'log "{EMU_TAG} osascript exec {RUN_ID}"'])
    Signal("T1059.002", "Command and Scripting Interpreter: AppleScript", "execution",
           "osascript -e <inert banner>", "/usr/bin/osascript",
           "osascript exec; parent = Script Editor/open; runs without touching disk").emit()


def p_deobfuscate(ctx: Context) -> None:
    inert_cmd = f'echo "{EMU_TAG}: deobfuscation marker {RUN_ID} (no fetch)"'
    encoded = base64.b64encode(inert_cmd.encode()).decode()
    decoded = base64.b64decode(encoded).decode()
    if ctx.on_macos:
        _run(["/bin/zsh", "-c", decoded])
    Signal("T1140", "Deobfuscate/Decode Files or Information", "execution",
           "base64 -d of inert payload (curl|zsh chain replaced with echo)", "in-memory string",
           "scripted base64 decode then zsh exec; Singularity 'Data decoded with Base64'",
           note="No curl/network. Decoded payload is an echo marker.").emit()


def p_zsh(ctx: Context) -> None:
    if ctx.on_macos:
        _run(["/bin/zsh", "-c", f'print -- "{EMU_TAG} zsh exec {RUN_ID}"'])
    Signal("T1059.004", "Command and Scripting Interpreter: Unix Shell", "execution",
           "/bin/zsh -c <inert>", "/bin/zsh",
           "zsh/sh child of osascript/Script Editor; suspicious interpreter chain").emit()


# ---------------------------------------------------------------------------
# phases — discovery / anti-analysis
# ---------------------------------------------------------------------------
def p_locale(ctx: Context) -> None:
    plist = ctx.home() / "Library/Preferences/com.apple.HIToolbox.plist"
    if ctx.on_macos:
        _run(["defaults", "read", str(plist), "AppleEnabledInputSources"])
    else:
        touch_for_telemetry(ctx, plist)
    Signal("T1614.001", "System Location Discovery: System Language Discovery", "discovery",
           "defaults read HIToolbox.plist AppleEnabledInputSources (locale/CIS check)", str(plist),
           "defaults read of com.apple.HIToolbox AppleEnabledInputSources; Singularity "
           "'system language enumeration through keyboard input gathering'",
           note="Real malware sends cis_blocked + exits on Russian layout; harness continues.").emit()


def p_vm(ctx: Context) -> None:
    info = ""
    if ctx.on_macos:
        try:
            info = "hw.model=" + subprocess.run(["sysctl", "-n", "hw.model"],
                   capture_output=True, text=True, timeout=10, check=False).stdout.strip()
        except Exception:  # noqa: BLE001
            pass
    else:
        info = f"platform={platform.platform()}"
    Signal("T1497.001", "Virtualization/Sandbox Evasion: System Checks", "discovery",
           "enumerate hw.model / VM indicators (read-only)", "sysctl hw.model; ioreg",
           "sysctl/ioreg VM-string queries after suspicious exec",
           note=info or "no indicators read").emit()


# ---------------------------------------------------------------------------
# phases — credential access (DEFANGED: access pattern only)
# ---------------------------------------------------------------------------
def _walk(ctx: Context, tid: str, tname: str, label: str, paths: list[Path], tel: str) -> None:
    hit = any(touch_for_telemetry(ctx, p) for p in paths)
    Signal(tid, tname, "credential-access",
           f"path-walk + read-handle (1-byte discard) on {label}",
           "; ".join(str(p) for p in paths[:3]) + (" ..." if len(paths) > 3 else ""),
           tel, note=f"defanged: open/stat only, no decrypt/parse/copy. live_paths_present={hit}").emit()


def p_keychain(ctx: Context) -> None:
    h = ctx.home()
    _walk(ctx, "T1555.001", "Credentials from Password Stores: Keychain", "Keychain",
          [h / "Library/Keychains/login.keychain-db", Path("/Library/Keychains/System.keychain")],
          "non-Keychain-Access process opening login.keychain-db; Singularity 'keychain "
          "credential collection [KEYCHAIN]' (no security(1)/SecKeychain here by design)")


def p_browser(ctx: Context) -> None:
    asp = ctx.home() / "Library/Application Support"
    paths = [asp / rel for rel in BROWSERS.values()]
    paths.append(ctx.home() / "Library/Application Support/Firefox/Profiles")
    _walk(ctx, "T1555.003", "Credentials from Web Browsers", "browser credential stores",
          paths,
          "non-browser process opening Login Data across Chrome/Brave/Edge/Opera/Vivaldi/Arc "
          "and Firefox profiles; open-and-discard, never queries the DB")


def p_wallets(ctx: Context) -> None:
    asp = ctx.home() / "Library/Application Support"
    paths = [asp / w for w in DESKTOP_WALLETS]
    paths.append(asp / "Google/Chrome/Default/Local Extension Settings/nkbihfbeogaeaoehlefnkodbefgpgknn")
    _walk(ctx, "T1005", "Data from Local System", "crypto wallet dirs",
          paths,
          "enumeration of Exodus/Atomic/Ledger Live/Electrum/Trezor Suite + MetaMask extension storage")


def p_telegram(ctx: Context) -> None:
    h = ctx.home()
    _walk(ctx, "T1005", "Data from Local System", "Telegram session",
          [h / "Library/Application Support/Telegram Desktop/tdata",
           h / "Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram"],
          "access to Telegram Desktop 'tdata' / Telegram group container by a non-Telegram process")


# ---------------------------------------------------------------------------
# phases — collection (Filegrabber) + staging
# ---------------------------------------------------------------------------
def p_filegrabber(ctx: Context) -> None:
    """T1083 + T1005 — enumerate Desktop/Documents (real, listing only) and read
    ONLY planted decoys. Real user files are never opened."""
    h = ctx.home()
    found = 0
    for root in (h / "Desktop", h / "Documents"):
        found += enumerate_only(root, FILEGRABBER_EXTS + FILEGRABBER_IMG)

    decoys = []
    for root, name in ((h / "Desktop", f"{EMU_TAG}_decoy_{RUN_ID}.txt"),
                       (h / "Documents", f"{EMU_TAG}_decoy_{RUN_ID}.docx")):
        decoys.append(plant_decoy(ctx, root / name, "synthetic decoy — no real data"))
    for d in decoys:
        touch_for_telemetry(ctx, d)  # read our own decoy, never a real file

    Signal("T1083", "File and Directory Discovery", "collection",
           "rglob Desktop+Documents for Filegrabber extensions (listing only)",
           f"{h}/Desktop, {h}/Documents",
           f"directory traversal of Desktop/Documents filtering on {','.join(FILEGRABBER_EXTS)} "
           f"(<2MB) and .png (<6MB); real-file metadata reads, contents untouched",
           note=f"matched_real_files={found}; only planted decoys were opened").emit()


def p_staging(ctx: Context) -> None:
    """T1074.001 + T1560.001 — stage decoys into shub_<rand>/FileGrabber, zip to
    shub_log.zip, write shub_split.sh, emit shub_mzip_* chunks. Decoys only."""
    stage = _stage_root()
    fg = stage / "FileGrabber"
    fg.mkdir(parents=True, exist_ok=True)
    ctx.ledger.push(f"remove staging dir {stage}", lambda d=stage: shutil.rmtree(d, ignore_errors=True))
    for n in ("notes.txt", "wallet.key", "report.docx"):
        (fg / n).write_text(f"{EMU_TAG} decoy {n}\n")
    _backup_or_remove(ctx, STAGE_ZIP)
    with zipfile.ZipFile(STAGE_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in fg.iterdir():
            zf.write(f, arcname=f.name)
    _backup_or_remove(ctx, SPLIT_SH)
    SPLIT_SH.write_text(f"#!/bin/bash\n# {EMU_TAG} inert split stub (real: 70MB chunks @ >85MB)\n"
                        f"echo '{EMU_TAG} split marker {RUN_ID}'\n")
    SPLIT_SH.chmod(0o755)
    for i in range(2):
        chunk = Path(f"/tmp/shub_mzip_{i:03d}.zip")
        _backup_or_remove(ctx, chunk)
        with zipfile.ZipFile(chunk, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("part", f"{EMU_TAG} chunk {i}")

    Signal("T1560.001", "Archive Collected Data: Archive via Utility", "collection",
           "stage decoys -> shub_log.zip; write shub_split.sh; emit shub_mzip_*.zip",
           f"{STAGE_ZIP}; {SPLIT_SH}; /tmp/shub_mzip_*.zip",
           "hidden /tmp/shub_* dir + .zip creation; split-script write; chunk-file burst "
           "(real malware: split at 85MB into 70MB chunks, 150MB cap)",
           note="archive holds only decoys; real data untouched; rolled back").emit()


def p_timestomp(ctx: Context) -> None:
    """T1070.006 — Timestomp: reset a staged file's timestamps."""
    target = STAGE_ZIP if STAGE_ZIP.exists() else _stage_root()
    if ctx.on_macos and target.exists():
        _run(["touch", "-t", "202001010000", str(target)])
    elif target.exists():
        old = time.mktime((2020, 1, 1, 0, 0, 0, 0, 0, 0))
        try:
            os.utime(target, (old, old))
        except Exception:  # noqa: BLE001
            pass
    Signal("T1070.006", "Indicator Removal: Timestomp", "defense-evasion",
           "reset timestamps on staged archive", str(target),
           "file timestamp set to a past date; Singularity 'File timestamp was reset [TIMESTOMP]'").emit()


# ---------------------------------------------------------------------------
# phases — wallet app.asar hijack (DEFANGED against a decoy bundle)
# ---------------------------------------------------------------------------
def p_wallet_inject(ctx: Context) -> None:
    """T1554 — Compromise Host Software Binary (app.asar swap), DEFANGED.

    Builds a DECOY wallet .app in the workdir and performs the swap + Gatekeeper
    bypass + ad-hoc codesign against THAT. Real /Applications wallets are only
    stat-ed (discovery); never modified, never pkill'd; nothing fetched.
    """
    for w in ("Exodus", "Atomic Wallet", "Ledger Live", "Trezor Suite"):
        touch_for_telemetry(ctx, Path("/Applications") / f"{w}.app" / "Contents/Info.plist")

    decoy_app = ctx.workdir / "DecoyWallet.app"
    res = decoy_app / "Contents" / "Resources"
    res.mkdir(parents=True, exist_ok=True)
    (res / "app.asar").write_text(f"{EMU_TAG} original asar")
    ctx.ledger.push(f"remove decoy wallet {decoy_app}",
                    lambda p=decoy_app: shutil.rmtree(p, ignore_errors=True))
    payload = Path("/tmp/exodus_asar.zip")
    _backup_or_remove(ctx, payload)
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("app.asar", f"{EMU_TAG} replacement asar (inert)")
    (res / "app.asar").write_text(f"{EMU_TAG} replacement asar (inert)")
    if ctx.on_macos:
        _run(["xattr", "-cr", str(decoy_app)])
        _run(["codesign", "-f", "-s", "-", str(decoy_app)])

    Signal("T1554", "Compromise Host Software Binary", "impact",
           "swap app.asar on DECOY wallet bundle; xattr -cr; ad-hoc codesign; /tmp/*_asar.zip",
           "DecoyWallet.app; /tmp/exodus_asar.zip",
           "app.asar write inside an .app bundle; xattr -cr (quarantine clear, T1553.001); "
           "codesign -f -s - (ad-hoc); *_asar.zip in /tmp",
           note="decoy bundle only; no real wallet touched; no pkill; no download").emit()


# ----------------------------------
# phases — persistence + backdoor
# ---------------------------------------------------------------------------
def _plist_body(prog_path: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{PLIST_LABEL}</string>
    <!-- {EMU_TAG}: emulation artifact, inert payload, run {RUN_ID} -->
    <key>ProgramArguments</key>
    <array><string>{prog_path}</string></array>
    <key>StartInterval</key><integer>60</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/dev/null</string>
    <key>StandardErrorPath</key><string>/dev/null</string>
</dict>
</plist>
"""


def p_persistence(ctx: Context) -> None:
    """T1543.001 + T1036.005 — GoogleUpdate.app masquerade dir, inert GoogleUpdate
    binary, and com.google.keystone.agent.plist (StartInterval 60). The plist is
    written to disk but NOT launchctl-loaded; the on-disk drop is the detection."""
    h = ctx.home()
    gdir = h / GOOGLE_DIR
    gbin = gdir / GOOGLE_BIN
    plist = h / "Library/LaunchAgents" / PLIST_NAME

    gdir.mkdir(parents=True, exist_ok=True)
    google_root = h / "Library/Application Support/Google/GoogleUpdate.app"
    ctx.ledger.push(f"remove masquerade dir {google_root}",
                    lambda p=google_root: shutil.rmtree(p, ignore_errors=True))
    gbin.write_text(
        f"#!/bin/bash\n# {EMU_TAG} inert backdoor stub (real: beacons C2 /api/bot/heartbeat\n"
        f"# every 60s, base64-decodes 'code' to /tmp/.c.sh, executes, deletes).\n"
        f"# This stub does NONE of that.\necho \"{EMU_TAG} GoogleUpdate beacon {RUN_ID} (inert)\"\n")
    gbin.chmod(0o755)
    plist.parent.mkdir(parents=True, exist_ok=True)
    _backup_or_remove(ctx, plist)
    plist.write_text(_plist_body(str(gbin)))

    Signal("T1543.001", "Create or Modify System Process: Launch Agent", "persistence",
           f"create GoogleUpdate.app/.../GoogleUpdate + {PLIST_NAME} (StartInterval 60)",
           f"~/{GOOGLE_DIR}/{GOOGLE_BIN}; ~/Library/LaunchAgents/{PLIST_NAME}",
           "file-create of com.google.keystone.agent.plist + a GoogleUpdate binary under a fake "
           "Google Software Update path (T1036.005)",
           note="masquerades as Google Keystone; backdoor stub is inert echo; not loaded").emit()


def p_backdoor_file(ctx: Context) -> None:
    """Backdoor exec stub — write /tmp/.c.sh, chmod, run (inert), delete.
    Mirrors the malware's write-exec-delete at the watched IoC path, but the
    script only echoes; nothing is fetched or executed remotely."""
    _backup_or_remove(ctx, BACKDOOR_SH)
    BACKDOOR_SH.write_text(f"#!/bin/bash\necho \"{EMU_TAG} .c.sh exec {RUN_ID} (inert)\"\n")
    BACKDOOR_SH.chmod(0o755)
    if ctx.on_macos:
        _run(["/bin/bash", str(BACKDOOR_SH)])
    try:
        BACKDOOR_SH.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    Signal("T1059.004", "Command and Scripting Interpreter: Unix Shell (backdoor stub)",
           "backdoor", "write+chmod+exec+delete /tmp/.c.sh (inert)", str(BACKDOOR_SH),
           "create of hidden /tmp/.c.sh, chmod +x (T1222.002), exec, immediate delete",
           note="real malware writes server-returned code here; stub only echoes").emit()


def p_c2_marker(ctx: Context) -> None:
    Signal("T1071.001", "Application Layer Protocol: Web Protocols (MARKER ONLY)", "c2-marker",
           "log beacon intent; NO socket opened", C2_DEFANGED,
           "correlate with a network scenario: HTTPS beacon to /api/bot/heartbeat ~60s, "
           "chunk upload to /gate/chunk, telemetry to /api/debug/event. No network I/O here.",
           note="defanged domain; out-of-scope; pair with network atomic").emit()


# (label, technique_id, callable) — order = kill chain
PLAN: list[tuple[str, str, Callable[[Context], None]]] = [
    ("execution", "T1204.002", p_uri),
    ("execution", "T1059.002", p_osascript),
    ("execution", "T1140", p_deobfuscate),
    ("execution", "T1059.004", p_zsh),
    ("discovery", "T1614.001", p_locale),
    ("discovery", "T1497.001", p_vm),
    ("credential-access", "T1555.001", p_keychain),
    ("credential-access", "T1555.003", p_browser),
    ("credential-access", "T1005.wallets", p_wallets),
    ("credential-access", "T1005.telegram", p_telegram),
    ("collection", "T1083", p_filegrabber),
    ("collection", "T1560.001", p_staging),
    ("defense-evasion", "T1070.006", p_timestomp),
    ("impact", "T1554", p_wallet_inject),
    ("persistence", "T1543.001", p_persistence),
    ("backdoor", "T1059.004.bd", p_backdoor_file),
    ("c2-marker", "T1071.001", p_c2_marker),
]


def main() -> int:
    workdir = Path("/tmp") / f"{EMU_TAG.lower()}_work_{RUN_ID}"
    workdir.mkdir(parents=True, exist_ok=True)
    ledger = RollbackLedger()
    ledger.push(f"remove workdir {workdir}", lambda d=workdir: shutil.rmtree(d, ignore_errors=True))
    ctx = Context(ledger=ledger, workdir=workdir)

    print(f"=== {EMU_TAG} run {RUN_ID} ===")
    print(f"  platform: {'macOS' if ON_MACOS else 'non-macOS (reduced fidelity)'}")
    print(f"  mode: LIVE (defanged), rollback ON, {len(PLAN)} phases\n")

    try:
        for label, tid, fn in PLAN:
            print(f"[phase:{label}] {tid}")
            fn(ctx)
            print()
    finally:
        ledger.rollback()

    print(f"\nDone. {len(PLAN)} signals emitted to stdout.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
