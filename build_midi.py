#!/usr/bin/env python3
"""標準MIDIファイル(SMF Format 0)を外部ライブラリなしで手作りする。
LK-222の鍵盤ライトアップ用にチャンネル3(0-indexed 2)固定でNote On/Offを並べる。
"""
import struct
import sys

TICKS_PER_BEAT = 480
CHANNEL = 2  # ch3 (0-indexed) -> LK-222で鍵盤ライトアップ

# きらきら星: (note, beats)
NOTES = [
    (60, 1), (60, 1), (67, 1), (67, 1),
    (69, 1), (69, 1), (67, 2),
    (65, 1), (65, 1), (64, 1), (64, 1),
    (62, 1), (62, 1), (60, 2),
]


def varlen(value: int) -> bytes:
    buf = [value & 0x7F]
    value >>= 7
    while value:
        buf.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(buf)


def build_track() -> bytes:
    events = bytearray()
    # テンポ設定 (120bpm = 500000 us/beat)
    events += varlen(0) + bytes([0xFF, 0x51, 0x03]) + (500000).to_bytes(3, "big")
    for note, beats in NOTES:
        dur_ticks = int(TICKS_PER_BEAT * beats * 0.85)
        gap_ticks = int(TICKS_PER_BEAT * beats * 0.15)
        events += varlen(0) + bytes([0x90 | CHANNEL, note, 100])  # Note On
        events += varlen(dur_ticks) + bytes([0x80 | CHANNEL, note, 0])  # Note Off
        if gap_ticks:
            events += varlen(gap_ticks) + bytes([0xFF, 0x00])  # no-op wait padding不可のため次のNote Onのdeltaに含める
    # 上のno-op(FF 00)は無効メタなので使わず、gapは次のNote Onのdelta_timeに繰り込む方式に修正
    return bytes(events)


def build_track_v2() -> bytes:
    events = bytearray()
    events += varlen(0) + bytes([0xFF, 0x51, 0x03]) + (500000).to_bytes(3, "big")
    pending_delta = 0
    for note, beats in NOTES:
        dur_ticks = int(TICKS_PER_BEAT * beats * 0.85)
        gap_ticks = int(TICKS_PER_BEAT * beats * 0.15)
        events += varlen(pending_delta) + bytes([0x90 | CHANNEL, note, 100])
        events += varlen(dur_ticks) + bytes([0x80 | CHANNEL, note, 0])
        pending_delta = gap_ticks
    events += varlen(pending_delta) + bytes([0xFF, 0x2F, 0x00])  # End of Track
    return bytes(events)


def main(path: str) -> None:
    track_data = build_track_v2()
    mthd = b"MThd" + struct.pack(">IHHH", 6, 0, 1, TICKS_PER_BEAT)
    mtrk = b"MTrk" + struct.pack(">I", len(track_data)) + track_data
    with open(path, "wb") as f:
        f.write(mthd + mtrk)
    print(f"wrote {path} ({len(mthd) + len(mtrk)} bytes)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "kirakira.mid")
