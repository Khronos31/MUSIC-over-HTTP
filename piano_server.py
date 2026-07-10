#!/usr/bin/env python3
"""LK-222(CASIO電子ピアノ)でMIDIとWAVを同時再生するHTTPデーモン。

POST /play_song
  {"wav_filename": "...", "midi_filename": "..."}  # 用意済みMIDIファイルを使う
  {"wav_filename": "...", "abc": "X:1\\nT:...\\n..."}  # ABC記法テキストをその場でabc2midi変換して使う
  - wav_filenameは必須。midi_filename/abcはどちらか一方を指定する。
  - MIDI/WAVの再生元ファイル名(basenameのみ)はホワイトリスト検証する。
  - aplaymidi と aplay を同時に起動し、両方の終了を待って結果を返す。

依存: 標準ライブラリのみ。alsa-utils (aplaymidi, aplay) と abcmidi (abc2midi) が別途必要。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MIDI_DIR = "/mnt/ha-config/embodied-ha/midi"
WAV_DIR = "/mnt/ha-config/embodied-ha/wav"
MIDI_CLIENT_NAME = "CASIO USB-MIDI"  # aplaymidi -l のクライアント名。ポート番号(例:24:0)は起動ごとに変わるため名前で解決する
AUDIO_DEVICE = "plughw:CARD=Audio,DEV=0"  # USBオーディオアダプタ(iStore Audio)
ABC_CACHE_DIR = "/home/yunomin61/piano_abc_cache"
BIND_HOST = "0.0.0.0"
BIND_PORT = 8090

_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_path(base_dir: str, filename: str) -> str:
    if not filename or not _FILENAME_RE.match(filename) or filename in {".", ".."}:
        raise ValueError(f"invalid filename: {filename!r}")
    path = os.path.join(base_dir, filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"not found: {path}")
    return path


def _abc_to_midi(abc_text: str) -> str:
    if not abc_text.strip():
        raise ValueError("abc が空です")
    os.makedirs(ABC_CACHE_DIR, exist_ok=True)
    stem = uuid.uuid4().hex
    abc_path = os.path.join(ABC_CACHE_DIR, f"{stem}.abc")
    midi_path = os.path.join(ABC_CACHE_DIR, f"{stem}.mid")
    with open(abc_path, "w", encoding="utf-8") as f:
        f.write(abc_text)
    proc = subprocess.run(
        ["abc2midi", abc_path, "-o", midi_path, "-silent"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if proc.returncode != 0 or not os.path.isfile(midi_path):
        raise ValueError(f"abc2midi failed: {proc.stdout.decode(errors='replace')}")
    return midi_path


def _resolve_midi_port() -> str:
    proc = subprocess.run(["aplaymidi", "-l"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in proc.stdout.decode(errors="replace").splitlines():
        if MIDI_CLIENT_NAME in line:
            return line.split()[0]
    raise RuntimeError(f"MIDI port not found for client {MIDI_CLIENT_NAME!r}: {proc.stdout.decode(errors='replace')}")


def play_song(*, wav_filename: str, midi_filename: str = "", abc: str = "") -> dict:
    if bool(midi_filename) == bool(abc):
        raise ValueError("midi_filename と abc はどちらか一方だけ指定してください")
    wav_path = _safe_path(WAV_DIR, wav_filename)
    midi_path = _safe_path(MIDI_DIR, midi_filename) if midi_filename else _abc_to_midi(abc)
    midi_port = _resolve_midi_port()

    midi_proc = subprocess.Popen(
        ["aplaymidi", "-p", midi_port, midi_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    wav_proc = subprocess.Popen(
        ["aplay", "-D", AUDIO_DEVICE, wav_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    midi_out, _ = midi_proc.communicate()
    wav_out, _ = wav_proc.communicate()

    return {
        "midi": {"returncode": midi_proc.returncode, "output": midi_out.decode(errors="replace")},
        "wav": {"returncode": wav_proc.returncode, "output": wav_out.decode(errors="replace")},
    }


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/play_song":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
            wav_filename = str(data.get("wav_filename") or "")
            midi_filename = str(data.get("midi_filename") or "")
            abc = str(data.get("abc") or "")
            result = play_song(wav_filename=wav_filename, midi_filename=midi_filename, abc=abc)
            ok = result["midi"]["returncode"] == 0 and result["wav"]["returncode"] == 0
            self._send_json(200 if ok else 500, {"status": "ok" if ok else "error", **result})
        except (ValueError, FileNotFoundError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": str(exc)})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print(f"[piano_server] {self.address_string()} - {fmt % args}")


def main() -> None:
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    print(f"[piano_server] listening on {BIND_HOST}:{BIND_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
