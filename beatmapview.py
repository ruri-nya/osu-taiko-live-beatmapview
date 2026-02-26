"""
依存: pip install pygame websocket-client
前提: tosu が起動して http://127.0.0.1:24050 で動いていること
"""

import os
import sys
import json
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from typing import Optional

import websocket

# ─── Mod ビットフラグ (osu! 標準) ──────────────────────
MOD_DT = 1 << 6   # 64
MOD_NC = 1 << 9   # 512  (NC は内部的に DT も立つ)
MOD_HT = 1 << 8   # 256

def mods_to_speed_rate(mods_number: int) -> float:
    """DT/NC → 1.5x、HT → 0.75x、それ以外 → 1.0x"""
    if mods_number & MOD_DT or mods_number & MOD_NC:
        return 1.5
    if mods_number & MOD_HT:
        return 0.75
    return 1.0

def mods_to_label(mods_number: int) -> str:
    if mods_number & MOD_NC:
        return "NC"
    if mods_number & MOD_DT:
        return "DT"
    if mods_number & MOD_HT:
        return "HT"
    return ""

# ─── 設定 ──────────────────────────────────────────────
TOSU_WS_URL        = "ws://127.0.0.1:24050/websocket/v2"
TOSU_WS_PRECISE    = "ws://127.0.0.1:24050/websocket/v2/precise"

WINDOW_W, WINDOW_H = 1280, 360
FPS = 240

# Taiko ノーツ見た目
HIT_CIRCLE_X  = 200          # 判定ライン X 座標
SCROLL_SPEED   = 0.8        # px/ms (BPMや速度によらず固定。後でSV対応可)
LOOKAHEAD_MS   = 4000        # 右端に何ms先を表示するか

# 色
COL_BG         = (20,  20,  20)
COL_LANE       = (40,  40,  40)
COL_LINE       = (80,  80,  80)
COL_DON        = (255, 80,  30)   # 赤 D
COL_KAT        = (30, 160, 255)   # 青 K
COL_DON_BIG    = (255, 140,  60)
COL_KAT_BIG    = (100, 200, 255)
COL_SPINNER    = (200, 200,  50)
COL_DRUMROLL   = (200, 150,  50)
COL_HIT_RING   = (255, 255, 255)
COL_TEXT       = (220, 220, 220)
COL_WARN       = (255,  60,  60)

NOTE_R_SMALL   = 28
NOTE_R_BIG     = 40
LANE_Y         = WINDOW_H // 2

# ─── beatmap parser────────────────────────────────

@dataclass
class TaikoNote:
    time_ms: int
    note_type: str
    end_time_ms: int = 0 

def parse_osu_taiko(content: str) -> list[TaikoNote]:
    """
    osuのTaiko ノーツ変換ルールに従う。
    """
    notes: list[TaikoNote] = []
    in_hit_objects = False

    for raw_line in content.splitlines():
        line = raw_line.strip()

        if line == "[HitObjects]":
            in_hit_objects = True
            continue
        if line.startswith("[") and in_hit_objects:
            break
        if not in_hit_objects or not line or line.startswith("//"):
            continue

        parts = line.split(",")
        if len(parts) < 5:
            continue

        try:
            x        = int(parts[0])
            hit_time = int(parts[2])
            obj_type = int(parts[3])
            hitsound = int(parts[4])
        except ValueError:
            continue

        # スピナー (type & 8)
        if obj_type & 8:
            end_time = int(parts[5]) if len(parts) > 5 else hit_time
            notes.append(TaikoNote(hit_time, "spinner", end_time))
            continue

        # スライダー → ドラムロール (type & 2)
        if obj_type & 2:
            end_time = hit_time
            if len(parts) > 7:
                try:
                    slides = int(parts[6])
                    length = float(parts[7])
                    end_time = hit_time + int(length * 10)  # 暫定
                except (ValueError, IndexError):
                    pass
            big = bool(hitsound & 4)
            notes.append(TaikoNote(hit_time, "drumroll_big" if big else "drumroll", end_time))
            continue

        is_kat = bool(hitsound & 2) or bool(hitsound & 8)
        is_big = bool(hitsound & 4)   # finish

        if is_kat:
            ntype = "kat_big" if is_big else "kat"
        else:
            ntype = "don_big" if is_big else "don"

        notes.append(TaikoNote(hit_time, ntype))

    notes.sort(key=lambda n: n.time_ms)
    return notes


def _fix_drumroll_endtimes(notes: list[TaikoNote], content: str) -> list[TaikoNote]:
    """
    sv 1.0 なら length * beat_duration、sv 0.5 なら length * beat_duration * 0.5 など。
    本格対応は必要に応じて拡張してください。
    """
    return notes


# ─── ゲーム状態（スレッド間共有） ────────────────────────

@dataclass
class GameState:
    # tosu から受け取るデータ
    state_name: str = "Menu"
    game_time_ms: int = 0 
    beatmap_path: str = ""
    songs_folder: str = "" 
    audio_filename: str = ""

    notes: list[TaikoNote] = field(default_factory=list)
    loaded_beatmap_path: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    play_start_wall: float = 0.0
    play_start_game: int   = 0

    playing: bool = False
    prev_state: str = "Menu"

    # Mod 情報
    mods_number: int = 0        # play.mods.number のビットフラグ
    speed_rate: float = 1.0     # DT=1.5 / HT=0.75 / nomod=1.0
    mod_label: str = ""


state = GameState()


# ─── tosu WebSocket クライアント ──────────────────────────

_debug_dumped = False  # 最初の1回だけ全体をダンプする

def on_message(ws, message):
    global _debug_dumped
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return

    # ─── デバッグ: 最初の受信データのキー構造をダンプ ───
    if not _debug_dumped:
        _debug_dumped = True
        print("[DEBUG] Top-level keys:", list(data.keys()))
        # beatmap / directPath / folders の中身を確認
        for key in ("beatmap", "directPath", "folders", "files", "state"):
            if key in data:
                print(f"[DEBUG] data['{key}'] = {json.dumps(data[key], ensure_ascii=False)[:300]}")
    # ────────────────────────────────────────────────────

    with state.lock:
        raw_state = data.get("state", {})
        new_state_name = raw_state.get("name", state.state_name)

        if new_state_name == "Playing" and state.prev_state != "Playing":
            play_data = data.get("play", {})
            mods_num = play_data.get("mods", {}).get("number", 0)
            state.mods_number = mods_num
            state.speed_rate  = mods_to_speed_rate(mods_num)
            state.mod_label   = mods_to_label(mods_num)

            state.play_start_wall = time.perf_counter()
            state.play_start_game = data.get("beatmap", {}).get("time", {}).get("live", 0)
            state.playing = True
            print(f"[BV] Play start! mods={state.mod_label or 'NoMod'} "
                  f"speed={state.speed_rate}x game_time={state.play_start_game}ms")

        if new_state_name != "Playing":
            state.playing = False

        state.prev_state = state.state_name
        state.state_name = new_state_name

        beatmap_data = data.get("beatmap", {})
        live_time = beatmap_data.get("time", {}).get("live", None)
        if live_time is not None:
            state.game_time_ms = live_time
        direct_path = data.get("directPath", {})
        folders     = data.get("folders", {})

        new_beatmap = direct_path.get("beatmapFile", "")
        new_songs   = folders.get("songs", "")

        if new_songs and new_songs != state.songs_folder:
            state.songs_folder = new_songs
            print(f"[WS] Songs folder: {new_songs}")

        if new_beatmap and new_beatmap != state.beatmap_path:
            state.beatmap_path = new_beatmap
            state.notes = []       # パスが変わったら再ロード要求
            print(f"[WS] Beatmap changed: {new_beatmap}")


def on_error(ws, error):
    print(f"[WS] Error: {error}")


def on_close(ws, code, msg):
    print("[WS] Connection closed. Reconnecting in 3s...")
    time.sleep(3)
    start_ws()


def on_open(ws):
    print("[WS] Connected to tosu!")


def start_ws():
    ws = websocket.WebSocketApp(
        TOSU_WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    ws.run_forever()


# ─── /v2/precise: currentTime だけ取って時刻補間に使う ───

def on_precise_message(ws, message):
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return
    t = data.get("currentTime")
    if t is None:
        return
    with state.lock:
        state.game_time_ms = t


def on_precise_close(ws, code, msg):
    time.sleep(3)
    start_precise_ws()


def start_precise_ws():
    ws = websocket.WebSocketApp(
        TOSU_WS_PRECISE,
        on_message=on_precise_message,
        on_error=lambda ws, e: None,
        on_close=on_precise_close,
    )
    ws.run_forever()


# ─── 譜面ロード（別スレッド） ─────────────────────────────

def beatmap_loader_thread():
    while True:
        time.sleep(0.3)
        with state.lock:
            current_rel  = state.beatmap_path
            songs_folder = state.songs_folder
            already      = state.loaded_beatmap_path

        if not current_rel or not songs_folder:
            continue
        if current_rel == already:
            continue

        full_path = os.path.join(songs_folder, current_rel)
        print(f"[Loader] Loading: {full_path}")
        try:
            with open(full_path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
            notes = parse_osu_taiko(raw)
            print(f"[Loader] Parsed {len(notes)} notes")
            with state.lock:
                state.notes = notes
                state.loaded_beatmap_path = current_rel
        except FileNotFoundError:
            print(f"[Loader] File not found: {full_path}")
        except Exception as e:
            print(f"[Loader] Error: {e}")


# ─── 色変換ヘルパー ────────────────────────────────────────

def rgb_to_hex(r, g, b):
    return f"#{r:02x}{g:02x}{b:02x}"

def note_color_hex(ntype: str) -> str:
    m = {
        "don":          COL_DON,
        "don_big":      COL_DON_BIG,
        "kat":          COL_KAT,
        "kat_big":      COL_KAT_BIG,
        "drumroll":     COL_DRUMROLL,
        "drumroll_big": COL_DRUMROLL,
        "spinner":      COL_SPINNER,
    }
    return rgb_to_hex(*m.get(ntype, COL_DON))

def note_radius(ntype: str) -> int:
    return NOTE_R_BIG if "big" in ntype else NOTE_R_SMALL


# ─── tkinter レンダラー ─────────────────────────────────────

def run_renderer(window_w: int, window_h: int):
    global WINDOW_W, WINDOW_H, LANE_Y

    WINDOW_W = window_w
    WINDOW_H = window_h
    LANE_Y   = window_h // 2

    root = tk.Tk()
    root.title("osu!taiko BeatmapView")
    root.geometry(f"{window_w}x{window_h}")
    root.resizable(True, True)
    root.attributes("-topmost", True)   # tkinter の確実な最前面
    root.attributes("-alpha", 220/255)  # opacity

    # アイコン
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images.ico")
    if os.path.exists(icon_path):
        try:
            root.iconbitmap(icon_path)
            print("[BV] Icon loaded")
        except Exception as e:
            print(f"[BV] Icon load failed: {e}")

    canvas = tk.Canvas(root, bg=rgb_to_hex(*COL_BG), highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)

    print("[BV] Always-on-top enabled (tkinter)")

    # 色定数をhex化
    HEX_BG      = rgb_to_hex(*COL_BG)
    HEX_LANE    = rgb_to_hex(*COL_LANE)
    HEX_GRID1   = "#3c3c3c"
    HEX_GRID2   = "#2d2d2d"
    HEX_RING    = rgb_to_hex(*COL_HIT_RING)
    HEX_TEXT    = rgb_to_hex(*COL_TEXT)
    HEX_WARN    = rgb_to_hex(*COL_WARN)
    HEX_OK      = "#64ff64"
    HEX_GRAY    = "#787878"
    HEX_WHITE   = "#ffffff"

    last_frame_time = time.perf_counter()
    fps_display = 0.0
    prev_W, prev_H = -1, -1

    # ── 再利用するcanvasアイテムのIDを保持 ──────────────────
    # 背景・グリッド・判定ラインはリサイズ時だけ再作成
    bg_items: list = []
    # ノーツは譜面ロード時に全て事前作成、coords()で位置だけ更新
    note_items: list = []   # (note_index, item_ids...)のリスト
    note_cache_key = None   # notes が変わったか検出用

    # UIテキストは起動時に1回作成してconfigure/coordsで更新
    id_state_text = canvas.create_text(10, 10, text="", fill=HEX_OK,
                                       font=("Courier", 14, "bold"), anchor="nw")
    id_mod_text   = canvas.create_text(130, 10, text="", fill=HEX_TEXT,
                                       font=("Courier", 14, "bold"), anchor="nw")
    id_time_text  = canvas.create_text(10, 10, text="", fill=HEX_TEXT,
                                       font=("Courier", 12), anchor="ne")
    id_fps_text   = canvas.create_text(10, 10, text="", fill=HEX_GRAY,
                                       font=("Courier", 11), anchor="sw")

    def build_bg(W, H, LY):
        """背景・グリッド・判定ラインを再描画（リサイズ時のみ呼ぶ）"""
        for iid in bg_items:
            canvas.delete(iid)
        bg_items.clear()

        bg_items.append(canvas.create_rectangle(
            0, LY - NOTE_R_BIG - 10, W, LY + NOTE_R_BIG + 10,
            fill=HEX_LANE, outline=""))

        for dtt in range(0, LOOKAHEAD_MS + 1, 500):
            gx  = HIT_CIRCLE_X + int(dtt * SCROLL_SPEED)
            col = HEX_GRID1 if dtt % 1000 == 0 else HEX_GRID2
            bg_items.append(canvas.create_line(gx, 0, gx, H, fill=col))

        for r, w in ((NOTE_R_BIG + 14, 3), (NOTE_R_SMALL + 10, 2)):
            bg_items.append(canvas.create_oval(
                HIT_CIRCLE_X - r, LY - r, HIT_CIRCLE_X + r, LY + r,
                outline=HEX_RING, width=w))

        # UIアイテムを最前面に
        for iid in (id_state_text, id_mod_text, id_time_text, id_fps_text):
            canvas.tag_raise(iid)

    def build_note_items(notes, LY):
        """ノーツのcanvasアイテムを事前作成（譜面変更時のみ呼ぶ）"""
        for ids in note_items:
            for iid in ids:
                canvas.delete(iid)
        note_items.clear()

        for note in notes:
            col = note_color_hex(note.note_type)
            r   = note_radius(note.note_type)

            if note.note_type in ("drumroll", "drumroll_big"):
                i0 = canvas.create_rectangle(0, 0, 1, 1, fill=col, outline="", state="hidden")
                i1 = canvas.create_oval(0, 0, 1, 1, fill=col, outline="", state="hidden")
                i2 = canvas.create_oval(0, 0, 1, 1, fill=col, outline="", state="hidden")
                note_items.append((i0, i1, i2))
            elif note.note_type == "spinner":
                i0 = canvas.create_rectangle(0, 0, 1, 1, fill=col, outline="", state="hidden")
                i1 = canvas.create_oval(0, 0, 1, 1, outline=col, width=4, state="hidden")
                i2 = canvas.create_oval(0, 0, 1, 1, outline=col, width=4, state="hidden")
                note_items.append((i0, i1, i2))
            else:
                i0 = canvas.create_oval(0, 0, 1, 1, fill=col, outline=HEX_WHITE, width=2, state="hidden")
                note_items.append((i0,))

        # UIアイテムを最前面に
        for iid in (id_state_text, id_mod_text, id_time_text, id_fps_text):
            canvas.tag_raise(iid)

    def draw():
        nonlocal last_frame_time, fps_display, prev_W, prev_H, note_cache_key
        global WINDOW_W, WINDOW_H, LANE_Y

        now = time.perf_counter()
        dt_frame = now - last_frame_time
        last_frame_time = now
        if dt_frame > 0:
            fps_display = fps_display * 0.9 + (1.0 / dt_frame) * 0.1

        W = canvas.winfo_width()
        H = canvas.winfo_height()
        if W < 2 or H < 2:
            root.after(0, draw)
            return

        WINDOW_W = W
        WINDOW_H = H
        LANE_Y   = H // 2

        # リサイズ検出 → 背景再構築
        if W != prev_W or H != prev_H:
            build_bg(W, H, LANE_Y)
            prev_W, prev_H = W, H

        # 状態取得
        with state.lock:
            game_state = state.state_name
            notes_snap = state.notes      # 参照のみ（コピーしない）
            mod_label  = state.mod_label
            current_ms = state.game_time_ms

        # 譜面変更検出 → ノーツアイテム再構築
        ck = id(notes_snap)
        if ck != note_cache_key:
            build_note_items(notes_snap, LANE_Y)
            note_cache_key = ck

        # ノーツ位置更新（coords のみ、作成なし）
        for idx, note in enumerate(notes_snap):
            if idx >= len(note_items):
                break
            ids = note_items[idx]
            dtt = note.time_ms - current_ms

            nx = HIT_CIRCLE_X + int(dtt * SCROLL_SPEED)
            r  = note_radius(note.note_type)

            if note.note_type in ("drumroll", "drumroll_big"):
                end_dtt = note.end_time_ms - current_ms
                # ドラムロールは終端が判定ラインを過ぎたら消す
                if end_dtt < 0 or dtt > LOOKAHEAD_MS:
                    for iid in ids:
                        canvas.itemconfigure(iid, state="hidden")
                    continue
                ex  = HIT_CIRCLE_X + int(end_dtt * SCROLL_SPEED)
                bar_y = LANE_Y - r // 2
                canvas.coords(ids[0], nx, bar_y, max(ex, nx+4), bar_y + r)
                canvas.coords(ids[1], nx-r, LANE_Y-r, nx+r, LANE_Y+r)
                canvas.coords(ids[2], ex-r, LANE_Y-r, ex+r, LANE_Y+r)
                for iid in ids:
                    canvas.itemconfigure(iid, state="normal")

            elif note.note_type == "spinner":
                end_dtt = note.end_time_ms - current_ms
                # スピナーも終端が判定ラインを過ぎたら消す
                if end_dtt < 0 or dtt > LOOKAHEAD_MS:
                    for iid in ids:
                        canvas.itemconfigure(iid, state="hidden")
                    continue
                ex  = HIT_CIRCLE_X + int(end_dtt * SCROLL_SPEED)
                sr  = NOTE_R_SMALL + 6
                canvas.coords(ids[0], nx, LANE_Y-6, max(ex, nx+4), LANE_Y+6)
                canvas.coords(ids[1], nx-sr, LANE_Y-sr, nx+sr, LANE_Y+sr)
                canvas.coords(ids[2], ex-sr, LANE_Y-sr, ex+sr, LANE_Y+sr)
                for iid in ids:
                    canvas.itemconfigure(iid, state="normal")

            else:
                # 通常ノーツは判定ラインに到達（dtt<=0）でスパッと消す
                if dtt < 0 or dtt > LOOKAHEAD_MS:
                    for iid in ids:
                        canvas.itemconfigure(iid, state="hidden")
                    continue
                canvas.coords(ids[0], nx-r, LANE_Y-r, nx+r, LANE_Y+r)
                canvas.itemconfigure(ids[0], state="normal")

        # UIテキスト更新（configure のみ）
        state_col = HEX_OK if game_state == "Playing" else HEX_WARN
        canvas.itemconfigure(id_state_text, text=game_state, fill=state_col)
        canvas.coords(id_state_text, 10, 10)

        if mod_label:
            mod_col = {"DT": "#ffc832", "NC": "#ffa050", "HT": "#50b4ff"}.get(mod_label, HEX_TEXT)
            canvas.itemconfigure(id_mod_text, text=mod_label, fill=mod_col)
            canvas.coords(id_mod_text, 10 + len(game_state) * 10 + 12, 10)
        else:
            canvas.itemconfigure(id_mod_text, text="")

        mins = current_ms // 60000
        secs = (current_ms % 60000) // 1000
        ms   = current_ms % 1000
        canvas.itemconfigure(id_time_text, text=f"{mins:02d}:{secs:02d}.{ms:03d}")
        canvas.coords(id_time_text, W - 10, 10)

        canvas.itemconfigure(id_fps_text, text=f"{fps_display:.0f}fps")
        canvas.coords(id_fps_text, 10, H - 8)

        root.after(4, draw)   # ~120fps（UIスレッドを占領しない）

    root.bind("<Escape>", lambda e: root.destroy())
    root.after(16, draw)
    root.mainloop()

# ─── エントリーポイント ───────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="osu!taiko DS Tool")
    parser.add_argument("--width",  type=int, default=1280, help="ウィンドウ幅 (default: 1280)")
    parser.add_argument("--height", type=int, default=360,  help="ウィンドウ高さ (default: 360)")
    args = parser.parse_args()

    print("=" * 50)
    print("  osu!taiko beatmapView")
    print(f"  windowsize: {args.width}x{args.height}")
    print("  tosu が 127.0.0.1:24050 で動いていることを確認してください")
    print("  ESC で終了")
    print("=" * 50)

    ws_thread = threading.Thread(target=start_ws, daemon=True)
    ws_thread.start()

    precise_thread = threading.Thread(target=start_precise_ws, daemon=True)
    precise_thread.start()

    loader_thread = threading.Thread(target=beatmap_loader_thread, daemon=True)
    loader_thread.start()

    run_renderer(args.width, args.height)


if __name__ == "__main__":

    main()
