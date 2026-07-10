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
  - 検証だけ同期で行い、再生はバックグラウンドで開始して即座に {"status": "started"} を返す
    （長い曲でHTTPクライアントがタイムアウトしないため）。結果は GET /status で確認する。
  - 同時再生は1つまで。再生中に新しいリクエストが来たら 409 (already_playing) を返す。

GET /status
  - 現在の再生状態と直近の結果を返す:
    {"playing": true/false, "started_at": epoch, "finished_at": epoch|null,
     "summary": {"midi":bool,"wav":bool,"miku":bool,...}, "result": {...}|null}

POST /save_song
  {"name": "kirakira_duet", "abc": "X:1\\nT:...\\n...", "wav_filename": "song-xxxx.wav",
   "miku_notes": [...], "miku_bpm": 120}
  - あかねはWriteツールを持たないため、「残したい曲」を永続化するための保存専用エンドポイント。
  - name(英数字・_・-のみ)を key に、abcテキストと変換済みMIDI、(あれば)WAVのコピー、(あれば)ミクのパートを
    SONG_LIBRARY_DIR配下に {name}.abc / {name}.mid / {name}.wav / {name}.miku.json としてセットで保存する。同名は上書き。
  - library_name指定の/play_songは、{name}.miku.jsonが存在すればミクのパートも自動で一緒に演奏する。

GET /songs
  - SONG_LIBRARY_DIRに保存済みの曲一覧を返す: {"songs": [{"name": "...", "has_wav": true/false}, ...]}

miku_notes(任意, /play_songの追加パラメータ)
  {"miku_notes": [{"pitch": "C4", "duration": "quarter", "lyric": "き"}, ...], "miku_bpm": 120}
  - NSX-39(ポケット・ミク)に歌詞SysExを流し込み、Note On/Offで1音節ずつ歌わせる。
  - 形式はembodied-ha側のrecordツールと同一(pitch=音名/rest, duration=whole..sixteenth, lyric=ひらがな1音節)。
  - abc/midi_filename/wav_filenameと併用可。ミク単独(miku_notesのみ)でもよい。最大64音節。

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
import threading
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

# --- NSX-39 (ポケット・ミク) ---
MIKU_DEVICE_NAME = "NSX-39"  # amidi -l の名前。カード番号は接続順で変わるため名前で解決する
MIKU_MAX_SYLLABLES = 64  # 歌詞SysExの1スロット上限
_PITCH_RE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d+)$")
_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_DURATION_BEATS = {"whole": 4.0, "half": 2.0, "quarter": 1.0, "eighth": 0.5, "sixteenth": 0.25}

# ひらがな→NSX-39音素コード (Yamaha公式 nsx39.js 由来、2026-06-30実機検証済み)
_MIKU_PHONEMES = {
    "あ": 0x00, "い": 0x01, "う": 0x02, "え": 0x03, "お": 0x04,
    "か": 0x05, "き": 0x06, "く": 0x07, "け": 0x08, "こ": 0x09,
    "が": 0x0A, "ぎ": 0x0B, "ぐ": 0x0C, "げ": 0x0D, "ご": 0x0E,
    "さ": 0x15, "し": 0x20, "す": 0x17, "せ": 0x18, "そ": 0x19,
    "ざ": 0x1A, "じ": 0x25, "ず": 0x1C, "ぜ": 0x1D, "ぞ": 0x1E,
    "た": 0x29, "ち": 0x36, "つ": 0x3C, "て": 0x2C, "と": 0x2D,
    "だ": 0x2E, "で": 0x31, "ど": 0x32, "づ": 0x1C,
    "な": 0x3F, "に": 0x40, "ぬ": 0x41, "ね": 0x42, "の": 0x43,
    "は": 0x47, "ひ": 0x48, "ふ": 0x49, "へ": 0x4A, "ほ": 0x4B,
    "ば": 0x4C, "び": 0x4D, "ぶ": 0x4E, "べ": 0x4F, "ぼ": 0x50,
    "ぱ": 0x51, "ぴ": 0x52, "ぷ": 0x53, "ぺ": 0x54, "ぽ": 0x55,
    "ま": 0x64, "み": 0x65, "む": 0x66, "め": 0x67, "も": 0x68,
    "や": 0x6C, "ゆ": 0x6D, "よ": 0x6E,
    "ら": 0x6F, "り": 0x70, "る": 0x71, "れ": 0x72, "ろ": 0x73,
    "わ": 0x77, "を": 0x7A, "ん": 0x7B,
    "きゃ": 0x0F, "きゅ": 0x10, "きょ": 0x11,
    "ぎゃ": 0x12, "ぎゅ": 0x13, "ぎょ": 0x14,
    "しゃ": 0x1F, "しゅ": 0x21, "しょ": 0x23,
    "じゃ": 0x24, "じゅ": 0x26, "じょ": 0x28,
    "ちゃ": 0x35, "ちゅ": 0x37, "ちょ": 0x39,
    "にゃ": 0x44, "にゅ": 0x45, "にょ": 0x46,
    "ひゃ": 0x56, "ひゅ": 0x57, "ひょ": 0x58,
    "びゃ": 0x59, "びゅ": 0x5A, "びょ": 0x5B,
    "ぴゃ": 0x5C, "ぴゅ": 0x5D, "ぴょ": 0x5E,
    "ふぁ": 0x5F, "ふぃ": 0x60, "ふぇ": 0x62,
    "みゃ": 0x69, "みゅ": 0x6A, "みょ": 0x6B,
    "りゃ": 0x74, "りゅ": 0x75, "りょ": 0x76,
}


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


def save_song(*, name: str, abc: str, wav_filename: str = "", miku_notes: list | None = None, miku_bpm: float = 100.0) -> dict:
    if not name or not _NAME_RE.match(name):
        raise ValueError(f"invalid name: {name!r}")
    if not abc.strip():
        raise ValueError("abc が空です")
    if miku_notes:
        _convert_miku_notes(miku_notes, miku_bpm)  # 保存前に形式検証だけ行う(デバイス不要)
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

    result = {"name": name, "abc_path": abc_path, "midi_path": midi_path, "wav_path": None, "miku_path": None}
    if wav_filename:
        src_wav = _safe_path(WAV_DIR, wav_filename)
        dst_wav = os.path.join(SONG_LIBRARY_DIR, f"{name}.wav")
        shutil.copyfile(src_wav, dst_wav)
        result["wav_path"] = dst_wav
    if miku_notes:
        miku_path = os.path.join(SONG_LIBRARY_DIR, f"{name}.miku.json")
        with open(miku_path, "w", encoding="utf-8") as f:
            json.dump({"miku_bpm": miku_bpm, "miku_notes": miku_notes}, f, ensure_ascii=False, indent=1)
        result["miku_path"] = miku_path
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
        {
            "name": name,
            "has_wav": os.path.isfile(os.path.join(SONG_LIBRARY_DIR, f"{name}.wav")),
            "has_miku": os.path.isfile(os.path.join(SONG_LIBRARY_DIR, f"{name}.miku.json")),
        }
        for name in names
    ]


def _resolve_midi_port() -> str:
    proc = subprocess.run(["aplaymidi", "-l"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in proc.stdout.decode(errors="replace").splitlines():
        if MIDI_CLIENT_NAME in line:
            return line.split()[0]
    raise RuntimeError(f"MIDI port not found for client {MIDI_CLIENT_NAME!r}: {proc.stdout.decode(errors='replace')}")


def _resolve_miku_rawmidi() -> str:
    proc = subprocess.run(["amidi", "-l"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = proc.stdout.decode(errors="replace")
    for line in output.splitlines():
        if MIKU_DEVICE_NAME in line:
            match = re.search(r"hw:(\d+),(\d+)", line)
            if match:
                return f"/dev/snd/midiC{match.group(1)}D{match.group(2)}"
    raise RuntimeError(f"raw MIDI device not found for {MIKU_DEVICE_NAME!r} (電源が入っているか確認): {output}")


def _parse_pitch(pitch: str) -> int | None:
    value = str(pitch or "").strip()
    if value.lower() == "rest":
        return None
    match = _PITCH_RE.match(value)
    if not match:
        raise ValueError(f"invalid pitch: {pitch!r}")
    name, accidental, octave = match.groups()
    semitone = _SEMITONES[name.upper()] + (1 if accidental == "#" else -1 if accidental == "b" else 0)
    midi = (int(octave) + 1) * 12 + semitone
    if not 0 <= midi <= 127:
        raise ValueError(f"pitch out of MIDI range: {pitch!r}")
    return midi


def _convert_miku_notes(notes: list, bpm: float) -> tuple[list[int], list]:
    """notesを検証して(音素コード列, [(note|None, 秒数)])に変換する。デバイスには触れない。"""
    if not isinstance(notes, list) or not notes:
        raise ValueError("miku_notes must be a non-empty array")
    if not isinstance(bpm, (int, float)) or bpm <= 0:
        raise ValueError("miku_bpm must be positive")
    phonemes: list[int] = []
    sequence: list[tuple[int | None, float]] = []
    for index, item in enumerate(notes):
        if not isinstance(item, dict):
            raise ValueError(f"miku_notes[{index}] must be an object")
        duration_key = str(item.get("duration") or "").strip().lower()
        if duration_key not in _DURATION_BEATS:
            raise ValueError(f"miku_notes[{index}].duration invalid: {item.get('duration')!r}")
        seconds = (60.0 / float(bpm)) * _DURATION_BEATS[duration_key]
        note = _parse_pitch(str(item.get("pitch") or ""))
        if note is None:
            sequence.append((None, seconds))
            continue
        lyric = str(item.get("lyric") or "").strip()
        if lyric not in _MIKU_PHONEMES:
            raise ValueError(f"miku_notes[{index}].lyric が音素表にありません: {lyric!r}")
        phonemes.append(_MIKU_PHONEMES[lyric])
        sequence.append((note, seconds))
    if len(phonemes) > MIKU_MAX_SYLLABLES:
        raise ValueError(f"歌詞が長すぎます({len(phonemes)}音節): 最大{MIKU_MAX_SYLLABLES}音節")
    return phonemes, sequence


def _prepare_miku(notes: list, bpm: float) -> tuple[str, bytes, list]:
    """検証と変換をすべて行い、(デバイスパス, 歌詞SysEx, [(note|None, 秒数)]) を返す。音は出さない。"""
    phonemes, sequence = _convert_miku_notes(notes, bpm)
    device = _resolve_miku_rawmidi()
    sysex = bytes([0xF0, 0x43, 0x79, 0x09, 0x11, 0x0A, 0x00] + phonemes + [0xF7])
    return device, sysex, sequence


def _play_miku_notes(f, sequence: list) -> dict:
    played = 0
    for note, seconds in sequence:
        if note is None:
            time.sleep(seconds)
            continue
        f.write(bytes([0x90, note, 100]))
        time.sleep(seconds * 0.85)
        f.write(bytes([0x80, note, 0]))
        time.sleep(seconds * 0.15)
        played += 1
    return {"notes_played": played}


def prepare_playback(
    *,
    wav_filename: str = "",
    midi_filename: str = "",
    abc: str = "",
    library_name: str = "",
    midi_delay_sec: float = 0.0,
    miku_notes: list | None = None,
    miku_bpm: float = 100.0,
) -> dict:
    """検証・変換をすべて行い、音を一切出さずに再生計画を返す。失敗はここで例外になる。"""
    modes_given = sum(bool(x) for x in (midi_filename, abc, library_name))
    if modes_given > 1:
        raise ValueError("midi_filename, abc, library_name は同時に一つまでです")
    if modes_given == 0 and not miku_notes:
        raise ValueError("midi_filename / abc / library_name / miku_notes のいずれかを指定してください")

    midi_path = ""
    wav_path = ""
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
        if not miku_notes:
            miku_candidate = os.path.join(SONG_LIBRARY_DIR, f"{library_name}.miku.json")
            if os.path.isfile(miku_candidate):
                with open(miku_candidate, encoding="utf-8") as f:
                    saved = json.load(f)
                miku_notes = saved.get("miku_notes")
                miku_bpm = float(saved.get("miku_bpm") or 100.0)
    else:
        if midi_filename or abc:
            midi_path = _safe_path(MIDI_DIR, midi_filename) if midi_filename else _abc_to_midi(abc)
        if wav_filename:
            wav_path = _safe_path(WAV_DIR, wav_filename)

    miku_prepared = _prepare_miku(miku_notes, miku_bpm) if miku_notes else None
    midi_port = _resolve_midi_port() if midi_path else ""

    return {
        "midi_path": midi_path,
        "midi_port": midi_port,
        "wav_path": wav_path,
        "miku": miku_prepared,
        "midi_delay_sec": midi_delay_sec,
    }


def execute_playback(plan: dict) -> dict:
    """再生計画を実行して完了まで待つ(ブロッキング)。バックグラウンドスレッドから呼ぶ。"""
    # 歌詞SysExのロードは無音なので、他パートの再生開始前に済ませて頭出しを揃える
    miku_file = None
    sequence: list = []
    if plan["miku"]:
        device, sysex, sequence = plan["miku"]
        miku_file = open(device, "wb", buffering=0)
        miku_file.write(sysex)
        time.sleep(0.15)

    try:
        # VOICEVOX Songの歌声WAVは冒頭に無音パディングがあり、MIDIより聴感上の発音が遅れる。
        # midi_delay_secでMIDI/ミク側の開始を後ろにずらして聴感上の頭出しを揃える。
        wav_proc = None
        if plan["wav_path"]:
            wav_proc = subprocess.Popen(
                ["aplay", "-D", AUDIO_DEVICE, plan["wav_path"]],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            if plan["midi_delay_sec"] > 0:
                time.sleep(plan["midi_delay_sec"])

        miku_result: dict = {}
        miku_thread = None
        if miku_file is not None:

            def _miku_run():
                try:
                    miku_result.update(_play_miku_notes(miku_file, sequence))
                except Exception as exc:  # noqa: BLE001
                    miku_result["error"] = str(exc)

            miku_thread = threading.Thread(target=_miku_run, daemon=True)
            miku_thread.start()

        midi_proc = None
        if plan["midi_path"]:
            midi_proc = subprocess.Popen(
                ["aplaymidi", "-p", plan["midi_port"], plan["midi_path"]],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )

        result: dict = {"midi": None, "wav": None, "miku": None}
        if midi_proc is not None:
            midi_out, _ = midi_proc.communicate()
            result["midi"] = {"returncode": midi_proc.returncode, "output": midi_out.decode(errors="replace")}
        if miku_thread is not None:
            miku_thread.join()
            result["miku"] = miku_result
        if wav_proc is not None:
            wav_out, _ = wav_proc.communicate()
            result["wav"] = {"returncode": wav_proc.returncode, "output": wav_out.decode(errors="replace")}
        return result
    finally:
        if miku_file is not None:
            miku_file.close()


# 再生状態(1台のピアノ/ミクを共有するため同時再生は1つに制限する)
_play_lock = threading.Lock()
_play_state: dict = {"playing": False, "started_at": None, "finished_at": None, "summary": None, "result": None}


def _run_playback_background(plan: dict) -> None:
    try:
        result = execute_playback(plan)
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}
    with _play_lock:
        _play_state["playing"] = False
        _play_state["finished_at"] = time.time()
        _play_state["result"] = result


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
                miku_notes = data.get("miku_notes")
                miku_bpm = float(data.get("miku_bpm") or 100.0)
                with _play_lock:
                    if _play_state["playing"]:
                        self._send_json(409, {
                            "error": "already_playing",
                            "message": "別の曲を再生中です。終わるのを待つか GET /status で状況を確認してください。",
                            "current": _play_state["summary"],
                        })
                        return
                    # 検証・変換は同期で行い、失敗はこの場で400を返す。音出しはここから先のスレッドで。
                    plan = prepare_playback(
                        wav_filename=wav_filename,
                        midi_filename=midi_filename,
                        abc=abc,
                        library_name=library_name,
                        midi_delay_sec=midi_delay_sec,
                        miku_notes=miku_notes,
                        miku_bpm=miku_bpm,
                    )
                    summary = {
                        "library_name": library_name or None,
                        "midi": bool(plan["midi_path"]),
                        "wav": bool(plan["wav_path"]),
                        "miku": bool(plan["miku"]),
                    }
                    _play_state.update(
                        playing=True, started_at=time.time(), finished_at=None,
                        summary=summary, result=None,
                    )
                threading.Thread(target=_run_playback_background, args=(plan,), daemon=True).start()
                self._send_json(200, {"status": "started", "parts": summary,
                                      "hint": "再生はバックグラウンドで進行中。結果は GET /status で確認できます。"})
            else:
                name = str(data.get("name") or "")
                abc = str(data.get("abc") or "")
                wav_filename = str(data.get("wav_filename") or "")
                miku_notes = data.get("miku_notes")
                miku_bpm = float(data.get("miku_bpm") or 100.0)
                result = save_song(
                    name=name, abc=abc, wav_filename=wav_filename,
                    miku_notes=miku_notes, miku_bpm=miku_bpm,
                )
                self._send_json(200, {"status": "ok", **result})
        except (ValueError, FileNotFoundError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/songs":
            try:
                self._send_json(200, {"songs": list_songs()})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return
        if self.path == "/status":
            with _play_lock:
                self._send_json(200, dict(_play_state))
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print(f"[piano_server] {self.address_string()} - {fmt % args}")


def main() -> None:
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    print(f"[piano_server] listening on {BIND_HOST}:{BIND_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
