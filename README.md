# osu!taiko Live View

osu!taiko用のリアルタイム譜面ビューアです。  
[tosu](https://github.com/tosu/tosu) の WebSocket API を使ってゲームの状態を読み取り、別ウィンドウにノーツをスクロール表示します。

![screenshot](screenshot.png)

---

## 必要なもの

- Windows 10/11
- Python 3.10 以上
- [tosu](https://github.com/tosu/tosu)（osu! のメモリリーダー）

---

## インストール

```bash
pip install websocket-client
```
## 使い方

1. **tosu を起動する**（`127.0.0.1:24050` で動いている必要があります）
2. **osu! を起動する**
3. スクリプトを実行する

```bash
python ds.py
```

ウィンドウサイズを指定する場合：

```bash
python ds.py --width 1600 --height 400
```

| 操作 | 動作 |
|------|------|
| `ESC` | 終了 |
| ウィンドウドラッグ | リサイズ |

---

## 機能

- **リアルタイム同期** — tosu の `/v2/precise` WebSocket で同期
- **全ノーツ対応** — Don / Kat / 大音符 / ドラムロール / スピナー
- **Mod対応** — DT / NC / HT のスクロール速度自動調整
- **半透明** — opacity 約86%（背景が透けて見える）

---

## 設定

`ds.py` の上部にある定数で外観を調整できます。

```python
SCROLL_SPEED  = 0.65   # スクロール速度 (px/ms)
LOOKAHEAD_MS  = 4000   # 何ms先まで表示するか
HIT_CIRCLE_X  = 200    # 判定ラインのX座標
NOTE_R_SMALL  = 28     # 通常ノーツの半径
NOTE_R_BIG    = 40     # 大ノーツの半径
```

opacity を変えたい場合は `run_renderer` 内の以下の行を編集：

```python
root.attributes("-alpha", 220/255)  # 0.0〜1.0
```

---

## 仕組み

```
osu! (ゲーム)
  └─ tosu (メモリリーダー)
       ├─ WebSocket /v2        → 譜面情報・ゲーム状態・Mod
       └─ WebSocket /v2/precise → currentTime (高頻度)
            └─ ds.py (このツール)
                 ├─ .osu ファイルをローカルから直接パース
                 └─ tkinter Canvas にノーツを描画
```

---

## 注意

- tosu が起動していないと接続エラーが出ますが、自動で再接続します
- osu! の Songs フォルダは tosu から自動取得するため設定不要です

---
