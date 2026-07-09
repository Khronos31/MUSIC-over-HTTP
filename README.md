# MUSIC-over-HTTP

DG-STK5S（Ubuntu Server、家庭内LAN上のMIDI/USBオーディオ集約ホスト）で動く、電子ピアノ(CASIO LK-222)の演奏デーモン。HTTP POSTでMIDIとWAVを受け取り、同時に再生する。

## 構成

- **`piano_server.py`** — 標準ライブラリのみで書かれたHTTPデーモン。`POST /play_song`でMIDI(またはABC記法テキスト)とWAVのファイル名を受け取り、`aplaymidi`(MIDI→CASIO USB-MIDI)と`aplay`(WAV→USBオーディオアダプタ経由でピアノのAUX IN)を同時起動して再生する。
- **`piano-server.service`** — systemdユニット定義。CIFSマウント(`/mnt/ha-config`、別途`/etc/fstab`で設定)完了後に起動するよう`RequiresMountsFor`を指定。
- **`build_midi.py`** — 外部ライブラリなしで標準MIDIファイル(SMF Format 0)を直接組み立てるスクリプト。現在は`abc2midi`(ABC記法→MIDI変換、`abcmidi`パッケージ)への移行により主経路ではなくなったが、参考実装として残す。

## API

```
POST /play_song
Content-Type: application/json

{"wav_filename": "song-xxxx.wav", "midi_filename": "kirakira.mid"}
# または
{"wav_filename": "song-xxxx.wav", "abc": "X:1\nT:...\n%%MIDI channel 3\n..."}
```

- `wav_filename`: `/mnt/ha-config/embodied-ha/wav/` 配下のファイル名(basenameのみ)
- `midi_filename` / `abc`: どちらか一方を指定。`midi_filename`は`/mnt/ha-config/embodied-ha/midi/`配下、`abc`はABC記法テキストをその場で`abc2midi`変換
- MIDIチャンネル3・4を使うとLK-222の鍵盤がライトアップする(ナビゲートチャンネル)。ch1/2は発音のみ

呼び出し元の使い方（家全体のコンテキストにおけるこのAPIの位置づけ）は `Khronos31/embodied-ha` リポジトリの `/config/embodied-ha/device_apis.md`（あかね向けドキュメント）を参照。

## 依存

- `alsa-utils`（`aplay`/`aplaymidi`）
- `abcmidi`（`abc2midi`）
- Samba NAS(HAOS側アドオン)の`config`共有をCIFSマウントして`/config/embodied-ha/`を参照する構成

## デプロイ

DG-STK5S上で直接 `git clone` し、`piano-server.service` を `/etc/systemd/system/` にコピー、`systemctl enable --now` する。
