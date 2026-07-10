#!/usr/bin/env python3
"""LK-222(CASIO電子ピアノ)でMIDIとWAVを同時再生するHTTPデーモン。

POST /play_song
  {"wav_filename": "...", "midi_filename": "..."}  # 用意済みMIDIファイルを使う
  {"wav_filename": "...", "abc": "X:1\\nT:...\\n..."}  # ABC記法テキストをその場でabc2midi変換して使う
  {"library_name": "kirakira_duet"}  # save_songで保存済みの曲を名前指定で再生
  {"midi_filename": "..."} / {"abc": "..."} / {"library_name": "..."}  # wav_filename省略でピアノ単独演奏
  - midi_filename/abc/library_nameはいずれか一つだけ指定する。wav_filenameは省略可
    （省略時はMIDIのみ再生。library_name指定時は対応する{name}.wavが存在すれば自動で使う）。
  - MIDI/WAVの再生元ファイル名(basenameのみ)はホワイトリスト検証する。
  - wav指定時はaplaymidiとaplayを同時に起動し、両方の終了を待って結果を返す。

POST /save_song
  {"name": "kirakira_duet", "abc": "X:1\\nT:...\\n...", "wav_filename": "song-xxxx.wav"}
  - あかねはWriteツールを持たないため、「残したい曲」を永続化するための保存専用エンドポイント。
  - name(英数字・_・-のみ)を key に、abcテキストと変換済みMIDI、(あれば)WAVのコピーをSONG_LIBRARY_DIR配下に
    {name}.abc / {name}.mid / {name}.wav としてセットで保存する。同名は上書き。

GET /songs
  - SONG_LIBRARY_DIRに保存済みの曲一覧を返す: {"songs": [{"name": "...", "has_wav": true/false}, ...]}

ABC_CACHE_DIR(/play_song の abc 変換で使う一時ファイル置き場)は7日より古いファイルを
次回のabc変換時に自動削除する(使い捨て)。残したい曲は/save_songで明示的に保存すること。

依存: 標準ライブラリのみ。alsa-utils (aplaymidi, aplay) と abcmidi (abc2midi) が別途必要。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MIDI_DIR = "/mnt/ha-config/embodied-ha/midi"
WAV_DIR = "/mnt/ha-config/embodied-ha/wav"
SONG_LIBRARY_DIR = "/mnt/ha-config/embodied-ha/song_library"  # 「残したい曲」の永続化先(ABC+MIDI+WAVをセットで保存)
MIDI_CLIENT_NAME = "CASIO USB-MIDI"  # aplaymidi -l のクライアント名。ポート番号(例:24:0)は起動ごとに変わるため名前で解決する
AUDIO_DEVICE = "plughw:CARD=Audio,DEV=0"  # USBオーディオアダプタ(iStore Audio)
ABC_CACHE_DIR = "/home/yunomin61/piano_abc_cache"
ABC_CACHE_MAX_AGE_SEC = 7 * 24 * 3600  # 使い捨てキャッシュの保持期間。残したい曲は/save_songで別途永続化する
BIND_HOST = "0.0.0.0"
BIND_PORT = 8090

_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_path(base_dir: str, filename: str) -> str:
    if not filename or not _FILENAME_RE.match(filename) or filename in {".", ".."}:
        raise ValueError(f"invalid filename: {filename!r}")
    path = os.path.join(base_dir, filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"not found: {path}")
    return path


def _cleanup_abc_cache() -> None:
    if not os.path.isdir(ABC_CACHE_DIR):
        return
    cutoff = time.time() - ABC_CACHE_MAX_AGE_SEC
    for fname in os.listdir(ABC_CACHE_DIR):
        path = os.path.join(ABC_CACHE_DIR, fname)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def _abc_to_midi(abc_text: str) -> str:
    if not abc_text.strip():
        raise ValueError("abc が空です")
    os.makedirs(ABC_CACHE_DIR, exist_ok=True)
    _cleanup_abc_cache()
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


def save_song(*, name: str, abc: str, wav_filename: str = "") -> dict:
    if not name or not _NAME_RE.match(name):
        raise ValueError(f"invalid name: {name!r}")
    if not abc.strip():
        raise ValueError("abc が空です")
    os.makedirs(SONG_LIBRARY_DIR, exist_ok=True)
    abc_path = os.path.join(SONG_LIBRARY_DIR, f"{name}.abc")
    midi_path = os.path.join(SONG_LIBRARY_DIR, f"{name}.mid")
    with open(abc_path, "w", encoding="utf-8") as f:
        f.write(abc)
    proc = subprocess.run(
        ["abc2midi", abc_path, "-o", midi_path, "-silent"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if proc.returncode != 0 or not os.path.isfile(midi_path):
        raise ValueError(f"abc2midi failed: {proc.stdout.decode(errors='replace')}")

    result = {"name": name, "abc_path": abc_path, "midi_path": midi_path, "wav_path": None}
    if wav_filename:
        src_wav = _safe_path(WAV_DIR, wav_filename)
        dst_wav = os.path.join(SONG_LIBRARY_DIR, f"{name}.wav")
        shutil.copyfile(src_wav, dst_wav)
        result["wav_path"] = dst_wav
    return result


def list_songs() -> list[dict]:
    if not os.path.isdir(SONG_LIBRARY_DIR):
        return []
    names = sorted({
        os.path.splitext(fname)[0]
        for fname in os.listdir(SONG_LIBRARY_DIR)
        if fname.endswith(".abc")
    })
    return [
        {"name": name, "has_wav": os.path.isfile(os.path.join(SONG_LIBRARY_DIR, f"{name}.wav"))}
        for name in names
    ]


def _resolve_midi_port() -> str:
    proc = subprocess.run(["aplaymidi", "-l"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in proc.stdout.decode(errors="replace").splitlines():
        if MIDI_CLIENT_NAME in line:
            return line.split()[0]
    raise RuntimeError(f"MIDI port not found for client {MIDI_CLIENT_NAME!r}: {proc.stdout.decode(errors='replace')}")


def play_song(
    *,
    wav_filename: str = "",
    midi_filename: str = "",
    abc: str = "",
    library_name: str = "",
    midi_delay_sec: float = 0.0,
) -> dict:
    modes_given = sum(bool(x) for x in (midi_filename, abc, library_name))
    if modes_given != 1:
        raise ValueError("midi_filename, abc, library_name のいずれか一つだけ指定してください")

    if library_name:
        if not _NAME_RE.match(library_name):
            raise ValueError(f"invalid library_name: {library_name!r}")
        midi_path = os.path.join(SONG_LIBRARY_DIR, f"{library_name}.mid")
        if not os.path.isfile(midi_path):
            raise FileNotFoundError(f"not found: {midi_path}")
        if wav_filename:
            wav_path = _safe_path(WAV_DIR, wav_filename)
        else:
            candidate = os.path.join(SONG_LIBRARY_DIR, f"{library_name}.wav")
            wav_path = candidate if os.path.isfile(candidate) else ""
    else:
        midi_path = _safe_path(MIDI_DIR, midi_filename) if midi_filename else _abc_to_midi(abc)
        wav_path = _safe_path(WAV_DIR, wav_filename) if wav_filename else ""

    midi_port = _resolve_midi_port()

    # VOICEVOX Songの歌声WAVは冒頭に無音パディングがあり、MIDIより聴感上の発音が遅れる。
    # midi_delay_secでMIDI側の開始を後ろにずらして聴感上の頭出しを揃える。wav省略時はピアノ単独演奏。
    wav_proc = None
    if wav_path:
        wav_proc = subprocess.Popen(
            ["aplay", "-D", AUDIO_DEVICE, wav_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        if midi_delay_sec > 0:
            time.sleep(midi_delay_sec)
    midi_proc = subprocess.Popen(
        ["aplaymidi", "-p", midi_port, midi_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    midi_out, _ = midi_proc.communicate()
    result = {"midi": {"returncode": midi_proc.returncode, "output": midi_out.decode(errors="replace")}}
    if wav_proc is not None:
        wav_out, _ = wav_proc.communicate()
        result["wav"] = {"returncode": wav_proc.returncode, "output": wav_out.decode(errors="replace")}
    else:
        result["wav"] = None
    return result


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/play_song", "/save_song"}:
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
            if self.path == "/play_song":
                wav_filename = str(data.get("wav_filename") or "")
                midi_filename = str(data.get("midi_filename") or "")
                abc = str(data.get("abc") or "")
                library_name = str(data.get("library_name") or "")
                midi_delay_sec = float(data.get("midi_delay_sec") or 0.0)
                result = play_song(
                    wav_filename=wav_filename,
                    midi_filename=midi_filename,
                    abc=abc,
                    library_name=library_name,
                    midi_delay_sec=midi_delay_sec,
                )
                ok = result["midi"]["returncode"] == 0 and (result["wav"] is None or result["wav"]["returncode"] == 0)
                self._send_json(200 if ok else 500, {"status": "ok" if ok else "error", **result})
            else:
                name = str(data.get("name") or "")
                abc = str(data.get("abc") or "")
                wav_filename = str(data.get("wav_filename") or "")
                result = save_song(name=name, abc=abc, wav_filename=wav_filename)
                self._send_json(200, {"status": "ok", **result})
        except (ValueError, FileNotFoundError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/songs":
            self._send_json(404, {"error": "not found"})
            return
        try:
            songs = list_songs()
            self._send_json(200, {"songs": songs})
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
