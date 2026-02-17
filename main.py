from __future__ import annotations
import sys
import os
import threading
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pydub import AudioSegment
import pydub.utils
import subprocess

def resource_path(relative: str) -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent
    return str((base / relative).resolve())

def configure_pydub_ffmpeg():
    ffmpeg_path  = resource_path(r"bin\ffmpeg.exe")
    ffprobe_path = resource_path(r"bin\ffprobe.exe")

    # Debug prints so we *know* what's happening
    print("Using ffmpeg:", ffmpeg_path, "exists =", os.path.exists(ffmpeg_path))
    print("Using ffprobe:", ffprobe_path, "exists =", os.path.exists(ffprobe_path))

    # Force PyDub to use our bundled binaries
    AudioSegment.converter = ffmpeg_path
    AudioSegment.ffprobe = ffprobe_path

    # ALSO force PyDub's internal globals (this fixes a lot of WinError 2 cases)
    pydub.utils.FFMPEG_BINARY = ffmpeg_path
    pydub.utils.FFPROBE_BINARY = ffprobe_path

    # Hard fail early if missing
    if not os.path.exists(ffmpeg_path) or not os.path.exists(ffprobe_path):
        raise FileNotFoundError(
            "Bundled ffmpeg/ffprobe not found.\n"
            f"ffmpeg: {ffmpeg_path}\n"
            f"ffprobe: {ffprobe_path}\n"
        )

configure_pydub_ffmpeg()



# Drag & drop support
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:
    HAS_DND = False
    DND_FILES = None
    TkinterDnD = None


# ----------------------------
# DFPWM1a encoder (ported from the Java you pasted)
# ----------------------------

@dataclass
class DFPWMCodec:
    newdfpwm: bool = True

    def __post_init__(self):
        self.dfpwm_old = not self.newdfpwm

        if self.newdfpwm:
            self.RESP_INC = 1
            self.RESP_DEC = 1
            self.RESP_PREC = 10
        else:
            self.RESP_INC = 7
            self.RESP_DEC = 20
            self.RESP_PREC = 8

        self.response = 0
        self.level = 0
        self.lastbit = False

    def _ctx_update(self, curbit: bool) -> None:
        target = 127 if curbit else -128

        nlevel = self.level + (
            (self.response * (target - self.level) + (1 << (self.RESP_PREC - 1)))
            >> self.RESP_PREC
        )
        if nlevel == self.level and self.level != target:
            nlevel += 1 if curbit else -1

        if curbit == self.lastbit:
            rtarget = (1 << self.RESP_PREC) - 1
            rdelta = self.RESP_INC
        else:
            rtarget = 0
            rdelta = self.RESP_DEC

        if self.dfpwm_old:
            nresponse = self.response + ((rdelta * (rtarget - self.response) + 128) >> 8)
        else:
            nresponse = self.response

        if nresponse == self.response and self.response != rtarget:
            nresponse += 1 if (curbit == self.lastbit) else -1

        if self.RESP_PREC > 8:
            min_resp = (2 << (self.RESP_PREC - 8))
            if nresponse < min_resp:
                nresponse = min_resp

        self.response = nresponse
        self.lastbit = curbit
        self.level = nlevel

    def compress_pcm_s8_to_dfpwm(self, pcm_s8: bytes) -> bytes:
        """
        pcm_s8: signed 8-bit mono PCM bytes (each byte is -128..127)
        returns: dfpwm bytes (len = len(pcm_s8)//8), truncates remainder
        """
        out = bytearray()
        n = len(pcm_s8) - (len(pcm_s8) % 8)
        idx = 0

        while idx < n:
            d = 0
            for _ in range(8):
                b = pcm_s8[idx]
                idx += 1
                inlevel = b - 256 if b > 127 else b

                curbit = (inlevel > self.level) or (inlevel == self.level and self.level == 127)
                d = (d >> 1) + (128 if curbit else 0)

                self._ctx_update(curbit)

            out.append(d & 0xFF)

        return bytes(out)


# ----------------------------
# Audio helpers
# ----------------------------

def mp3_to_pcm_s8_chunks(mp3_path: str, sample_rate: int, chunk_seconds: int) -> list[bytes]:
    """
    Decode using bundled ffmpeg directly:
    MP3 -> mono -> sample_rate -> signed 8-bit PCM (raw bytes)
    then split into chunk_seconds chunks.
    """

    mp3_path = str(Path(mp3_path).expanduser().resolve())

    ffmpeg_path = resource_path(r"bin\ffmpeg.exe")

    # ffmpeg outputs raw signed 8-bit mono PCM to stdout
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        "-i", mp3_path,
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "s8",
        "pipe:1",
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "ffmpeg failed to decode the audio.\n\n"
            f"Command:\n{cmd}\n\n"
            f"stderr:\n{e.stderr.decode('utf-8', errors='replace')}"
        )

    pcm = proc.stdout  # raw signed 8-bit PCM samples, 1 byte per sample

    chunk_size = sample_rate * chunk_seconds  # bytes per chunk
    return [pcm[i:i + chunk_size] for i in range(0, len(pcm), chunk_size)]




def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_bytes(path: Path, data: bytes) -> None:
    with path.open("wb") as f:
        f.write(data)


def normalise_drop_path(data: str) -> str:
    """
    tkinterdnd2 can provide:
      - '{C:/Path With Spaces/file.mp3}'
      - 'C:/Path/file.mp3'
      - multiple files separated by spaces
    We'll take the first file only.
    """
    s = data.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    # If multiple paths, take first
    # (Very basic split that handles braces above)
    parts = s.split()
    return parts[0]


# ----------------------------
# GUI
# ----------------------------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MP3 → DFPWM (Split to 60s)")

        self.mp3_path_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Drop an MP3 here, or click Browse.")
        self.progress_var = tk.DoubleVar(value=0)

        self.sample_rate_var = tk.IntVar(value=48000)
        self.chunk_seconds_var = tk.IntVar(value=60)

        self._build_ui()
        self._setup_dnd()

    def _build_ui(self):
        pad = 10
        frm = ttk.Frame(self.root, padding=pad)
        frm.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)

        ttk.Label(frm, text="MP3 file:").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frm, textvariable=self.mp3_path_var)
        entry.grid(row=1, column=0, sticky="ew", pady=(2, 8))

        btnrow = ttk.Frame(frm)
        btnrow.grid(row=2, column=0, sticky="ew")
        btnrow.columnconfigure(0, weight=1)

        ttk.Button(btnrow, text="Browse…", command=self.browse).grid(row=0, column=0, sticky="w")
        ttk.Button(btnrow, text="Convert + Split", command=self.convert_clicked).grid(row=0, column=1, sticky="e")

        opts = ttk.Frame(frm)
        opts.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        opts.columnconfigure(3, weight=1)

        ttk.Label(opts, text="Sample rate:").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(opts, from_=8000, to=96000, increment=1000, textvariable=self.sample_rate_var, width=8).grid(row=0, column=1, sticky="w", padx=(6, 20))

        ttk.Label(opts, text="Chunk seconds:").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(opts, from_=5, to=600, increment=5, textvariable=self.chunk_seconds_var, width=6).grid(row=0, column=3, sticky="w", padx=(6, 0))

        self.drop_zone = ttk.Label(
            frm,
            text=("Drag & drop MP3 here" if HAS_DND else "Drag & drop needs: pip install tkinterdnd2"),
            padding=20,
            relief="groove",
            anchor="center"
        )
        self.drop_zone.grid(row=4, column=0, sticky="ew", pady=(12, 8))

        self.progress = ttk.Progressbar(frm, variable=self.progress_var, maximum=100)
        self.progress.grid(row=5, column=0, sticky="ew", pady=(6, 2))

        ttk.Label(frm, textvariable=self.status_var).grid(row=6, column=0, sticky="w", pady=(6, 0))

    def _setup_dnd(self):
        if not HAS_DND:
            return
        # Register drop target
        self.drop_zone.drop_target_register(DND_FILES)
        self.drop_zone.dnd_bind("<<Drop>>", self.on_drop)

    def on_drop(self, event):
        path = normalise_drop_path(event.data)
        self.set_mp3(path)

    def browse(self):
        path = filedialog.askopenfilename(
            title="Select MP3",
            filetypes=[("Audio files", "*.mp3 *.wav *.flac *.ogg *.m4a"), ("All files", "*.*")]
        )
        if path:
            self.set_mp3(path)

    def set_mp3(self, path: str):
        path = path.strip().strip('"')
        self.mp3_path_var.set(path)
        if os.path.exists(path):
            self.status_var.set("Ready. Click Convert + Split.")
        else:
            self.status_var.set("That path doesn't exist.")

    def convert_clicked(self):
        mp3_path = self.mp3_path_var.get().strip().strip('"')
        if not mp3_path:
            messagebox.showerror("No file", "Pick an MP3 first (browse or drag & drop).")
            return
        if not os.path.exists(mp3_path):
            messagebox.showerror("File not found", "That MP3 path doesn't exist.")
            return

        # Run conversion on a thread so the GUI doesn’t freeze
        self.progress_var.set(0)
        self.status_var.set("Working…")
        threading.Thread(target=self._convert_worker, args=(mp3_path,), daemon=True).start()

    def _convert_worker(self, mp3_path: str):
        try:
            sample_rate = int(self.sample_rate_var.get())
            chunk_seconds = int(self.chunk_seconds_var.get())

            src = Path(mp3_path)
            out_dir = src.with_name(src.stem + "_chunks")
            safe_mkdir(out_dir)

            self._ui(lambda: self.status_var.set("Decoding + splitting…"))
            pcm_chunks = mp3_to_pcm_s8_chunks(str(src), sample_rate=sample_rate, chunk_seconds=chunk_seconds)

            total = len(pcm_chunks)
            codec = DFPWMCodec(newdfpwm=True)

            for i, pcm in enumerate(pcm_chunks, start=1):
                dfpwm = codec.compress_pcm_s8_to_dfpwm(pcm)
                write_bytes(out_dir / f"{i}.dfpwm", dfpwm)

                pct = (i / total) * 100 if total else 100
                self._ui(lambda p=pct, i=i, t=total: (self.progress_var.set(p), self.status_var.set(f"Encoding chunk {i}/{t}…")))

            self._ui(lambda: self.status_var.set(f"Done! Saved to: {out_dir}"))
            self._ui(lambda: self.progress_var.set(100))

        except Exception as e:
            self._ui(lambda: messagebox.showerror(
                "Conversion failed",
                "Most common cause: ffmpeg isn’t installed / not on PATH (PyDub needs it).\n\n"
                f"Details:\n{e}"
            ))
            self._ui(lambda: self.status_var.set("Failed."))

    def _ui(self, fn):
        self.root.after(0, fn)


def main():
    if HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()

    # nicer default sizing
    root.geometry("560x280")

    # Use ttk theme if available
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
