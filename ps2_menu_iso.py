#!/usr/bin/env python3
"""
PS2 Menu ISO Builder  (standalone, standard library only)

Takes a list of PS2 game ISOs and produces ONE new bootable ISO that shows a
menu on the PS2 and launches the selected game through Neutrino.

How it works
  1. You build menu_elf/main.c once (see README.md) -> MENU.ELF.
  2. This tool writes your game list into a reserved data block inside that ELF.
  3. It then packs the patched ELF + SYSTEM.CNF into a new ISO9660 image.

The game ISOs themselves are NOT stored inside the menu ISO: they stay on your
USB drive / PS2 HDD / network share, and the menu hands them to Neutrino.

GUI:  python ps2_menu_iso.py
CLI:  python ps2_menu_iso.py build --elf MENU.ELF --out menu.iso game1.iso game2.iso
"""
import argparse
import datetime
import json
import os
import re
import shlex
import struct
import sys
import unicodedata

SECTOR = 2048
DATA_SIZE = 16384                      # must match DATA_SIZE in menu_elf/main.c
MAGIC = b"PS2MENUDATA1\n"              # must match the initializer in main.c
DEFAULT_ID = "HMNU_000.01"             # fake game ID (also the ELF's name on the ISO)
DEFAULT_NEUTRINO = "mc0:/neutrino/neutrino.elf"

# backend -> (neutrino flags, -dvd scheme). Check your Neutrino version's README.
BACKENDS = {
    "usb":    (["-bsd=usb"], "mass:"),
    "mx4sio": (["-bsd=mx4sio"], "mass:"),
    "ata":    (["-bsd=ata"], "mass:"),                       # exFAT/FAT32 on internal HDD
    "hdl":    (["-bsd=ata", "-bsdfs=hdl"], "hdl:"),          # HDLoader partitions
    "udpfs":  (["-bsd=udpfs"], "udpfs:"),                    # network (UDPFS server)
}


# ------------------------------------------------------------------ menu data
def find_menu_elf():
    """Look for a MENU.ELF shipped with the app (installer puts it next to the .exe)."""
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    dirs = [getattr(sys, "_MEIPASS", here), here, os.path.join(here, "menu_elf")]
    for d in dirs:
        for name in ("MENU.ELF", "menu.elf"):
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    return ""


def ascii_clean(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[|\r\n]", " ", s).strip()


def game_args(backend, path, extra=""):
    flags, scheme = BACKENDS[backend]
    path = path.replace("\\", "/").lstrip("/")
    if backend == "hdl":
        path = os.path.basename(path)
    return flags + [f"-dvd={scheme}{path}"] + (shlex.split(extra) if extra else [])


def build_config(menu_title, neutrino_path, games):
    lines = [f"T|{ascii_clean(menu_title)}", f"N|{ascii_clean(neutrino_path)}"]
    for g in games:
        args = game_args(g["backend"], g["path"], g.get("extra", ""))
        lines.append("G|" + "|".join([ascii_clean(g["title"])] + [ascii_clean(a) for a in args]))
    return "\n".join(lines) + "\n"


def patch_elf(elf, config):
    i = elf.find(MAGIC)
    if i < 0:
        raise ValueError("Data block not found: was this ELF built from menu_elf/main.c?")
    if elf.find(MAGIC, i + 1) != -1:
        raise ValueError("Data block marker found twice; rebuild the ELF from the provided main.c")
    if i + DATA_SIZE > len(elf):
        raise ValueError("ELF is truncated or DATA_SIZE doesn't match main.c")
    data = MAGIC + config.encode("ascii")
    if len(data) >= DATA_SIZE:
        raise ValueError(f"Menu too large ({len(data)} bytes, max {DATA_SIZE - 1}). Remove some games.")
    return elf[:i] + data.ljust(DATA_SIZE, b"\0") + elf[i + DATA_SIZE:]


# ---------------------------------------------------------------- ISO9660 writer
def _both16(v):
    return struct.pack("<H", v) + struct.pack(">H", v)


def _both32(v):
    return struct.pack("<I", v) + struct.pack(">I", v)


def _dir_record(lba, size, name, is_dir, now):
    length = 33 + len(name)
    length += length % 2
    rec = bytearray([length, 0])
    rec += _both32(lba) + _both32(size)
    rec += bytes([now.year - 1900, now.month, now.day, now.hour, now.minute, now.second, 0])
    rec += bytes([2 if is_dir else 0, 0, 0])
    rec += _both16(1)
    rec.append(len(name))
    rec += name
    rec += b"\0" * (length - len(rec))
    return bytes(rec)


def _path_entry(lba, endian):
    return struct.pack(endian + "BBIH", 1, 0, lba, 1) + b"\x00\x00"   # id + pad byte


def build_iso(files, volume_id="PS2MENU"):
    """files: {'NAME.EXT': bytes}. Returns a complete ISO9660 image (2048-byte sectors)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    PVD, TERM, PT_L, PT_M, ROOT = 16, 17, 18, 19, 20
    nxt, extents = 21, {}
    for name in sorted(files):
        extents[name] = nxt
        nxt += max(1, -(-len(files[name]) // SECTOR))
    total = nxt

    root = _dir_record(ROOT, SECTOR, b"\x00", True, now) + _dir_record(ROOT, SECTOR, b"\x01", True, now)
    for name in sorted(files):
        root += _dir_record(extents[name], len(files[name]), name.encode() + b";1", False, now)
    if len(root) > SECTOR:
        raise ValueError("Too many files for a single root directory sector")

    def a(text, n):
        return text.encode("ascii")[:n].ljust(n, b" ")

    stamp = now.strftime("%Y%m%d%H%M%S").encode() + b"00\x00"
    none = b"0" * 16 + b"\x00"
    pvd = bytearray(SECTOR)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[8:40] = a("PLAYSTATION", 32)
    pvd[40:72] = a(volume_id, 32)
    pvd[80:88] = _both32(total)
    pvd[120:124] = _both16(1)
    pvd[124:128] = _both16(1)
    pvd[128:132] = _both16(SECTOR)
    pvd[132:140] = _both32(10)
    pvd[140:144] = struct.pack("<I", PT_L)
    pvd[148:152] = struct.pack(">I", PT_M)
    pvd[156:190] = _dir_record(ROOT, SECTOR, b"\x00", True, now)
    pvd[190:813] = b" " * (813 - 190)
    pvd[813:830], pvd[830:847], pvd[847:864], pvd[864:881] = stamp, stamp, none, stamp
    pvd[881] = 1

    img = bytearray(total * SECTOR)
    img[PVD * SECTOR:(PVD + 1) * SECTOR] = pvd
    img[TERM * SECTOR:TERM * SECTOR + 7] = b"\xffCD001\x01"
    img[PT_L * SECTOR:PT_L * SECTOR + 10] = _path_entry(ROOT, "<")
    img[PT_M * SECTOR:PT_M * SECTOR + 10] = _path_entry(ROOT, ">")
    img[ROOT * SECTOR:ROOT * SECTOR + len(root)] = root
    for name, lba in extents.items():
        img[lba * SECTOR:lba * SECTOR + len(files[name])] = files[name]
    return bytes(img)


def make_menu_iso(elf_path, out_path, menu_title, neutrino_path, games, game_id=DEFAULT_ID, vmode="NTSC"):
    with open(elf_path, "rb") as f:
        elf = patch_elf(f.read(), build_config(menu_title, neutrino_path, games))
    cnf = (f"BOOT2 = cdrom0:\\{game_id};1\r\nVER = 1.00\r\nVMODE = {vmode}\r\n").encode()
    iso = build_iso({"SYSTEM.CNF": cnf, game_id: elf}, volume_id=game_id.replace(".", "_"))
    with open(out_path, "wb") as f:
        f.write(iso)
    return len(iso)


def guess_title(path):
    name = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"^[A-Z]{4}_\d{3}\.\d{2}\.", "", name)
    return ascii_clean(name.replace("_", " "))


# ------------------------------------------------------------------------- CLI
def cli(argv):
    p = argparse.ArgumentParser(description="Build a PS2 menu ISO")
    p.add_argument("command", choices=["build"])
    p.add_argument("isos", nargs="*", help="game ISO files (names/paths as they'll be on the device)")
    p.add_argument("--elf", default=find_menu_elf(), help="MENU.ELF built from menu_elf/main.c (default: bundled)")
    p.add_argument("--out", default="menu.iso")
    p.add_argument("--title", default="My PS2 Games")
    p.add_argument("--backend", default="usb", choices=sorted(BACKENDS))
    p.add_argument("--prefix", default="", help="folder on the device that holds the ISOs, e.g. DVD/")
    p.add_argument("--neutrino", default=DEFAULT_NEUTRINO)
    p.add_argument("--id", default=DEFAULT_ID, help="fake game ID for the menu ISO")
    p.add_argument("--vmode", default="NTSC", choices=["NTSC", "PAL"])
    a = p.parse_intermixed_args(argv)
    games = [{"title": guess_title(i), "backend": a.backend,
              "path": a.prefix + os.path.basename(i), "extra": ""} for i in a.isos]
    size = make_menu_iso(a.elf, a.out, a.title, a.neutrino, games, a.id, a.vmode)
    print(f"Wrote {a.out} ({size} bytes) with {len(games)} game(s).")


# ------------------------------------------------------------------------- GUI
def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("PS2 Menu ISO Builder")
    root.geometry("980x600")
    games = []
    v = {k: tk.StringVar(value=d) for k, d in [
        ("title", "My PS2 Games"), ("neutrino", DEFAULT_NEUTRINO), ("elf", find_menu_elf()),
        ("out", "menu.iso"), ("backend", "usb"), ("prefix", ""), ("id", DEFAULT_ID), ("vmode", "NTSC")]}

    top = ttk.Frame(root, padding=8)
    top.pack(fill="x")

    def row(r, label, key, browse=None, values=None):
        ttk.Label(top, text=label).grid(row=r, column=0, sticky="w", pady=2)
        w = (ttk.Combobox(top, textvariable=v[key], values=values, width=12, state="readonly")
             if values else ttk.Entry(top, textvariable=v[key], width=60))
        w.grid(row=r, column=1, sticky="w", padx=6)
        if browse:
            ttk.Button(top, text="Browse...", command=browse).grid(row=r, column=2, sticky="w")

    def pick_elf():
        f = filedialog.askopenfilename(title="MENU.ELF", filetypes=[("ELF", "*.elf *.ELF"), ("All", "*.*")])
        if f:
            v["elf"].set(f)

    def pick_out():
        f = filedialog.asksaveasfilename(defaultextension=".iso", filetypes=[("ISO", "*.iso")])
        if f:
            v["out"].set(f)

    row(0, "Menu title", "title")
    row(1, "MENU.ELF (built from main.c)", "elf", pick_elf)
    row(2, "Neutrino path on PS2", "neutrino")
    row(3, "Output ISO", "out", pick_out)
    row(4, "Default storage", "backend", values=sorted(BACKENDS))
    row(5, "Device folder for ISOs", "prefix")
    row(6, "Menu ISO game ID", "id")
    row(7, "Video mode", "vmode", values=["NTSC", "PAL"])

    tree = ttk.Treeview(root, columns=("title", "backend", "path", "extra"), show="headings", selectmode="extended")
    for c, w in (("title", 280), ("backend", 80), ("path", 400), ("extra", 160)):
        tree.heading(c, text=c.upper())
        tree.column(c, width=w)
    tree.pack(fill="both", expand=True, padx=8)

    def refresh():
        tree.delete(*tree.get_children())
        for i, g in enumerate(games):
            tree.insert("", "end", iid=str(i), values=(g["title"], g["backend"], g["path"], g["extra"]))

    def sel():
        return sorted(int(i) for i in tree.selection())

    def add_paths(paths):
        for p in paths:
            games.append({"title": guess_title(p), "backend": v["backend"].get(),
                          "path": v["prefix"].get() + os.path.basename(p), "extra": ""})
        refresh()

    def add_isos():
        add_paths(filedialog.askopenfilenames(filetypes=[("ISO", "*.iso"), ("All", "*.*")]))

    def add_folder():
        d = filedialog.askdirectory()
        if d:
            add_paths(sorted(os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs if f.lower().endswith(".iso")))

    def edit(_=None):
        s = sel()
        if not s:
            return
        g = games[s[0]]
        dlg = tk.Toplevel(root)
        dlg.title("Edit game")
        dlg.transient(root)
        dlg.grab_set()
        fields = {}
        for r, (k, label) in enumerate([("title", "Title"), ("backend", "Storage"),
                                        ("path", "Path on device"), ("extra", "Extra Neutrino args")]):
            ttk.Label(dlg, text=label).grid(row=r, column=0, sticky="w", padx=6, pady=3)
            var = tk.StringVar(value=g[k])
            if k == "backend":
                ttk.Combobox(dlg, textvariable=var, values=sorted(BACKENDS), state="readonly").grid(row=r, column=1, padx=6)
            else:
                ttk.Entry(dlg, textvariable=var, width=55).grid(row=r, column=1, padx=6)
            fields[k] = var

        def ok():
            for k, var in fields.items():
                g[k] = var.get()
            dlg.destroy()
            refresh()
        ttk.Button(dlg, text="OK", command=ok).grid(row=4, column=1, sticky="e", padx=6, pady=6)

    def move(d):
        s = sel()
        order = s if d < 0 else s[::-1]
        for i in order:
            j = i + d
            if 0 <= j < len(games) and j not in s:
                games[i], games[j] = games[j], games[i]
        refresh()
        for i in s:
            tree.selection_add(str(min(max(i + d, 0), len(games) - 1)))

    def remove():
        for i in reversed(sel()):
            del games[i]
        refresh()

    def save():
        f = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Project", "*.json")])
        if f:
            with open(f, "w") as fh:
                json.dump({"settings": {k: x.get() for k, x in v.items()}, "games": games}, fh, indent=2)

    def load():
        f = filedialog.askopenfilename(filetypes=[("Project", "*.json")])
        if f:
            with open(f) as fh:
                data = json.load(fh)
            for k, val in data["settings"].items():
                if k in v:
                    v[k].set(val)
            games[:] = data["games"]
            refresh()

    def build():
        if not games:
            return messagebox.showinfo("Empty", "Add some games first.")
        if not v["elf"].get():
            return messagebox.showinfo("MENU.ELF", "MENU.ELF wasn't found next to the app. Browse to the one you built from main.c (see README).")
        try:
            size = make_menu_iso(v["elf"].get(), v["out"].get(), v["title"].get(), v["neutrino"].get(),
                                 games, v["id"].get(), v["vmode"].get())
            messagebox.showinfo("Done", f"Wrote {v['out'].get()} ({size // 1024} KB), {len(games)} game(s).")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Build failed", str(e))

    bar = ttk.Frame(root, padding=8)
    bar.pack(fill="x")
    for text, cmd in [("Add ISOs...", add_isos), ("Add folder...", add_folder), ("Edit...", edit),
                      ("Up", lambda: move(-1)), ("Down", lambda: move(1)), ("Remove", remove),
                      ("Save project", save), ("Load project", load)]:
        ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
    ttk.Button(bar, text="Build menu ISO", command=build).pack(side="right")
    tree.bind("<Double-1>", edit)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli(sys.argv[1:])
    else:
        gui()
