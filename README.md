# MUSIC-over-HTTP

DG-STK5S（Ubuntu Server、家庭内LAN上のMIDI/USBオーディオ集約ホスト）で動く、電子ピアノ(CASIO LK-222)の演奏デーモン。HTTP POSTでMIDIとWAVを受け取り、同時に再生する。

## 構成

- **`piano_server.py`** — 標準ライブラリのみで書かれたHTTPデーモン。`POST /play_song`でMIDI(またはABC記法テキスト)と(任意で)WAVのファイル名を受け取り、`aplaymidi`(MIDI→CASIO USB-MIDI、ポート番号はクライアント名から毎回動的解決)と`aplay`(WAV→USBオーディオアダプタ経由でピアノのAUX IN)を起動して再生する。WAV省略時はピアノ単独演奏。
- **`piano-server.service`** — systemdユニット定義。マウント状態に関わらずデーモン自体は起動する（マウントへのアクセスは実際のリクエスト処理時のみ）。CIFSマウント(`/mnt/ha-config`)は`/etc/fstab`に`x-systemd.automount`で遅延マウント設定し、起動直後のネットワーク未到達による失敗を回避する。
- **`build_midi.py`** — 外部ライブラリなしで標準MIDIファイル(SMF Format 0)を直接組み立てるスクリプト。現在は`abc2midi`(ABC記法→MIDI変換、`abcmidi`パッケージ)への移行により主経路ではなくなったが、参考実装として残す。

## API

### `POST /play_song`

```
{"wav_filename": "song-xxxx.wav", "midi_filename": "kirakira.mid"}
# または
{"wav_filename": "song-xxxx.wav", "abc": "X:1\nT:...\n%%MIDI channel 3\n...", "midi_delay_sec": 0.25}
# ピアノ単独演奏（歌声WAVなし）
{"abc": "X:1\nT:...\n..."}
```

- `wav_filename`: `/mnt/ha-config/embodied-ha/wav/` 配下のファイル名(basenameのみ)。省略可（省略時はMIDIのみ再生）
- `midi_filename` / `abc` / `library_name`: いずれか一つを指定。`midi_filename`は`/mnt/ha-config/embodied-ha/midi/`配下、`abc`はABC記法テキストをその場で`abc2midi`変換、`library_name`は`/save_song`で保存済みの曲を名前で再生（対応する`{name}.wav`があれば`wav_filename`省略時に自動で使う）
- `midi_delay_sec`: MIDI開始を指定秒数だけ遅らせる（任意、デフォルト0）。VOICEVOX Songの歌声WAVは冒頭に約0.16秒の無音パディングがあり、実機テストでは`0.25`でほぼ同期が取れることを確認済み
- MIDIチャンネル3・4を使うとLK-222の鍵盤がライトアップする(ナビゲートチャンネル)。**ch3=左手側、ch4=右手側**(本体の取扱説明書より)。ch1/2は発音のみ
- 和音はABC記法の角括弧`[CEG]`で指定できる（同一チャンネルで複数Note Onが同時に飛ぶだけ）。ch3に低音の伴奏コード、ch4に高音のメロディを割り当てた2声デュエットの実機演奏を確認済み(2026-07-10)

### `POST /save_song`

```
{"name": "kirakira_duet", "abc": "X:1\nT:...\n...", "wav_filename": "song-xxxx.wav"}
```

あかね(embodied-ha側のエージェント)はWriteツールを持たないため、「残したい曲」を永続化するための保存専用エンドポイント。`name`(英数字・`_`・`-`のみ)をキーに、`SONG_LIBRARY_DIR`(`/mnt/ha-config/embodied-ha/song_library/`)配下へ`{name}.abc`・`{name}.mid`（abcから変換済み）・（`wav_filename`指定時のみ）`{name}.wav`をセットで保存する。同名は上書き。`/home/yunomin61/piano_abc_cache/`（`/play_song`のabc変換に使う使い捨てキャッシュ、7日で自動削除）とは別の永続領域。

### `GET /songs`

保存済みの曲一覧を返す: `{"songs": [{"name": "kirakira_duet", "has_wav": false}, ...]}`

### ミク歌唱（`/play_song`の`miku_notes`）

```
{"miku_bpm": 120, "miku_notes": [{"pitch": "C4", "duration": "quarter", "lyric": "き"}, ...],
 "abc": "..."}
```

DG-STK5Sに接続された**NSX-39（ポケット・ミク）**にリアルタイムで歌わせる。歌詞SysExをスロットに流し込み、Note On/Offで1音節ずつ発声させる方式。`miku_notes`の形式はembodied-ha側の`record`ツールと同一（pitch=音名/`rest`、duration=`whole`〜`sixteenth`、lyric=ひらがな1音節、最大64音節）。ひらがな→音素コード変換はサーバー内蔵の対応表（Yamaha公式`nsx39.js`由来）で行う。`abc`/`wav_filename`と併用でき、歌詞ロード（無音）を再生開始前に済ませることで他パートと頭出しが揃う。ミク+LK-222ピアノ伴奏の同時演奏を実機確認済み（2026-07-10）。NSX-39のデバイスは`amidi -l`から名前で動的解決（カード番号は接続順で変わるため）。

呼び出し元の使い方（家全体のコンテキストにおけるこのAPIの位置づけ）は `Khronos31/embodied-ha` リポジトリの `/config/embodied-ha/device_apis.md`（あかね向けドキュメント）を参照。

## 依存

- `alsa-utils`（`aplay`/`aplaymidi`）
- `abcmidi`（`abc2midi`）
- Samba NAS(HAOS側アドオン)の`config`共有をCIFSマウントして`/config/embodied-ha/`を参照する構成

## デプロイ

DG-STK5S上で直接 `git clone` し、`piano-server.service` を `/etc/systemd/system/` にコピー、`systemctl enable --now` する。
