#!/usr/bin/env python3
"""
PS2 Menu Studio  -  the plug-and-play app

Nothing to compile. The app:
  1. downloads two ready-made open-source PS2 programs (once, then cached):
       - NHDDL     (pcm720/nhddl):      the on-console game menu
       - Neutrino  (rickgaiser/neutrino): the ISO loader NHDDL uses
  2. sets up your USB drive / SD card / HDD folder: Neutrino, NHDDL, and your
     games copied into CD/ and DVD/ folders with clean names
  3. creates a NEW bootable menu ISO that contains the NHDDL menu

Run:  python ps2_menu_studio.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.request
import zipfile

import ps2_menu_iso as core

NHDDL_REPO = "pcm720/nhddl"
NEUTRINO_REPO = "rickgaiser/neutrino"
MENU_ID = core.DEFAULT_ID
CD_LIMIT = 737_280_000          # ~703 MB: smaller images go in CD/, larger in DVD/


# ------------------------------------------------------------------ helpers
def cache_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "PS2MenuStudio")
    os.makedirs(d, exist_ok=True)
    return d


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def safe_name(title, limit=50):
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    t = re.sub(r"\s+", " ", t).strip(" .")
    return (t[:limit].strip(" .")) or "Game"


def read_game_id(path):
    """Game ID (e.g. SLUS_203.12) from the ISO's SYSTEM.CNF, or None."""
    S = 2048
    try:
        with open(path, "rb") as f:
            f.seek(16 * S)
            pvd = f.read(S)
            if pvd[1:6] != b"CD001":
                return None
            lba = int.from_bytes(pvd[158:162], "little")
            size = int.from_bytes(pvd[166:170], "little")
            f.seek(lba * S)
            data = f.read(size)
            pos = 0
            while pos < len(data):
                rl = data[pos]
                if rl == 0:
                    pos = (pos // S + 1) * S
                    continue
                name = data[pos + 33:pos + 33 + data[pos + 32]].decode("ascii", "ignore").upper()
                if name.startswith("SYSTEM.CNF"):
                    flba = int.from_bytes(data[pos + 2:pos + 6], "little")
                    f.seek(flba * S)
                    cnf = f.read(S).decode("ascii", "ignore")
                    m = re.search(r"BOOT2\s*=\s*cdrom0:\\?([A-Z]{4}_\d{3}\.\d{2})", cnf, re.I)
                    return m.group(1).upper() if m else None
                pos += rl
    except OSError:
        pass
    return None


# ------------------------------------------------------- component download
def github_latest(repo):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"User-Agent": "PS2MenuStudio", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def download(url, dest, progress=None):
    req = urllib.request.Request(url, headers={"User-Agent": "PS2MenuStudio"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while chunk := r.read(256 * 1024):
            out.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(got * 100 / total)
    os.replace(dest + ".part", dest)


def pick_nhddl_asset(release):
    names = {a["name"]: a for a in release.get("assets", [])}
    for n, a in names.items():
        if n.lower() == "nhddl.elf":
            return a
    for n, a in names.items():
        if n.lower().startswith("nhddl") and n.lower().endswith(".elf") and "-" not in n:
            return a
    raise RuntimeError("Couldn't find nhddl.elf in the latest NHDDL release. Assets: " + ", ".join(names))


def pick_neutrino_asset(release):
    assets = [a for a in release.get("assets", []) if "source" not in a["name"].lower()]
    for ext in (".zip", ".7z"):
        for a in assets:
            if a["name"].lower().endswith(ext):
                return a
    raise RuntimeError("Couldn't find a Neutrino archive in the latest release. Assets: "
                       + ", ".join(a["name"] for a in release.get("assets", [])))


def extract_archive(path, dest):
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            z.extractall(dest)
    elif path.lower().endswith(".7z"):
        try:
            import py7zr
        except ImportError as e:
            raise RuntimeError("This Neutrino release is a .7z archive. Install py7zr "
                               "(pip install py7zr), or extract it yourself and use "
                               "'Use my own Neutrino folder...'.") from e
        with py7zr.SevenZipFile(path) as z:
            z.extractall(dest)
    else:
        raise RuntimeError(f"Unsupported archive type: {path}")


def find_neutrino_root(folder):
    for root, _, files in os.walk(folder):
        if any(f.lower() == "neutrino.elf" for f in files):
            return root
    return None


def install_neutrino_folder(src, log=print):
    root = find_neutrino_root(src)
    if not root:
        raise RuntimeError("That folder doesn't contain neutrino.elf")
    dst = os.path.join(cache_dir(), "neutrino")
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(root, dst)
    log("Neutrino installed into the app cache.")
    return dst


def component_paths():
    c = cache_dir()
    return os.path.join(c, "nhddl.elf"), os.path.join(c, "neutrino")


def components_ready():
    elf, neu = component_paths()
    return os.path.isfile(elf) and bool(find_neutrino_root(neu) if os.path.isdir(neu) else None)


def ensure_components(update, log, progress=None):
    elf, neu = component_paths()
    if not update and components_ready():
        return elf, neu
    try:
        log("Checking the latest NHDDL release...")
        rel = github_latest(NHDDL_REPO)
        asset = pick_nhddl_asset(rel)
        log(f"Downloading {asset['name']} ({rel.get('tag_name', '?')})...")
        download(asset["browser_download_url"], elf, progress)

        log("Checking the latest Neutrino release...")
        rel = github_latest(NEUTRINO_REPO)
        asset = pick_neutrino_asset(rel)
        log(f"Downloading {asset['name']} ({rel.get('tag_name', '?')})...")
        with tempfile.TemporaryDirectory() as tmp:
            arc = os.path.join(tmp, asset["name"])
            download(asset["browser_download_url"], arc, progress)
            log("Unpacking Neutrino...")
            ex = os.path.join(tmp, "x")
            extract_archive(arc, ex)
            install_neutrino_folder(ex, log)
    except Exception as e:  # noqa: BLE001
        if components_ready():
            log(f"Couldn't update ({e}); using the copies already downloaded.")
            return elf, neu
        raise RuntimeError(f"Download failed: {e}\n\nNo internet? Use the 'Use my own files...' buttons "
                           f"to point the app at nhddl.elf and a Neutrino folder you already have.") from e
    return elf, neu


# --------------------------------------------------------------- build steps
def install_to_drive(dest, elf, neu, log):
    log(f"Installing Neutrino and NHDDL to {dest} ...")
    shutil.copytree(neu, os.path.join(dest, "neutrino"), dirs_exist_ok=True)
    shutil.copy2(elf, os.path.join(dest, "nhddl.elf"))


def stage_games(dest, games, log, progress):
    need = sum(g["size"] for g in games)
    free = shutil.disk_usage(dest).free
    if need > free:
        raise RuntimeError(f"Not enough space on the drive: need {human(need)}, free {human(free)}.")
    done = 0
    for g in games:
        sub = "CD" if g["size"] <= CD_LIMIT else "DVD"
        os.makedirs(os.path.join(dest, sub), exist_ok=True)
        out = os.path.join(dest, sub, safe_name(g["title"]) + ".iso")
        if os.path.exists(out) and os.path.getsize(out) == g["size"]:
            log(f"Skipping {g['title']} (already there)")
            done += g["size"]
            continue
        log(f"Copying {g['title']} -> {sub}/")
        with open(g["src"], "rb") as s, open(out, "wb") as d:
            while chunk := s.read(8 * 1024 * 1024):
                d.write(chunk)
                done += len(chunk)
                progress(done * 100 / max(need, 1))


def make_menu_iso(elf_path, out_path, vmode, log):
    with open(elf_path, "rb") as f:
        elf = f.read()
    cnf = f"BOOT2 = cdrom0:\\{MENU_ID};1\r\nVER = 1.00\r\nVMODE = {vmode}\r\n".encode()
    iso = core.build_iso({"SYSTEM.CNF": cnf, MENU_ID: elf}, volume_id=MENU_ID.replace(".", "_"))
    with open(out_path, "wb") as f:
        f.write(iso)
    log(f"Menu ISO written: {out_path} ({human(len(iso))})")


def run_build(opts, games, log, progress):
    elf, neu = ensure_components(opts["update"], log, progress)
    if opts["install"]:
        install_to_drive(opts["dest"], elf, neu, log)
    if opts["copy"] and games:
        stage_games(opts["dest"], games, log, progress)
    if opts["iso"]:
        make_menu_iso(elf, opts["out"], opts["vmode"], log)
    progress(100)
    log("All done.")


# ------------------------------------------------------------------------ GUI
def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog

    cfg_path = os.path.join(cache_dir(), "settings.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}

    root = tk.Tk()
    root.title("PS2 Menu Studio")
    root.geometry("860x640")
    games = []
    v = {"dest": tk.StringVar(value=cfg.get("dest", "")),
         "out": tk.StringVar(value=cfg.get("out", os.path.join(os.path.expanduser("~"), "PS2_Menu.iso"))),
         "vmode": tk.StringVar(value=cfg.get("vmode", "NTSC")),
         "install": tk.BooleanVar(value=True), "copy": tk.BooleanVar(value=True),
         "iso": tk.BooleanVar(value=True)}

    # --- step 1: games
    f1 = ttk.LabelFrame(root, text="1. Your games", padding=6)
    f1.pack(fill="both", expand=True, padx=8, pady=(8, 4))
    bar = ttk.Frame(f1)
    bar.pack(fill="x")
    tree = ttk.Treeview(f1, columns=("title", "id", "size", "src"), show="headings", height=8, selectmode="extended")
    for c, w in (("title", 260), ("id", 90), ("size", 80), ("src", 380)):
        tree.heading(c, text=c.upper())
        tree.column(c, width=w)
    tree.pack(fill="both", expand=True, pady=4)

    def refresh():
        tree.delete(*tree.get_children())
        for i, g in enumerate(games):
            tree.insert("", "end", iid=str(i), values=(g["title"], g["id"] or "?", human(g["size"]), g["src"]))

    def add(paths):
        have = {g["src"] for g in games}
        for p in paths:
            if p not in have:
                games.append({"src": p, "title": core.guess_title(p), "id": read_game_id(p),
                              "size": os.path.getsize(p)})
        refresh()

    def add_isos():
        add(filedialog.askopenfilenames(filetypes=[("ISO images", "*.iso"), ("All", "*.*")]))

    def add_folder():
        d = filedialog.askdirectory()
        if d:
            add(sorted(os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs if f.lower().endswith(".iso")))

    def rename(_=None):
        s = tree.selection()
        if s:
            g = games[int(s[0])]
            new = simpledialog.askstring("Rename", "Title shown in the menu:", initialvalue=g["title"], parent=root)
            if new:
                g["title"] = safe_name(new)
                refresh()

    def remove():
        for i in sorted((int(x) for x in tree.selection()), reverse=True):
            del games[i]
        refresh()

    for text, cmd in [("Add ISOs...", add_isos), ("Add folder...", add_folder), ("Rename...", rename), ("Remove", remove)]:
        ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
    tree.bind("<Double-1>", rename)

    # --- step 2: drive
    f2 = ttk.LabelFrame(root, text="2. Your PS2 drive (USB stick, SD card, or HDD folder)", padding=6)
    f2.pack(fill="x", padx=8, pady=4)
    ttk.Entry(f2, textvariable=v["dest"], width=70).pack(side="left", padx=2)
    ttk.Button(f2, text="Browse...", command=lambda: v["dest"].set(filedialog.askdirectory() or v["dest"].get())).pack(side="left")

    # --- step 3: menu iso
    f3 = ttk.LabelFrame(root, text="3. Menu ISO to create", padding=6)
    f3.pack(fill="x", padx=8, pady=4)
    ttk.Entry(f3, textvariable=v["out"], width=60).pack(side="left", padx=2)
    ttk.Button(f3, text="Browse...", command=lambda: v["out"].set(
        filedialog.asksaveasfilename(defaultextension=".iso", filetypes=[("ISO", "*.iso")]) or v["out"].get())).pack(side="left")
    ttk.Combobox(f3, textvariable=v["vmode"], values=["NTSC", "PAL"], width=6, state="readonly").pack(side="left", padx=6)

    # --- options + go
    f4 = ttk.Frame(root, padding=(8, 2))
    f4.pack(fill="x")
    ttk.Checkbutton(f4, text="Install Neutrino + NHDDL on the drive", variable=v["install"]).pack(side="left")
    ttk.Checkbutton(f4, text="Copy games to the drive", variable=v["copy"]).pack(side="left", padx=8)
    ttk.Checkbutton(f4, text="Create menu ISO", variable=v["iso"]).pack(side="left")

    prog = ttk.Progressbar(root)
    prog.pack(fill="x", padx=8, pady=2)
    logbox = tk.Text(root, height=8, state="disabled")
    logbox.pack(fill="both", padx=8, pady=2)

    def log(msg):
        def _do():
            logbox.configure(state="normal")
            logbox.insert("end", msg + "\n")
            logbox.see("end")
            logbox.configure(state="disabled")
        root.after(0, _do)

    def progress(p):
        root.after(0, lambda: prog.configure(value=p))

    busy = {"on": False}

    def start(update=False, only_download=False):
        if busy["on"]:
            return
        opts = {k: v[k].get() for k in v}
        opts["update"] = update
        if only_download:
            opts.update(install=False, copy=False, iso=False)
        else:
            if (opts["install"] or opts["copy"]) and not (opts["dest"] and os.path.isdir(opts["dest"])):
                return messagebox.showinfo("Pick your drive", "Choose the drive or folder in step 2 first.")
            if opts["copy"] and not games:
                return messagebox.showinfo("No games", "Add some games in step 1 (or untick 'Copy games').")
        with open(cfg_path, "w") as f:
            json.dump({k: v[k].get() for k in ("dest", "out", "vmode")}, f)

        def work():
            busy["on"] = True
            try:
                run_build(opts, list(games), log, progress)
                root.after(0, lambda: messagebox.showinfo("Done", "Finished! See the log for details."))
            except Exception as e:  # noqa: BLE001
                log(f"ERROR: {e}")
                root.after(0, lambda: messagebox.showerror("Something went wrong", str(e)))
            finally:
                busy["on"] = False
        threading.Thread(target=work, daemon=True).start()

    def own_files():
        elf = filedialog.askopenfilename(title="Pick nhddl.elf", filetypes=[("ELF", "*.elf"), ("All", "*.*")])
        if elf:
            shutil.copy2(elf, component_paths()[0])
        folder = filedialog.askdirectory(title="Pick your Neutrino folder (contains neutrino.elf)")
        if folder:
            try:
                install_neutrino_folder(folder, log)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("Neutrino", str(e))

    bot = ttk.Frame(root, padding=8)
    bot.pack(fill="x")
    ttk.Button(bot, text="Update components", command=lambda: start(update=True, only_download=True)).pack(side="left", padx=2)
    ttk.Button(bot, text="Use my own files...", command=own_files).pack(side="left", padx=2)
    ttk.Button(bot, text="BUILD EVERYTHING", command=start).pack(side="right")
    log("Ready. Components download automatically the first time you build.")
    root.mainloop()


if __name__ == "__main__":
    gui()
