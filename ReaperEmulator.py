#!/usr/bin/env python3
"""
ctem_emu_reaper.py — single-file defanged emulation of the SHub/Reaper macOS
infostealer kill chain, for BAS-delivered detection/control validation.

This is the bundled, dependency-free (stdlib-only) version of the multi-file
harness. It is NOT malware:
  * credential-access phases generate the ACCESS PATTERN only (1-byte-discard
    open / stat); they never read, decrypt, copy, parse, or transmit secrets
  * the "download chain" decodes to an echo, not a curl
  * staging archives planted DECOYS, never real user files
  * NO network I/O (C2 is a marker; pair with a network scenario by run_id)
  * every mutation is rolled back unless --no-rollback is passed
  * every artifact is tagged CTEM-EMU-REAPER + run_id for attribution

Usage:
    python3 ctem_emu_reaper.py                 # full defanged run + rollback
    python3 ctem_emu_reaper.py --dry-run       # log intended actions, no mutation
    python3 ctem_emu_reaper.py --no-rollback   # leave artifacts (BAS owns cleanup)
    python3 ctem_emu_reaper.py --load-agent    # also launchctl-load inert LaunchAgent
    python3 ctem_emu_reaper.py --only T1555.001 [--only ...]   # run subset
    python3 ctem_emu_reaper.py --list          # list technique IDs and exit

Run on a macOS test VM for full fidelity. On non-macOS the chain logic runs but
macOS paths won't exist (opens fall through to a parent stat).
"""
from __future__ import annotations
import argparse
import base64
import os
import platform
import shutil
import subprocess
import uuid
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# framework
# ---------------------------------------------------------------------------
RUN_ID = uuid.uuid4().hex[:12]
EMU_TAG = "CTEM-EMU-REAPER"
HERE = Path(__file__).resolve().parent
ON_MACOS = platform.system() == "Darwin"


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
        print(f"  [signal] {self.technique_id:<12} {self.action} -> {self.target}")
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
    dry_run: bool = False
    load_agent: bool = False
    on_macos: bool = ON_MACOS

    def home(self) -> Path:
        return home()


# ---- guarded primitives ----------------------------------------------------
def touch_for_telemetry(ctx: Context, path: Path) -> bool:
    """Force a kernel open/stat event, then discard. Never retains secret bytes."""
    if ctx.dry_run:
        print(f"  [dry-run] would generate access telemetry on {path}")
        return False
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


def plant_decoy(ctx: Context, dest: Path, content: str) -> Path:
    if ctx.dry_run:
        print(f"  [dry-run] would plant decoy at {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"# {EMU_TAG} decoy artifact — safe to delete\n{content}\n")
    ctx.ledger.push(f"remove decoy {dest}", lambda p=dest: p.unlink(missing_ok=True))
    return dest


def _run(ctx: Context, argv: list[str]) -> None:
    if ctx.dry_run:
        print(f"  [dry-run] would exec: {' '.join(argv)}")
        return
    try:
        subprocess.run(argv, capture_output=True, timeout=15, check=False)
    except FileNotFoundError:
        print(f"  [skip] binary not present on this host: {argv[0]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] exec failed: {exc}")


# ---------------------------------------------------------------------------
# masquerade .app bundle — generated on disk at runtime (can't live in one file)
# ---------------------------------------------------------------------------
_APP_INFO_PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- {EMU_TAG}: emulation artifact masquerading as XProtectRemediator -->
    <key>CFBundleName</key><string>XProtectRemediator</string>
    <key>CFBundleDisplayName</key><string>XProtectRemediator</string>
    <key>CFBundleIdentifier</key><string>com.ctem.emu.reaper.xprotect</string>
    <key>CFBundleExecutable</key><string>run</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.0-emu</string>
    <key>LSUIElement</key><true/>
</dict>
</plist>
"""
_APP_RUN = f"""#!/bin/zsh
# {EMU_TAG} inert app payload. Masquerades as XProtectRemediator (T1036.005).
echo "{EMU_TAG}: XProtectRemediator.app launched (inert)"
exit 0
"""


def build_app_bundle(ctx: Context) -> Path:
    app = ctx.workdir / "XProtectRemediator.app"
    if ctx.dry_run:
        print(f"  [dry-run] would build {app}")
        return app
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_text(_APP_INFO_PLIST)
    runfile = macos / "run"
    runfile.write_text(_APP_RUN)
    runfile.chmod(0o755)
    ctx.ledger.push(f"remove app bundle {app}",
                    lambda p=app: shutil.rmtree(p, ignore_errors=True))
    return app


# ---------------------------------------------------------------------------
# phases
# ---------------------------------------------------------------------------
def p_uri(ctx: Context) -> None:
    inert = f'display notification "{EMU_TAG} run {RUN_ID}" with title "XProtectRemediator"'
    uri = f"applescript://com.apple.ScriptEditor.id?action=new&script={inert}"
    build_app_bundle(ctx)  # materialize the masquerade .app for this run
    if ctx.on_macos:
        _run(ctx, ["open", uri])
    Signal("T1204.002", "User Execution: Malicious File", "execution",
           "invoke applescript:// URI via open(1)", uri[:80] + "...",
           "open(1) spawning Script Editor; applescript URL scheme handler in unified log").emit()


def p_osascript(ctx: Context) -> None:
    if ctx.on_macos:
        _run(ctx, ["osascript", "-e", f'log "{EMU_TAG} osascript exec {RUN_ID}"'])
    Signal("T1059.002", "Command and Scripting Interpreter: AppleScript", "execution",
           "osascript -e <inert banner>", "/usr/bin/osascript",
           "osascript exec with -e; parent = Script Editor/open; ES_EVENT_TYPE_NOTIFY_EXEC").emit()


def p_deobfuscate(ctx: Context) -> None:
    inert_cmd = f'echo "{EMU_TAG}: deobfuscation marker {RUN_ID} (no fetch performed)"'
    encoded = base64.b64encode(inert_cmd.encode()).decode()
    decoded = base64.b64decode(encoded).decode()
    if ctx.on_macos:
        _run(ctx, ["/bin/zsh", "-c", decoded])
    Signal("T1140", "Deobfuscate/Decode Files or Information", "execution",
           "base64 -d of inert payload (curl chain replaced with echo)", "in-memory string",
           "scripted base64 decode then zsh exec; decoded content benign by design",
           note="No curl/network. Decoded payload is an echo marker.").emit()


def p_zsh(ctx: Context) -> None:
    if ctx.on_macos:
        _run(ctx, ["/bin/zsh", "-c", f'print -- "{EMU_TAG} zsh exec {RUN_ID}"'])
    Signal("T1059.004", "Command and Scripting Interpreter: Unix Shell", "execution",
           "/bin/zsh -c <inert>", "/bin/zsh",
           "zsh child of osascript/Script Editor; suspicious interpreter chain").emit()


def p_keyboard(ctx: Context) -> None:
    plist = ctx.home() / "Library/Preferences/com.apple.HIToolbox.plist"
    touch_for_telemetry(ctx, plist)
    Signal("T1497.001", "Virtualization/Sandbox Evasion: System Checks", "discovery",
           "read HIToolbox.plist (keyboard-layout heuristic)", str(plist),
           "process reading com.apple.HIToolbox.plist; defaults read AppleEnabledInputSources",
           note="Real malware aborts on Russian layout; harness always continues.").emit()


def p_vm(ctx: Context) -> None:
    info = ""
    if ctx.on_macos and not ctx.dry_run:
        try:
            info = "hw.model=" + subprocess.run(
                ["sysctl", "-n", "hw.model"], capture_output=True, text=True,
                timeout=10, check=False).stdout.strip()
        except Exception:  # noqa: BLE001
            pass
    else:
        info = f"platform={platform.platform()}"
    Signal("T1497.001", "Virtualization/Sandbox Evasion: System Checks", "discovery",
           "enumerate hw.model / VM indicators (read-only)", "sysctl hw.model; ioreg",
           "sysctl/ioreg queries for VM strings shortly after suspicious exec",
           note=info or "no indicators read").emit()


def _walk(ctx: Context, tid: str, tname: str, label: str, paths: list[Path], tel: str) -> None:
    hit = any(touch_for_telemetry(ctx, p) for p in paths)
    Signal(tid, tname, "credential-access",
           f"path-walk + read-handle (1-byte discard) on {label} stores",
           "; ".join(str(p) for p in paths[:3]) + (" ..." if len(paths) > 3 else ""),
           tel, note=f"defanged: open/stat only, no decrypt/parse/copy. live_paths_present={hit}").emit()


def p_keychain(ctx: Context) -> None:
    h = ctx.home()
    _walk(ctx, "T1555.001", "Credentials from Password Stores: Keychain", "Keychain",
          [h / "Library/Keychains/login.keychain-db", Path("/Library/Keychains/System.keychain")],
          "non-Keychain-Access process opening login.keychain-db; ES open on *.keychain-db "
          "(no security(1)/SecKeychain by design)")


def p_browser(ctx: Context) -> None:
    asp = ctx.home() / "Library/Application Support"
    _walk(ctx, "T1555.003", "Credentials from Web Browsers", "browser credential",
          [asp / "Google/Chrome/Default/Login Data", asp / "Google/Chrome/Default/Web Data",
           asp / "Google/Chrome/Local State", ctx.home() / "Library/Application Support/Firefox/Profiles"],
          "non-browser process opening Chrome 'Login Data'/'Local State' or Firefox profiles; "
          "open-and-discard, never queries the DB")


def p_wallets(ctx: Context) -> None:
    asp = ctx.home() / "Library/Application Support"
    _walk(ctx, "T1005", "Data from Local System", "crypto wallet",
          [asp / "Exodus", asp / "atomic", asp / "Ledger Live", asp / "electrum",
           asp / "Google/Chrome/Default/Local Extension Settings/nkbihfbeogaeaoehlefnkodbefgpgknn"],
          "enumeration of Exodus/Atomic/Ledger/Electrum dirs and MetaMask extension storage")


def p_telegram(ctx: Context) -> None:
    h = ctx.home()
    _walk(ctx, "T1005", "Data from Local System", "Telegram session",
          [h / "Library/Application Support/Telegram Desktop/tdata",
           h / "Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram"],
          "access to Telegram Desktop 'tdata' or the Telegram group container by a non-Telegram process")


def p_staging(ctx: Context) -> None:
    decoy_dir = ctx.workdir / "decoys"
    if not ctx.dry_run:
        decoy_dir.mkdir(parents=True, exist_ok=True)
    files = [plant_decoy(ctx, decoy_dir / n, f"synthetic decoy for {n} — no real data")
             for n in ("notes.txt", "passwords.txt", "wallet.seed")]
    stage_dir = Path("/tmp") / f".{EMU_TAG.lower()}_{RUN_ID}"
    zip_path = stage_dir / "stage.zip"
    if not ctx.dry_run:
        stage_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                if f.exists():
                    zf.write(f, arcname=f.name)
        ctx.ledger.push(f"remove staging dir {stage_dir}",
                        lambda d=stage_dir: shutil.rmtree(d, ignore_errors=True))
    Signal("T1560.001", "Archive Collected Data: Archive via Utility", "collection",
           "create hidden /tmp/.<dir>/stage.zip from DECOY files", str(zip_path),
           "hidden dir + .zip under /tmp; file-create burst (real malware chunks ~70MB, caps ~150MB)",
           note="archive contains only decoys; real user data untouched; rolled back").emit()


_PLIST_NAME = "com.google.keystone.agent.plist"
_PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.google.keystone.agent</string>
    <!-- {EMU_TAG}: emulation artifact, inert payload, run {RUN_ID} -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string><string>-c</string>
        <string>echo "{EMU_TAG} launchagent fired {RUN_ID}"</string>
    </array>
    <key>RunAtLoad</key><true/>
</dict>
</plist>
"""


def p_persistence(ctx: Context) -> None:
    agents = ctx.home() / "Library/LaunchAgents"
    plist = agents / _PLIST_NAME
    if not ctx.dry_run:
        agents.mkdir(parents=True, exist_ok=True)
        if plist.exists():
            backup = plist.with_suffix(".plist.emu-backup")
            backup.write_bytes(plist.read_bytes())
            ctx.ledger.push(f"restore original {plist}",
                            lambda p=plist, b=backup: (p.write_bytes(b.read_bytes()), b.unlink(missing_ok=True)))
        else:
            ctx.ledger.push(f"remove planted {plist}", lambda p=plist: p.unlink(missing_ok=True))
        plist.write_text(_PLIST)
        if ctx.load_agent and ctx.on_macos:
            subprocess.run(["launchctl", "load", str(plist)], capture_output=True, check=False)
            ctx.ledger.push(f"launchctl unload {plist}",
                            lambda p=plist: subprocess.run(["launchctl", "unload", str(p)],
                                                           capture_output=True, check=False))
    Signal("T1543.001", "Create or Modify System Process: Launch Agent", "persistence",
           f"drop {_PLIST_NAME} (inert payload){' + launchctl load' if ctx.load_agent else ''}", str(plist),
           "file-create of ~/Library/LaunchAgents/com.google.keystone.agent.plist by unexpected process",
           note="masquerades as Google Keystone (T1036.005); payload is an echo marker").emit()


def p_c2_marker(ctx: Context) -> None:
    Signal("T1071.001", "Application Layer Protocol: Web Protocols (MARKER ONLY)", "c2-marker",
           "log beacon intent; NO socket opened", "hebsbsbzjsjshduxbs[.]xyz",
           "correlate with a separate network scenario: HTTPS beacon ~60s, encrypted. "
           "This harness performs no network I/O.",
           note="defanged domain; out-of-scope; pair with network atomic").emit()


# (label, technique_id, callable) — order = kill chain
PLAN: list[tuple[str, str, Callable[[Context], None]]] = [
    ("execution", "T1204.002", p_uri),
    ("execution", "T1059.002", p_osascript),
    ("execution", "T1140", p_deobfuscate),
    ("execution", "T1059.004", p_zsh),
    ("discovery", "T1497.001a", p_keyboard),
    ("discovery", "T1497.001b", p_vm),
    ("credential-access", "T1555.001", p_keychain),
    ("credential-access", "T1555.003", p_browser),
    ("credential-access", "T1005a", p_wallets),
    ("credential-access", "T1005b", p_telegram),
    ("collection", "T1560.001", p_staging),
    ("persistence", "T1543.001", p_persistence),
    ("c2-marker", "T1071.001", p_c2_marker),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Reaper/SHub macOS emulation (defanged, single-file)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-rollback", action="store_true")
    ap.add_argument("--load-agent", action="store_true")
    ap.add_argument("--only", action="append", default=[],
                    help="run only matching technique id(s); repeatable")
    ap.add_argument("--list", action="store_true", help="list technique ids and exit")
    args = ap.parse_args()

    if args.list:
        for label, tid, _ in PLAN:
            print(f"{tid:<14} {label}")
        return 0

    workdir = Path("/tmp") / f"{EMU_TAG.lower()}_work_{RUN_ID}"
    workdir.mkdir(parents=True, exist_ok=True)
    ledger = RollbackLedger()
    if not args.dry_run and not args.no_rollback:
        ledger.push(f"remove workdir {workdir}", lambda d=workdir: shutil.rmtree(d, ignore_errors=True))
    ctx = Context(ledger=ledger, workdir=workdir, dry_run=args.dry_run, load_agent=args.load_agent)

    selected = [(l, t, f) for (l, t, f) in PLAN
                if not args.only or any(o in t for o in args.only)]

    print(f"=== {EMU_TAG} run {RUN_ID} ===")
    print(f"  platform: {'macOS' if ON_MACOS else 'non-macOS (reduced fidelity)'}")
    print(f"  mode: {'DRY-RUN' if args.dry_run else 'LIVE (defanged)'}  "
          f"rollback: {'OFF' if args.no_rollback else 'ON'}  phases: {len(selected)}\n")

    try:
        for label, tid, fn in selected:
            print(f"[phase:{label}] {tid}")
            fn(ctx)
            print()
    finally:
        if args.no_rollback:
            print(f"\n=== rollback skipped (--no-rollback); {len(ledger)} pending undo ops ===")
            print(f"    clean manually: {workdir}")
        else:
            ledger.rollback()

    n = len(selected)
    print(f"\nDone. {n} signals emitted to stdout.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
