# DiskFresh

**A beautiful, safe and practical Windows disk-cleaning utility built with Python and PySide6.**

DiskFresh helps you understand where disk space is being used and safely reclaim space from common temporary and cache locations — without turning disk cleaning into a risky one-click operation.

![Platform](https://img.shields.io/badge/platform-Windows-0078D4)
![Python](https://img.shields.io/badge/Python-3.14%2B-3776AB)
![PySide6](https://img.shields.io/badge/UI-PySide6-41CD52)
![License](https://img.shields.io/badge/license-MIT-green)

---

## ✨ Why DiskFresh?

Windows systems gradually accumulate temporary files, caches, logs and other disposable data.

DiskFresh provides a clear way to:

- 🔍 **Scan** for potentially removable files
- 📊 **See** how much space can be recovered
- 🧹 **Clean** only what you explicitly select
- 📁 **Find large files** without deleting them
- 🛡️ **Protect** personal and system-critical files
- ⚡ **Keep the interface responsive** during scans
- 📝 **Review cleaning results** after an operation

The philosophy is simple:

> **Show first. Explain clearly. Ask before deleting.**

---

## 🚀 Features

### 🧹 Safe Cleaner

Scans approved Windows locations for common disposable data, including:

- User temporary files
- Windows temporary files
- Browser caches
- Windows Update cache
- Thumbnail cache
- Application caches
- Log files
- Recycle Bin contents

Results are grouped by category so you can decide what to clean.

### 📦 Large Files

Find large files on selected drives or folders.

Choose a threshold such as:

- 100 MB
- 500 MB
- 1 GB
- 5 GB
- Custom

Large-file discovery is **read-only**. DiskFresh does not automatically remove anything from this section.

### 🛡️ Safety First

Disk cleaners can be dangerous when they make assumptions.

DiskFresh therefore uses a conservative approach:

- No automatic deletion
- Explicit user confirmation before cleaning
- Restricted cleaning locations
- Protection against arbitrary paths
- Re-checks files immediately before deletion
- Graceful handling of permission errors
- Reparse-point/symlink protection
- Personal folders are not treated as disposable
- System-critical files are not targeted

When Windows does not permit access, DiskFresh reports the problem rather than trying to bypass it.

### ⚡ Responsive UI

Scanning the filesystem can take time.

DiskFresh performs filesystem work away from the main UI thread so that the application remains responsive while scanning and cleaning.

Progress and cancellation are provided where appropriate.

### 📋 Reports

Cleaning results can be reviewed with information such as:

- Files scanned
- Files removed
- Space recovered
- Skipped files
- Permission errors
- Other failures

Reports can be exported for later reference.

---

## 🖥️ Interface

DiskFresh uses a modern PySide6 desktop interface with dedicated areas for:

- **Dashboard**
- **Cleaner**
- **Large Files**
- **Reports**
- **Settings**

The dashboard gives you a quick overview of your drive and the potential space that can be recovered.

---

## 🛠️ Requirements

- Windows 10/11
- Python 3.14+
- PySide6

Install PySide6 with:

```powershell
python -m pip install PySide6
```

---

## ▶️ Run

Clone or download the repository and run:

```powershell
python DiskFresh.py
```

If your file has a different name:

```powershell
python DiskFresh_fixed_enhanced_v3.py
```

---

## 🧭 How to Use

### 1. Scan

Open DiskFresh and start a scan.

DiskFresh analyzes the configured safe locations without deleting anything.

### 2. Review

Review each category and its estimated recoverable space.

### 3. Select

Select only the categories you want to clean.

### 4. Confirm

Review the cleaning operation before anything is removed.

### 5. Clean

Start the cleanup and watch the progress.

### 6. Review the Result

Check the final report to see how much space was successfully recovered.

---

## 🔐 Design Principles

DiskFresh follows a few important principles:

**Safety over aggressiveness**

Recovering another 500 MB isn't worth risking someone's documents.

**Transparency over automation**

The user should know what is going to happen before it happens.

**Read-only discovery**

Finding files and deleting files are deliberately separate operations.

**Graceful failure**

A permission error should not crash the application or cause an unsafe workaround.

**Responsive desktop software**

Long filesystem operations should never unnecessarily freeze the interface.

---

## 🧩 Technology

DiskFresh is built with:

- **Python**
- **PySide6 / Qt**
- `pathlib`
- `hashlib`
- `shutil`
- Windows Shell APIs where required
- Python standard library components wherever practical

The application is designed to remain lightweight and understandable rather than depending on a large collection of third-party utilities.

---

## 📂 Project Structure

The application is organized around responsibilities such as:

```text
DiskFresh
├── Scanner
├── Cleaner
├── Large File Scanner
├── Report Manager
├── Settings Manager
├── Worker / background processing
└── Main Window / UI
```

This separation makes it easier to extend DiskFresh without turning the UI into filesystem-management code.

---

## 🔮 Possible Future Improvements

Ideas for future versions include:

- Duplicate-file finder
- More detailed drive analytics
- Storage visualization
- Scheduled scans
- Additional browser-cache detection
- Portable executable packaging
- Improved report history
- Per-category cleaning previews
- Multi-drive dashboard
- Optional administrator-assisted cleaning for locations that genuinely require it

Future features should preserve DiskFresh's conservative safety model.

---

## 🤝 Contributing

Contributions and suggestions are welcome.

Before adding a cleaning target, ask:

> **Can we prove that this location contains disposable data and protect everything else?**

Safety should take priority over the amount of space recovered.

---

## 📜 License

MIT License

See `LICENSE` for details.

---

## 🌱 Project Status

DiskFresh is a practical desktop utility project focused on **safe Windows storage cleanup and disk-space discovery**.

It is intentionally designed to be useful without trying to become an overly aggressive "clean everything" optimizer.

---

### DiskFresh

**Clean intelligently. See clearly. Stay safe.**
