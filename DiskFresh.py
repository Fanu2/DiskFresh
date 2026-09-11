# ============================================================================
#  DiskFresh — a safe, modern disk cleaner for Windows
#  ---------------------------------------------------------------------------
#  Requirements : Python 3.9+  |  PySide6        (pip install PySide6)
#  Run          : python diskfresh.py   (ideally "Run as administrator")
# ----------------------------------------------------------------------------
#  Safety model:
#   * Cleaning only ever touches files discovered under a small, hardcoded
#     allow-list of cache/temp locations. Every path is re-verified against
#     its category root (realpath + case-normalized) immediately before
#     deletion. Nothing outside the allow-list can ever be removed.
#   * Junctions / symlinks / reparse points are never followed.
#   * Locked or protected files raise PermissionError -> counted "skipped",
#     never force-deleted (worst case: read-only bit is cleared, nothing else).
#   * The Recycle Bin is emptied through the official shell API, never by
#     touching $Recycle.Bin directly.
#   * The Large Files scanner is strictly READ-ONLY.
# ============================================================================

import os
import sys
import stat
import time
import shutil
import ctypes
import fnmatch
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QThread, Signal, QRectF
from PySide6.QtGui import QLinearGradient
from PySide6.QtGui import QPainter, QColor, QBrush
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QCheckBox, QProgressBar,
    QScrollArea, QStackedWidget, QLineEdit, QSpinBox, QComboBox,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QFileDialog, QDialog,
    QMessageBox, QPlainTextEdit, QButtonGroup, QSizePolicy, QAbstractItemView,
)

APP_NAME    = "DiskFresh"
APP_VERSION = "1.0"
IS_WINDOWS  = sys.platform == "win32"

FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FILE_ATTRIBUTE_SYSTEM        = 0x0004

# Directories the Large-Files scanner should not descend into.
LARGE_SCAN_SKIP_DIRS = {"$recycle.bin", "system volume information", "winsxs"}


# ============================================================================
#  Small utilities
# ============================================================================

def fmt_size(num) -> str:
    """Human-readable byte size."""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if num < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{int(num):,} B"
            return f"{num:,.1f} {unit}"
        num /= 1024.0
    return "0 B"


def expand_path(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def _alpha(hex_color: str, a: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{a})"


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --- Recycle Bin (official shell API — never touch $Recycle.Bin directly) ---

class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong),
                ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong)]


def recycle_bin_info(drive: str = "C:\\") -> Tuple[int, int]:
    """Return (item_count, total_bytes) of the Recycle Bin on a drive."""
    if not IS_WINDOWS:
        return 0, 0
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(info)
        shell32 = ctypes.windll.shell32
        shell32.SHQueryRecycleBinW.argtypes = [
            ctypes.c_wchar_p, ctypes.POINTER(_SHQUERYRBINFO)]
        if shell32.SHQueryRecycleBinW(ctypes.c_wchar_p(drive),
                                      ctypes.byref(info)) == 0:
            return int(info.i64NumItems), int(info.i64Size)
    except Exception:
        pass
    return 0, 0


def empty_recycle_bin(drive: str = "C:\\") -> bool:
    """Empty the Recycle Bin using the shell API (no confirmation dialog)."""
    if not IS_WINDOWS:
        return False
    SHERB_NOCONFIRMATION, SHERB_NOPROGRESSUI, SHERB_NOSOUND = 0x1, 0x2, 0x4
    try:
        res = ctypes.windll.shell32.SHEmptyRecycleBinW(
            None, ctypes.c_wchar_p(drive),
            SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
        return res == 0
    except Exception:
        return False


# ============================================================================
#  Cleaning categories  (the safety allow-list — everything cleaner touches)
# ============================================================================

@dataclass
class Category:
    key: str
    name: str
    description: str
    icon: str
    color: str
    roots: Tuple[str, ...] = ()
    patterns: Tuple[str, ...] = ()      # empty => all files under roots
    special: Optional[str] = None       # 'recycle_bin' handled via shell API
    needs_admin: bool = False


def _collect_browser_cache_roots(la: str) -> Tuple[str, ...]:
    """Discover existing cache folders for Chromium browsers + Firefox."""
    roots: List[str] = []
    cache_subdirs = ("Cache", "Code Cache", "GPUCache", "Media Cache",
                     "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache")
    chromium_bases = [
        os.path.join(la, "Google", "Chrome", "User Data"),
        os.path.join(la, "Microsoft", "Edge", "User Data"),
        os.path.join(la, "BraveSoftware", "Brave-Browser", "User Data"),
        os.path.join(la, "Vivaldi", "User Data"),
    ]

    def is_profile(name: str) -> bool:
        n = name.lower()
        return n == "default" or n.startswith("profile ") or n == "guest profile"

    for base in chromium_bases:
        if not os.path.isdir(base):
            continue
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for sub in cache_subdirs:                       # root-level shader caches
            p = os.path.join(base, sub)
            if os.path.isdir(p):
                roots.append(p)
        for name in entries:                            # per-profile caches
            if is_profile(name):
                for sub in cache_subdirs:
                    p = os.path.join(base, name, sub)
                    if os.path.isdir(p):
                        roots.append(p)

    # Opera stores caches directly under its install-brand folder.
    for brand in ("Opera Stable", "Opera GX Stable"):
        p = os.path.join(la, "Opera Software", brand, "Cache")
        if os.path.isdir(p):
            roots.append(p)

    # Firefox: <profile>\cache2
    ff = os.path.join(la, "Mozilla", "Firefox", "Profiles")
    if os.path.isdir(ff):
        try:
            for prof in os.listdir(ff):
                p = os.path.join(ff, prof, "cache2")
                if os.path.isdir(p):
                    roots.append(p)
        except OSError:
            pass

    # De-duplicate, keep order.
    seen, out = set(), []
    for r in roots:
        n = os.path.normcase(os.path.normpath(r))
        if n not in seen:
            seen.add(n)
            out.append(r)
    return tuple(out)


def build_categories() -> List[Category]:
    la      = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
    windir  = os.environ.get("WINDIR", r"C:\Windows")
    tempdir = os.environ.get("TEMP") or os.path.join(la, "Temp")

    return [
        Category(
            key="user_temp", name="User Temp Files", icon="🧹", color="#4f8cff",
            description="Leftover installer and app files in your personal Temp folder.",
            roots=(tempdir,),
        ),
        Category(
            key="win_temp", name="Windows Temp Files", icon="🪟", color="#8b5cf6",
            description="Temporary files created by Windows services and installers.",
            roots=(os.path.join(windir, "Temp"),), needs_admin=True,
        ),
        Category(
            key="browsers", name="Browser Caches", icon="🌐", color="#22d3a6",
            description="Page/image caches of Chrome, Edge, Brave, Firefox, Vivaldi, Opera.",
            roots=_collect_browser_cache_roots(la),
        ),
        Category(
            key="win_update", name="Windows Update Cache", icon="⬇️", color="#f5b14c",
            description="Downloaded update installers. Windows re-downloads them if needed.",
            roots=(os.path.join(windir, "SoftwareDistribution", "Download"),
                   os.path.join(windir, "ServiceProfiles", "NetworkService",
                                "AppData", "Local", "Microsoft", "Windows",
                                "DeliveryOptimization", "Cache")),
            needs_admin=True,
        ),
        Category(
            key="thumbnails", name="Thumbnail Cache", icon="🖼️", color="#ec4899",
            description="Explorer thumbnail databases. Windows rebuilds them automatically.",
            roots=(os.path.join(la, "Microsoft", "Windows", "Explorer"),),
            patterns=("thumbcache_*.db", "iconcache_*.db"),
        ),
        Category(
            key="logs", name="Logs & Crash Dumps", icon="📜", color="#f97316",
            description="Windows Error Reporting, app crash dumps and old system logs.",
            roots=(os.path.join(la, "Microsoft", "Windows", "WER"),
                   os.path.join(la, "CrashDumps"),
                   os.path.join(windir, "Logs"),
                   os.path.join(windir, "Minidump"),
                   os.path.join(windir, "LiveKernelReports")),
            patterns=("*.dmp", "*.log", "*.cab", "*.txt", "*.etl", "*"),
            needs_admin=True,
        ),
        Category(
            key="shader", name="GPU Shader Caches", icon="🎮", color="#06b6d4",
            description="DirectX / NVIDIA / AMD shader caches. Games rebuild them on launch.",
            roots=(os.path.join(la, "D3DSCache"),
                   os.path.join(la, "NVIDIA", "DXCache"),
                   os.path.join(la, "NVIDIA", "GLCache"),
                   os.path.join(la, "AMD", "DxCache"),
                   os.path.join(la, "AMD", "Dx9Cache"),
                   os.path.join(la, "AMD", "GLCache"),
                   os.path.join(la, "Intel", "ShaderCache")),
        ),
        Category(
            key="recycle_bin", name="Recycle Bin (C:)", icon="🗑️", color="#ef4444",
            description="Everything currently sitting in the Recycle Bin on drive C:.",
            special="recycle_bin",
        ),
    ]


# ============================================================================
#  File-system walking helpers (error-tolerant, junction-safe)
# ============================================================================

def _walk_files(root: str, patterns: Tuple[str, ...]):
    """Recursively yield (path, size) under root, skipping unreadable entries
    and never following junctions/symlinks (reparse points)."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    if getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    if patterns:
                        name = e.name.lower()
                        if not any(fnmatch.fnmatchcase(name, p) for p in patterns):
                            continue
                    yield e.path, e.stat(follow_symlinks=False).st_size
            except OSError:
                continue


def _root_readable(path: str) -> bool:
    try:
        with os.scandir(path):
            pass
        return True
    except PermissionError:
        return False
    except OSError:
        return os.path.isdir(path)


# ============================================================================
#  Background workers
# ============================================================================

class ScanWorker(QThread):
    """Scans every cleaning category off the GUI thread."""
    live          = Signal(str, int, int)          # key, files so far, bytes so far
    category_done = Signal(str, int, int, bool)   # key, count, size, access_blocked
    overall       = Signal(int, str)              # percent, status text
    finished_scan = Signal(object, bool)          # results dict, cancelled
    fatal         = Signal(str)

    def __init__(self, categories: List[Category]):
        super().__init__()
        self.categories = categories
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self._run()
        except Exception:
            self.fatal.emit(traceback.format_exc())

    def _run(self):
        results = {}
        total = max(1, len(self.categories))
        for i, cat in enumerate(self.categories):
            if self._cancel:
                self.finished_scan.emit(results, True)
                return

            # ---- Recycle Bin (instant, via shell API) ----
            if cat.special == "recycle_bin":
                count, size = recycle_bin_info("C:\\")
                results[cat.key] = {"files": [], "count": count, "size": size,
                                    "special": True, "blocked": False}
                self.category_done.emit(cat.key, count, size, False)
                self.overall.emit(int((i + 1) * 100 / total),
                                  f"Recycle Bin: {count:,} items ({fmt_size(size)})")
                continue

            # ---- Normal directory categories ----
            self.overall.emit(int(i * 100 / total), f"Scanning {cat.name}…")
            files, total_size, blocked = [], 0, False
            last_emit = time.time()
            for root in cat.roots:
                if self._cancel:
                    break
                r = expand_path(root)
                if not os.path.isdir(r):
                    continue
                if not _root_readable(r):
                    blocked = True
                    continue
                for path, size in _walk_files(r, cat.patterns):
                    if self._cancel:
                        break
                    files.append((path, size))
                    total_size += size
                    now = time.time()
                    if now - last_emit > 0.15:
                        last_emit = now
                        self.live.emit(cat.key, len(files), total_size)
            if self._cancel:
                self.finished_scan.emit(results, True)
                return

            results[cat.key] = {"files": files, "count": len(files),
                                "size": total_size, "special": False,
                                "blocked": blocked}
            self.category_done.emit(cat.key, len(files), total_size, blocked)
            self.overall.emit(int((i + 1) * 100 / total),
                              f"{cat.name}: {len(files):,} files ({fmt_size(total_size)})")
        self.finished_scan.emit(results, False)


class CleanWorker(QThread):
    """Deletes exactly the files recorded by the last scan."""
    file_progress  = Signal(int, int, str)            # done, total, current name
    category_done  = Signal(str, int, int, int)       # key, deleted, freed, skipped
    finished_clean = Signal(object, bool)             # summary dict, cancelled
    fatal          = Signal(str)

    def __init__(self, jobs: List[Tuple[Category, dict]]):
        super().__init__()
        self.jobs = jobs
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self._run()
        except Exception:
            self.fatal.emit(traceback.format_exc())

    @staticmethod
    def _delete_file(path: str) -> bool:
        try:
            os.remove(path)
            return True
        except PermissionError:
            try:  # retry once after clearing the read-only bit — nothing more.
                os.chmod(path, stat.S_IWRITE)
                os.remove(path)
                return True
            except OSError:
                return False
        except OSError:
            return False

    def _prune_empty_dirs(self, cat: Category):
        """Remove now-empty subdirectories (rmdir only works on empty dirs,
        so this can never delete data)."""
        for root in cat.roots:
            r = expand_path(root)
            if not os.path.isdir(r):
                continue
            top = os.path.normcase(os.path.realpath(r))
            for dirpath, _dirs, _files in os.walk(r, topdown=False):
                try:
                    if os.path.normcase(os.path.realpath(dirpath)) == top:
                        continue
                    st = os.lstat(dirpath)
                    if getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    os.rmdir(dirpath)
                except OSError:
                    pass

    def _run(self):
        summary = {"freed": 0, "deleted": 0, "skipped": 0, "cats": []}
        total = sum(len(r.get("files", [])) for _, r in self.jobs)
        total += sum(1 for _, r in self.jobs
                     if r.get("special") and r.get("count", 0) > 0)
        done = 0

        for cat, res in self.jobs:
            if self._cancel:
                break
            deleted = freed = skipped = 0

            if res.get("special"):                       # Recycle Bin
                if res.get("count", 0) > 0:
                    done += 1
                    self.file_progress.emit(done, total, "Recycle Bin")
                    if empty_recycle_bin("C:\\"):
                        freed = int(res.get("size", 0))
                    else:
                        skipped = int(res.get("count", 0))
            else:
                # Defence in depth: re-verify every path is inside this
                # category's allow-listed roots right before deletion.
                roots_real = []
                for r in cat.roots:
                    rp = os.path.normcase(os.path.realpath(expand_path(r)))
                    if rp + os.sep not in roots_real:
                        roots_real.append(rp + os.sep)

                for path, size in res["files"]:
                    if self._cancel:
                        break
                    done += 1
                    if done % 15 == 0 or done == total:
                        self.file_progress.emit(done, total, os.path.basename(path))
                    real = os.path.normcase(os.path.realpath(path))
                    if not any(real.startswith(rr) for rr in roots_real):
                        skipped += 1
                        continue
                    if self._delete_file(path):
                        deleted += 1
                        freed += size
                    else:
                        skipped += 1
                if not self._cancel:
                    self._prune_empty_dirs(cat)

            self.category_done.emit(cat.key, deleted, freed, skipped)
            summary["cats"].append((cat.key, cat.name, deleted, freed, skipped))
            summary["freed"] += freed
            summary["deleted"] += deleted
            summary["skipped"] += skipped

        self.finished_clean.emit(summary, self._cancel)


class LargeFileWorker(QThread):
    """READ-ONLY scanner for files above a size threshold."""
    found = Signal(str, str, int, float, bool)   # name, dir, size, mtime, is_system
    tick  = Signal(int, int, int)                # dirs checked, files found, bytes
    done_scan = Signal(int, int, int, bool)      # dirs, files, bytes, cancelled
    fatal = Signal(str)

    def __init__(self, root: str, min_bytes: int):
        super().__init__()
        self.root = root
        self.min_bytes = min_bytes
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self._run()
        except Exception:
            self.fatal.emit(traceback.format_exc())

    def _run(self):
        dirs = found = fsize = 0
        stack = [self.root]
        last = time.time()
        while stack:
            if self._cancel:
                break
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    entries = list(it)
            except OSError:
                continue
            dirs += 1
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        if getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                            continue
                        if e.name.lower() in LARGE_SCAN_SKIP_DIRS:
                            continue
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        if st.st_size >= self.min_bytes:
                            is_sys = bool(getattr(st, "st_file_attributes", 0)
                                          & FILE_ATTRIBUTE_SYSTEM)
                            self.found.emit(e.name, d, st.st_size,
                                            st.st_mtime, is_sys)
                            found += 1
                            fsize += st.st_size
                except OSError:
                    continue
            now = time.time()
            if now - last > 0.2:
                last = now
                self.tick.emit(dirs, found, fsize)
        self.done_scan.emit(dirs, found, fsize, self._cancel)


# ============================================================================
#  Custom widgets
# ============================================================================

STYLESHEET = """
QWidget { background-color:#0d1117; color:#dce3ee; font-family:"Segoe UI"; font-size:13px; }
QLabel { background:transparent; }
QLabel#H1 { font-size:19px; font-weight:700; color:#ffffff; }
QLabel#Dim { color:#8b96a8; font-size:12px; }

#Sidebar { background-color:#10151d; border-right:1px solid #1c2431; }
#AppTitle { font-size:17px; font-weight:700; color:#ffffff; }
#AppSub { color:#7c8798; font-size:11px; }
QPushButton#NavBtn { background:transparent; border:none; border-radius:9px;
    padding:11px 14px; text-align:left; color:#8b96a8; }
QPushButton#NavBtn:hover { background:#171e29; color:#dce3ee; }
QPushButton#NavBtn:checked { background:#1d2736; color:#ffffff; font-weight:600; }

QFrame#Panel { background-color:#141a24; border:1px solid #1f2836; border-radius:14px; }
QFrame#Card { background-color:#141a24; border:1px solid #1f2836; border-radius:14px; }
QFrame#Card:hover { border-color:#2c3a52; }
QFrame#Chip { background-color:#121826; border:1px solid #202a3c; border-radius:12px; }

QCheckBox { background:transparent; spacing:8px; }
QCheckBox::indicator { width:20px; height:20px; border-radius:6px;
    border:2px solid #38445c; background:#10151d; }
QCheckBox::indicator:hover { border-color:#4f8cff; }
QCheckBox::indicator:checked { background:#4f8cff; border-color:#4f8cff; }
QCheckBox::indicator:disabled { border-color:#2a3345; background:#121822; }

QPushButton { background:#1a2230; border:1px solid #26334a; border-radius:9px;
    padding:9px 18px; color:#dce3ee; }
QPushButton:hover { background:#212c40; border-color:#33456a; }
QPushButton:disabled { color:#566072; background:#151b26; border-color:#202a3a; }
QPushButton#Primary { background:#4f8cff; border:none; color:#ffffff; font-weight:600; }
QPushButton#Primary:hover { background:#689dff; }
QPushButton#Primary:disabled { background:#28508f; color:#9db9e8; }
QPushButton#Danger { background:#e5484d; border:none; color:#ffffff; font-weight:600; }
QPushButton#Danger:hover { background:#f25c60; }
QPushButton#Danger:disabled { background:#5a2b30; color:#c9979a; }

QProgressBar { background:#161d29; border:1px solid #212c3e; border-radius:9px;
    min-height:18px; text-align:center; color:#cfe0ff; font-size:11px; }
QProgressBar::chunk { border-radius:8px; margin:2px;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #4f8cff, stop:1 #22d3a6); }

QLineEdit, QSpinBox, QComboBox { background:#121824; border:1px solid #26334a;
    border-radius:8px; padding:7px 10px; selection-background-color:#4f8cff; }
QComboBox QAbstractItemView { background:#141a24; border:1px solid #26334a;
    selection-background-color:#22304a; }

QTreeWidget { background:#10151d; alternate-background-color:#131924;
    border:1px solid #1f2836; border-radius:10px; }
QTreeWidget::item { height:26px; }
QTreeWidget::item:selected { background:#22304a; color:#ffffff; }
QHeaderView::section { background:#131a26; color:#8b96a8; border:none;
    border-bottom:1px solid #1f2836; padding:7px; }

QPlainTextEdit#LogView { background:#10151d; border:1px solid #1f2836;
    border-radius:10px; font-family:"Consolas"; font-size:12px; color:#9fb0c8; }

QScrollArea { border:none; background:transparent; }
QScrollArea > QWidget > QWidget { background:transparent; }
QScrollBar:vertical { background:transparent; width:10px; margin:2px; }
QScrollBar::handle:vertical { background:#26334a; border-radius:5px; min-height:30px; }
QScrollBar::handle:vertical:hover { background:#33456a; }
QScrollBar:horizontal { background:transparent; height:10px; margin:2px; }
QScrollBar::handle:horizontal { background:#26334a; border-radius:5px; min-width:30px; }
QScrollBar::add-line, QScrollBar::sub-line { width:0; height:0; }
QToolTip { background:#1a2230; color:#dce3ee; border:1px solid #26334a; padding:4px; }
"""


class DriveBar(QWidget):
    """Custom-painted drive usage bar."""
    def __init__(self):
        super().__init__()
        self.setFixedHeight(18)
        self.frac = 0.0

    def set_fraction(self, f: float):
        self.frac = max(0.0, min(1.0, f))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#1a2231"))
        p.drawRoundedRect(QRectF(self.rect()), 9, 9)
        w = self.width() * self.frac
        if w >= 10:
            grad = QLinearGradient(0, 0, w, 0)
            grad.setColorAt(0.0, QColor("#4f8cff"))
            grad.setColorAt(1.0, QColor("#22d3a6"))
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(0, 0, w, self.height()), 9, 9)


class CategoryCard(QFrame):
    """One cleaning category: icon, description, count, reclaimable size."""
    toggled = Signal()

    def __init__(self, cat: Category):
        super().__init__()
        self.cat = cat
        self.setObjectName("Card")
        self.setFixedHeight(152)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Checked locations:\n" +
                        "\n".join(expand_path(r) for r in cat.roots)
                        if cat.roots else "Emptied via the Windows shell API.")

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 14, 14)
        v.setSpacing(7)

        top = QHBoxLayout()
        top.setSpacing(10)
        icon = QLabel(cat.icon)
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            f"background:{_alpha(cat.color, 0.18)}; color:{cat.color};"
            f"border-radius:12px; font-size:19px;")
        top.addWidget(icon)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        t = QLabel(cat.name)
        t.setStyleSheet("font-weight:600; font-size:13px;")
        titles.addWidget(t)
        if cat.needs_admin:
            lock = QLabel("🔒 some folders need administrator rights")
            lock.setStyleSheet("color:#f5b14c; font-size:10px;")
            titles.addWidget(lock)
        top.addLayout(titles)
        top.addStretch(1)

        self.check = QCheckBox()
        self.check.setEnabled(False)
        self.check.toggled.connect(self.toggled.emit)
        top.addWidget(self.check)
        v.addLayout(top)

        desc = QLabel(cat.description)
        desc.setObjectName("Dim")
        desc.setWordWrap(True)
        v.addWidget(desc)

        bottom = QHBoxLayout()
        self.size_lbl = QLabel("—")
        self.size_lbl.setStyleSheet(
            f"color:{cat.color}; font-size:18px; font-weight:700;")
        self.count_lbl = QLabel("Not scanned")
        self.count_lbl.setObjectName("Dim")
        bottom.addWidget(self.size_lbl)
        bottom.addSpacing(8)
        bottom.addWidget(self.count_lbl)
        bottom.addStretch(1)
        v.addLayout(bottom)

    # ---- states ----
    def reset(self):
        self.check.setEnabled(False)
        self.check.setChecked(False)
        self.size_lbl.setText("—")
        self.count_lbl.setText("Not scanned")

    def set_live(self, n: int, size: int):
        self.size_lbl.setText(fmt_size(size))
        self.count_lbl.setText(f"{n:,} files so far…")

    def set_result(self, n: int, size: int, blocked: bool):
        self.size_lbl.setText(fmt_size(size))
        extra = " · some folders need admin" if blocked and n == 0 else ""
        self.count_lbl.setText(f"{n:,} files{extra}")
        self.check.setEnabled(n > 0)

    def set_cleaned(self, skipped: int):
        self.check.setEnabled(False)
        self.check.setChecked(False)
        if skipped:
            self.size_lbl.setText("Partially cleaned")
            self.count_lbl.setText(f"{skipped:,} files skipped (in use / locked)")
        else:
            self.size_lbl.setText("✓ Cleaned")
            self.count_lbl.setText("0 files")

    def is_selected(self) -> bool:
        return self.check.isEnabled() and self.check.isChecked()

    def mousePressEvent(self, event):
        if self.check.isEnabled():
            self.check.toggle()
        super().mousePressEvent(event)


class FileItem(QTreeWidgetItem):
    """Tree item with numeric sorting for Size / Modified columns."""
    def __lt__(self, other):
        col = self.treeWidget().sortColumn()
        if col in (2, 3):
            return self.data(col, Qt.ItemDataRole.UserRole) < \
                   other.data(col, Qt.ItemDataRole.UserRole)
        return super().__lt__(other)


# ============================================================================
#  Dialogs
# ============================================================================

class ConfirmDialog(QDialog):
    def __init__(self, parent, items: List[Tuple[str, int, int]]):
        super().__init__(parent)
        self.setWindowTitle("Confirm Cleanup")
        self.setModal(True)
        self.setMinimumWidth(540)

        v = QVBoxLayout(self)
        v.setSpacing(12)
        head = QLabel("⚠️  You are about to permanently delete:")
        head.setStyleSheet("font-size:15px; font-weight:600;")
        v.addWidget(head)

        panel = QFrame()
        panel.setObjectName("Panel")
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(16, 12, 16, 12)
        tf = cf = sf = 0
        for name, count, size in items:
            row = QHBoxLayout()
            row.addWidget(QLabel(name))
            lbl = QLabel(f"{count:,} files   ·   {fmt_size(size)}")
            lbl.setObjectName("Dim")
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            row.addWidget(lbl)
            pv.addLayout(row)
            tf += count; sf += size; cf += 1
        pv.addWidget(self._hr())
        tot = QHBoxLayout()
        t1 = QLabel(f"Total ({cf} categories)")
        t1.setStyleSheet("font-weight:700;")
        t2 = QLabel(f"{tf:,} files   ·   {fmt_size(sf)}")
        t2.setStyleSheet("font-weight:700; color:#22d3a6;")
        t2.setAlignment(Qt.AlignmentFlag.AlignRight)
        tot.addWidget(t1); tot.addStretch(1); tot.addWidget(t2)
        pv.addLayout(tot)
        v.addWidget(panel)

        warn = QLabel("Deleted files cannot be recovered. Recycle Bin contents are erased "
                      "permanently.\nTip: close browsers and other programs first — files "
                      "that are in use are skipped automatically.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color:#d9a13c; font-size:12px;")
        v.addWidget(warn)

        btns = QHBoxLayout()
        cancel = QPushButton("Keep Files")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("🗑  Delete Now")
        ok.setObjectName("Danger")
        ok.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        v.addLayout(btns)

    @staticmethod
    def _hr():
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color:#243044;")
        return line


class ResultDialog(QDialog):
    def __init__(self, parent, summary: dict, cancelled: bool):
        super().__init__(parent)
        self.setWindowTitle("Cleanup Complete")
        self.setModal(True)
        self.setMinimumWidth(500)
        v = QVBoxLayout(self)
        v.setSpacing(10)

        big = QLabel(("Freed " if not cancelled else "Partially cleaned — ")
                     + fmt_size(summary["freed"]))
        big.setStyleSheet("font-size:24px; font-weight:800; color:#2fd08c;")
        v.addWidget(big)
        sub = QLabel(f"{summary['deleted']:,} files deleted"
                     + (f"  ·  {summary['skipped']:,} skipped"
                        if summary["skipped"] else ""))
        sub.setObjectName("Dim")
        v.addWidget(sub)
        if cancelled:
            note = QLabel("The cleanup was cancelled — remaining items were left untouched.")
            note.setStyleSheet("color:#f5b14c;")
            v.addWidget(note)

        for _key, name, deleted, freed, skipped in summary["cats"]:
            v.addWidget(QLabel(f"•  {name}:  {deleted:,} deleted "
                               f"({fmt_size(freed)}), {skipped:,} skipped"))

        if summary["skipped"]:
            hint = QLabel("Skipped files are in use by running programs or require "
                          "administrator rights. Close apps / run as Administrator and "
                          "scan again to clean them.")
            hint.setWordWrap(True)
            hint.setStyleSheet("color:#d9a13c; font-size:12px;")
            v.addWidget(hint)

        ok = QPushButton("Done")
        ok.setObjectName("Primary")
        ok.clicked.connect(self.accept)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(ok)
        v.addLayout(row)


# ============================================================================
#  Main window
# ============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — Windows Disk Cleaner")
        self.resize(1180, 760)
        self.setMinimumSize(1020, 680)

        self.categories   = build_categories()
        self.cards        = {}
        self.scan_results = {}
        self.scan_worker: Optional[ScanWorker] = None
        self.clean_worker: Optional[CleanWorker] = None
        self.lf_worker: Optional[LargeFileWorker] = None
        self._clean_jobs: List[Tuple[Category, dict]] = []
        self.lf_count = 0
        self.lf_size  = 0

        self.root = QWidget()
        self.root.setObjectName("Root")
        self.setCentralWidget(self.root)
        outer = QHBoxLayout(self.root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Create the page stack before building the sidebar because the
        # sidebar navigation connects directly to self.stack.
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        outer.insertWidget(0, self._build_sidebar())
        self.stack.addWidget(self._build_dashboard())   # index 0
        self.stack.addWidget(self._build_large_files()) # index 1
        self.stack.addWidget(self._build_log_page())    # index 2

        self.refresh_drive_info()
        admin = is_admin()
        self.log(f"{APP_NAME} v{APP_VERSION} started "
                 f"(Python {sys.version.split()[0]}, "
                 f"{'Administrator' if admin else 'limited user'})")
        if not admin:
            self.log("Tip: run as Administrator to clean Windows Temp, "
                     "Update cache and system logs.")

    # ------------------------------------------------------------------ nav
    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(232)
        v = QVBoxLayout(side)
        v.setContentsMargins(14, 18, 14, 14)
        v.setSpacing(6)

        t = QLabel("🧽 DiskFresh")
        t.setObjectName("AppTitle")
        s = QLabel("Safe disk cleanup for Windows")
        s.setObjectName("AppSub")
        v.addWidget(t)
        v.addWidget(s)
        v.addSpacing(14)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for idx, (icon, label) in enumerate(
                [("🧼", "Cleaner"), ("📦", "Large Files"), ("📋", "Activity Log")]):
            b = QPushButton(f"   {icon}   {label}")
            b.setObjectName("NavBtn")
            b.setCheckable(True)
            b.setChecked(idx == 0)
            self.nav_group.addButton(b, idx)
            v.addWidget(b)
        self.nav_group.idClicked.connect(self.stack.setCurrentIndex)

        v.addStretch(1)
        self.admin_badge = QLabel()
        self.restart_btn = QPushButton("⬆  Restart as Admin")
        self.restart_btn.clicked.connect(self.restart_as_admin)
        self.restart_btn.setVisible(not is_admin())
        v.addWidget(self.admin_badge)
        v.addWidget(self.restart_btn)
        ver = QLabel(f"v{APP_VERSION} · safe by design")
        ver.setObjectName("AppSub")
        v.addWidget(ver)

        self._update_admin_badge()
        return side

    def _update_admin_badge(self):
        if is_admin():
            self.admin_badge.setText("●  Running as Administrator")
            self.admin_badge.setStyleSheet("color:#2fd08c; font-weight:600;")
        else:
            self.admin_badge.setText("●  Limited mode — some areas locked")
            self.admin_badge.setStyleSheet("color:#f5b14c; font-weight:600;")

    def restart_as_admin(self):
        if not IS_WINDOWS:
            return
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            exe = sys.executable
            params = f'"{os.path.abspath(__file__)}"'
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        if ret > 32:
            QApplication.instance().quit()

    # ------------------------------------------------------------- dashboard
    def _build_dashboard(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(18, 18, 18, 16)
        v.setSpacing(14)

        # ---- header: drive usage ----
        header = QFrame(); header.setObjectName("Panel")
        h = QHBoxLayout(header)
        h.setContentsMargins(20, 16, 20, 16)
        h.setSpacing(24)

        left = QVBoxLayout()
        title = QLabel("Local Disk (C:)")
        title.setObjectName("H1")
        self.drive_bar = DriveBar()
        self.used_lbl = QLabel("—"); self.used_lbl.setObjectName("Dim")
        self.free_lbl = QLabel("—"); self.free_lbl.setObjectName("Dim")
        self.free_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        left.addWidget(title)
        left.addWidget(self.drive_bar)
        row = QHBoxLayout(); row.addWidget(self.used_lbl); row.addWidget(self.free_lbl)
        left.addLayout(row)
        h.addLayout(left, 1)

        chips = QHBoxLayout(); chips.setSpacing(10)
        self.chip_vals = {}
        for key, cap in (("total", "Total"), ("used", "Used"), ("free", "Free")):
            chip = QFrame(); chip.setObjectName("Chip")
            chip.setFixedWidth(118)
            cv = QVBoxLayout(chip)
            cv.setContentsMargins(10, 10, 10, 10)
            val = QLabel("—"); val.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val.setStyleSheet("font-size:15px; font-weight:700;")
            cap_l = QLabel(cap); cap_l.setObjectName("Dim")
            cap_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cv.addWidget(val); cv.addWidget(cap_l)
            self.chip_vals[key] = val
            chips.addWidget(chip)
        h.addLayout(chips)

        v.addWidget(header)

        # ---- category cards ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(2, 2, 10, 2)
        grid.setSpacing(14)
        cols = 3
        for i, cat in enumerate(self.categories):
            card = CategoryCard(cat)
            card.toggled.connect(self.update_clean_btn)
            grid.addWidget(card, i // cols, i % cols)
            self.cards[cat.key] = card
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        grid.setRowStretch(len(self.categories) // cols + 1, 1)
        scroll.setWidget(host)
        v.addWidget(scroll, 1)

        # ---- action bar ----
        bar = QFrame(); bar.setObjectName("Panel")
        bv = QVBoxLayout(bar)
        bv.setContentsMargins(16, 14, 16, 14)
        bv.setSpacing(10)

        row1 = QHBoxLayout()
        self.scan_btn = QPushButton("🔍  Scan Now")
        self.scan_btn.setObjectName("Primary")
        self.scan_btn.clicked.connect(self.start_or_cancel_scan)
        self.sel_all_btn = QPushButton("Select All")
        self.sel_all_btn.clicked.connect(lambda: self._select_all(True))
        self.sel_none_btn = QPushButton("Clear")
        self.sel_none_btn.clicked.connect(lambda: self._select_all(False))
        self.last_scan_lbl = QLabel("Not scanned yet")
        self.last_scan_lbl.setObjectName("Dim")
        self.status_lbl = QLabel("Ready.")
        self.status_lbl.setObjectName("Dim")
        row1.addWidget(self.scan_btn)
        row1.addWidget(self.sel_all_btn)
        row1.addWidget(self.sel_none_btn)
        row1.addStretch(1)
        row1.addWidget(self.status_lbl)
        bv.addLayout(row1)

        row2 = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.clean_btn = QPushButton("Clean Selected")
        self.clean_btn.setObjectName("Danger")
        self.clean_btn.setEnabled(False)
        self.clean_btn.clicked.connect(self.confirm_clean)
        row2.addWidget(self.progress, 1)
        row2.addWidget(self.clean_btn)
        bv.addLayout(row2)

        v.addWidget(bar)
        return page

    # ------------------------------------------------------------ large files
    def _build_large_files(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(18, 18, 18, 16)
        v.setSpacing(12)

        title = QLabel("📦  Large Files Finder")
        title.setObjectName("H1")
        sub = QLabel("Read-only scan — files are never deleted or modified here. "
                     "Scanning a whole drive can take a few minutes.")
        sub.setObjectName("Dim")
        v.addWidget(title)
        v.addWidget(sub)

        bar = QFrame(); bar.setObjectName("Panel")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(16, 12, 16, 12)
        bh.setSpacing(8)
        bh.addWidget(QLabel("Files larger than"))
        self.lf_spin = QSpinBox()
        self.lf_spin.setRange(1, 100000)
        self.lf_spin.setValue(200)
        self.lf_unit = QComboBox()
        self.lf_unit.addItems(["MB", "GB"])
        bh.addWidget(self.lf_spin)
        bh.addWidget(self.lf_unit)
        bh.addSpacing(8)
        self.lf_path = QLineEdit("C:\\")
        bh.addWidget(self.lf_path, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._lf_browse)
        bh.addWidget(browse)
        self.lf_scan_btn = QPushButton("🔍  Scan")
        self.lf_scan_btn.setObjectName("Primary")
        self.lf_scan_btn.clicked.connect(self.start_large_scan)
        bh.addWidget(self.lf_scan_btn)
        self.lf_cancel_btn = QPushButton("Cancel")
        self.lf_cancel_btn.setEnabled(False)
        self.lf_cancel_btn.clicked.connect(self._lf_cancel)
        bh.addWidget(self.lf_cancel_btn)
        v.addWidget(bar)

        self.lf_tree = QTreeWidget()
        self.lf_tree.setHeaderLabels(["Name", "Location", "Size", "Modified"])
        self.lf_tree.setRootIsDecorated(False)
        self.lf_tree.setUniformRowHeights(True)
        self.lf_tree.setAlternatingRowColors(True)
        self.lf_tree.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.lf_tree.setSortingEnabled(False)
        self.lf_tree.setColumnWidth(0, 300)
        self.lf_tree.setColumnWidth(2, 110)
        self.lf_tree.setColumnWidth(3, 140)
        self.lf_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.lf_tree.itemDoubleClicked.connect(lambda *_: self._lf_open_folder())
        v.addWidget(self.lf_tree, 1)

        foot = QHBoxLayout()
        self.lf_status = QLabel("Enter a folder and a minimum size, then press Scan.")
        self.lf_status.setObjectName("Dim")
        self.lf_totals = QLabel("")
        self.lf_totals.setObjectName("Dim")
        open_btn = QPushButton("📂  Open Containing Folder")
        open_btn.clicked.connect(self._lf_open_folder)
        copy_btn = QPushButton("Copy Path")
        copy_btn.clicked.connect(self._lf_copy_path)
        foot.addWidget(open_btn)
        foot.addWidget(copy_btn)
        foot.addStretch(1)
        foot.addWidget(self.lf_totals)
        v.addLayout(foot)
        v.addWidget(self.lf_status)
        return page

    def _lf_browse(self):
        d = QFileDialog.getExistingDirectory(self, "Choose folder to search",
                                             self.lf_path.text() or "C:\\")
        if d:
            self.lf_path.setText(d)

    def start_large_scan(self):
        if self.lf_worker and self.lf_worker.isRunning():
            return
        root = self.lf_path.text().strip() or "C:\\"
        if not os.path.isdir(root):
            QMessageBox.warning(self, APP_NAME, "Folder does not exist.")
            return
        mult = 1024 ** 3 if self.lf_unit.currentText() == "GB" else 1024 ** 2
        min_bytes = int(self.lf_spin.value() * mult)

        self.lf_tree.clear()
        self.lf_tree.setSortingEnabled(False)
        self.lf_count = 0
        self.lf_size = 0
        self.lf_scan_btn.setEnabled(False)
        self.lf_cancel_btn.setEnabled(True)
        self.lf_totals.setText("")
        self.log(f"Large-files scan started: root='{root}', "
                 f"min={fmt_size(min_bytes)}")

        self.lf_worker = LargeFileWorker(root, min_bytes)
        self.lf_worker.found.connect(self.on_lf_found)
        self.lf_worker.tick.connect(self.on_lf_tick)
        self.lf_worker.done_scan.connect(self.on_lf_done)
        self.lf_worker.fatal.connect(self.on_lf_fatal)
        self.lf_worker.start()

    def _lf_cancel(self):
        if self.lf_worker and self.lf_worker.isRunning():
            self.lf_worker.cancel()
            self.lf_cancel_btn.setEnabled(False)
            self.lf_status.setText("Cancelling…")

    def on_lf_found(self, name, directory, size, mtime, is_system):
        item = FileItem([name, directory, fmt_size(size),
                         datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")])
        item.setData(2, Qt.ItemDataRole.UserRole, size)
        item.setData(3, Qt.ItemDataRole.UserRole, mtime)
        if is_system:
            item.setForeground(0, QBrush(QColor("#f5b14c")))
            item.setToolTip(0, "Windows system file — do not delete.")
        item.setToolTip(1, os.path.join(directory, name))
        self.lf_tree.addTopLevelItem(item)
        self.lf_count += 1
        self.lf_size += size

    def on_lf_tick(self, dirs, found, size):
        self.lf_status.setText(
            f"Checked {dirs:,} folders — {found:,} matching files found "
            f"({fmt_size(size)})")

    def on_lf_done(self, dirs, found, size, cancelled):
        self.lf_scan_btn.setEnabled(True)
        self.lf_cancel_btn.setEnabled(False)
        self.lf_tree.setSortingEnabled(True)
        self.lf_tree.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        word = "cancelled after checking" if cancelled else "Checked"
        self.lf_status.setText(
            f"{word} {dirs:,} folders. {found:,} files over the threshold "
            f"({fmt_size(size)}).")
        self.lf_totals.setText(f"{found:,} files  ·  {fmt_size(size)}")
        self.log(f"Large-files scan finished: {found:,} files, "
                 f"{fmt_size(size)}{' (cancelled)' if cancelled else ''}")
        self.lf_worker = None

    def on_lf_fatal(self, text):
        self.lf_scan_btn.setEnabled(True)
        self.lf_cancel_btn.setEnabled(False)
        self.log("Large-files scan error — see message box.")
        QMessageBox.critical(self, APP_NAME,
                             "The large-files scanner hit an unexpected error:\n\n"
                             + text[-2000:])
        self.lf_worker = None

    def _lf_selected(self) -> Optional[FileItem]:
        items = self.lf_tree.selectedItems()
        return items[0] if items else None

    def _lf_open_folder(self):
        item = self._lf_selected()
        if not item:
            return
        folder = item.text(1)
        try:
            os.startfile(folder)   # Windows shell — opens Explorer
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Could not open folder:\n{e}")

    def _lf_copy_path(self):
        item = self._lf_selected()
        if item:
            QApplication.clipboard().setText(os.path.join(item.text(1), item.text(0)))

    # ---------------------------------------------------------------- log
    def _build_log_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(18, 18, 18, 16)
        t = QLabel("📋  Activity Log")
        t.setObjectName("H1")
        v.addWidget(t)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        v.addWidget(self.log_view, 1)
        return page

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}]  {msg}")

    # ================================================== dashboard: scanning
    def refresh_drive_info(self):
        try:
            u = shutil.disk_usage("C:\\")
            self.drive_bar.set_fraction(u.used / u.total)
            self.chip_vals["total"].setText(fmt_size(u.total))
            self.chip_vals["used"].setText(fmt_size(u.used))
            self.chip_vals["free"].setText(fmt_size(u.free))
            self.used_lbl.setText(f"{fmt_size(u.used)} used "
                                  f"({u.used * 100 / u.total:.1f}%)")
            self.free_lbl.setText(f"{fmt_size(u.free)} free of {fmt_size(u.total)}")
        except OSError:
            self.free_lbl.setText("Drive C: not available")

    def start_or_cancel_scan(self):
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.cancel()
            self.scan_btn.setEnabled(False)
            self.status_lbl.setText("Cancelling…")
            return
        if self.clean_worker and self.clean_worker.isRunning():
            return

        self.scan_results = {}
        for card in self.cards.values():
            card.reset()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.scan_btn.setText("⏹  Cancel Scan")
        self.clean_btn.setEnabled(False)
        self.sel_all_btn.setEnabled(False)
        self.sel_none_btn.setEnabled(False)
        self.status_lbl.setText("Preparing scan…")
        self.log("Scan started.")

        self.scan_worker = ScanWorker(self.categories)
        self.scan_worker.live.connect(self.on_scan_live)
        self.scan_worker.category_done.connect(self.on_scan_category)
        self.scan_worker.overall.connect(self.on_scan_overall)
        self.scan_worker.finished_scan.connect(self.on_scan_finished)
        self.scan_worker.fatal.connect(self.on_scan_fatal)
        self.scan_worker.start()

    def on_scan_live(self, key, n, size):
        self.cards[key].set_live(n, size)
        self.status_lbl.setText(f"Scanning {self.cards[key].cat.name}… "
                                f"{n:,} files found")

    def on_scan_category(self, key, count, size, blocked):
        self.cards[key].set_result(count, size, blocked)
        self.log(f"Scanned {self.cards[key].cat.name}: "
                 f"{count:,} files, {fmt_size(size)}"
                 + (" (some folders inaccessible)" if blocked else ""))

    def on_scan_overall(self, pct, text):
        self.progress.setValue(pct)
        self.status_lbl.setText(text)

    def on_scan_finished(self, results, cancelled):
        self.scan_results = results or {}
        now = datetime.now().strftime("%H:%M:%S")
        self.last_scan_lbl.setText(f"Last scan: {now}")
        for cat in self.categories:
            res = self.scan_results.get(cat.key)
            if res:
                self.cards[cat.key].set_result(res["count"], res["size"],
                                               res.get("blocked", False))
                if res["count"] > 0:
                    self.cards[cat.key].check.setChecked(True)
            else:
                self.cards[cat.key].reset()

        total_n = sum(r["count"] for r in self.scan_results.values())
        total_s = sum(r["size"] for r in self.scan_results.values())
        self.progress.setValue(100 if not cancelled else self.progress.value())
        self.status_lbl.setText(
            ("Scan cancelled — partial results shown." if cancelled else
             f"Scan complete — {total_n:,} files, {fmt_size(total_s)} reclaimable."))
        self.scan_btn.setText("🔍  Scan Again")
        self.scan_btn.setEnabled(True)
        self.sel_all_btn.setEnabled(True)
        self.sel_none_btn.setEnabled(True)
        self.update_clean_btn()
        self.refresh_drive_info()
        self.log(f"Scan finished: {total_n:,} files, {fmt_size(total_s)}"
                 + (" (cancelled)" if cancelled else ""))
        self.scan_worker = None

    def on_scan_fatal(self, text):
        self.status_lbl.setText("Scan failed.")
        self.scan_btn.setText("🔍  Scan Now")
        self.scan_btn.setEnabled(True)
        self.sel_all_btn.setEnabled(True)
        self.sel_none_btn.setEnabled(True)
        self.log("Scan failed — see message box.")
        QMessageBox.critical(self, APP_NAME,
                             "The scanner hit an unexpected error:\n\n" + text[-2000:])
        self.scan_worker = None

    def _select_all(self, checked: bool):
        for card in self.cards.values():
            if card.check.isEnabled():
                card.check.setChecked(checked)

    def update_clean_btn(self):
        files = total = 0
        for key, res in self.scan_results.items():
            if self.cards[key].is_selected():
                files += res["count"]
                total += res["size"]
        if files > 0:
            self.clean_btn.setText(f"🗑  Clean Selected  ({fmt_size(total)})")
            self.clean_btn.setEnabled(True)
        else:
            self.clean_btn.setText("🗑  Clean Selected")
            self.clean_btn.setEnabled(False)

    # =================================================== dashboard: cleaning
    def confirm_clean(self):
        jobs = [(cat, self.scan_results[cat.key]) for cat in self.categories
                if self.cards[cat.key].is_selected() and cat.key in self.scan_results]
        if not jobs:
            return
        items = [(cat.name, res["count"], res["size"]) for cat, res in jobs]
        if ConfirmDialog(self, items).exec() != QDialog.DialogCode.Accepted:
            return

        self._clean_jobs = jobs
        for card in self.cards.values():
            card.check.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.sel_all_btn.setEnabled(False)
        self.sel_none_btn.setEnabled(False)
        self.clean_btn.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_lbl.setText("Cleaning…")
        self.log(f"Cleanup started: {len(jobs)} categories, "
                 f"{sum(r['count'] for _, r in jobs):,} files.")

        self.clean_worker = CleanWorker(jobs)
        self.clean_worker.file_progress.connect(self.on_clean_progress)
        self.clean_worker.category_done.connect(self.on_clean_category)
        self.clean_worker.finished_clean.connect(self.on_clean_finished)
        self.clean_worker.fatal.connect(self.on_clean_fatal)
        self.clean_worker.start()

    def on_clean_progress(self, done, total, name):
        self.progress.setValue(int(done * 100 / max(1, total)))
        self.status_lbl.setText(f"Cleaning… {done:,} / {total:,} files")

    def on_clean_category(self, key, deleted, freed, skipped):
        name = self.cards[key].cat.name
        self.log(f"Cleaned {name}: {deleted:,} deleted, "
                 f"{fmt_size(freed)} freed, {skipped:,} skipped.")

    def on_clean_finished(self, summary, cancelled):
        for key, _name, _d, _f, skipped in summary["cats"]:
            self.cards[key].set_cleaned(skipped)
            self.scan_results[key] = {"files": [], "count": 0, "size": 0,
                                      "special": False, "blocked": False}
        self.status_lbl.setText(
            f"Cleanup complete — {fmt_size(summary['freed'])} freed."
            + (" (cancelled)" if cancelled else ""))
        self.progress.setValue(100)
        self.scan_btn.setEnabled(True)
        self.sel_all_btn.setEnabled(True)
        self.sel_none_btn.setEnabled(True)
        self.update_clean_btn()
        self.refresh_drive_info()
        self.log(f"Cleanup finished: {fmt_size(summary['freed'])} freed, "
                 f"{summary['skipped']:,} skipped.")
        ResultDialog(self, summary, cancelled).exec()
        self.clean_worker = None

    def on_clean_fatal(self, text):
        self.status_lbl.setText("Cleanup failed.")
        self.scan_btn.setEnabled(True)
        self.sel_all_btn.setEnabled(True)
        self.sel_none_btn.setEnabled(True)
        self.update_clean_btn()
        self.log("Cleanup failed — see message box.")
        QMessageBox.critical(self, APP_NAME,
                             "The cleaner hit an unexpected error:\n\n" + text[-2000:])
        self.clean_worker = None

    # ---------------------------------------------------------------- close
    def closeEvent(self, event):
        running = [w for w in (self.scan_worker, self.clean_worker, self.lf_worker)
                   if w and w.isRunning()]
        if running:
            answer = QMessageBox.question(
                self, APP_NAME,
                "A scan or cleanup is still running.\nCancel it and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for w in running:
                w.cancel()
            for w in running:
                if not w.wait(3000):
                    w.terminate()
                    w.wait(1000)
        event.accept()


# ============================================================================
#  Entry point
# ============================================================================

def main():
    if not IS_WINDOWS:
        print(f"{APP_NAME} requires Windows — it uses Windows shell APIs and paths.")
        sys.exit(1)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    f = app.font()
    f.setFamily("Segoe UI")
    f.setPointSize(10)
    app.setFont(f)
    app.setStyleSheet(STYLESHEET)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
