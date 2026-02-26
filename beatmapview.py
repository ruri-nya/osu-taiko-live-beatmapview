"""
依存: pip install pygame websocket-client
前提: tosu が起動して http://127.0.0.1:24050 で動いていること
"""

import os
import sys
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import bisect
import pygame
from websocket import WebSocketApp

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
FPS = 240          # 高FPSでスクロールを滑らかに

# Taiko ノーツ見た目
HIT_CIRCLE_X  = 200          # 判定ライン X 座標
SCROLL_SPEED   = 0.65        # px/ms (BPMや速度によらず固定。後でSV対応可)
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

# ─── osu!ファイルパーサー ────────────────────────────────

@dataclass
class TaikoNote:
    time_ms: int
    note_type: str
    end_time_ms: int = 0 

def parse_osu_taiko(content: str) -> list[TaikoNote]:
    """
    .osu ファイルの [HitObjects] セクションを Taiko として解析する。
    osu! の Taiko ノーツ変換ルールに従う。
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
    .osu の TimingPoints から SliderMultiplier / BPM を読んで
    ドラムロール end_time を正確に計算する（オプション強化）。
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

    # ウォールクロック補間用（precise受信時に更新）
    interp_wall: float = 0.0   # precise を受け取った瞬間のwall時刻
    interp_game: int   = 0     # precise を受け取った瞬間のgame時刻
    interp_speed: float = 1.0  # speed_rate のコピー

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

    # lock なしで直接代入（GIL で安全、lock競合による描画詰まりを防ぐ）
    raw_state      = data.get("state", {})
    new_state_name = raw_state.get("name", state.state_name)

    if new_state_name == "Playing" and state.prev_state != "Playing":
        play_data        = data.get("play", {})
        mods_num         = play_data.get("mods", {}).get("number", 0)
        state.mods_number = mods_num
        state.speed_rate  = mods_to_speed_rate(mods_num)
        state.mod_label   = mods_to_label(mods_num)
        state.playing     = True
        print(f"[BV] Play start! mods={state.mod_label or 'NoMod'} speed={state.speed_rate}x")

    if new_state_name != "Playing":
        state.playing = False

    state.prev_state = state.state_name
    state.state_name = new_state_name

    beatmap_data = data.get("beatmap", {})
    live_time    = beatmap_data.get("time", {}).get("live", None)
    if live_time is not None:
        state.game_time_ms = live_time

    new_beatmap = data.get("directPath", {}).get("beatmapFile", "")
    new_songs   = data.get("folders", {}).get("songs", "")

    if new_songs and new_songs != state.songs_folder:
        state.songs_folder = new_songs
        print(f"[WS] Songs folder: {new_songs}")

    if new_beatmap and new_beatmap != state.beatmap_path:
        state.beatmap_path = new_beatmap
        state.notes = []
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
    ws = WebSocketApp(
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
    # precise 受信時にウォールクロックをラッチして補間の基準にする
    state.game_time_ms = t
    state.interp_game  = t
    state.interp_wall  = time.perf_counter()
    state.interp_speed = state.speed_rate


def on_precise_close(ws, code, msg):
    time.sleep(3)
    start_precise_ws()


def start_precise_ws():
    ws = WebSocketApp(
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
            state.notes = notes
            state.loaded_beatmap_path = current_rel
            note_times_cache[:] = [n.time_ms for n in notes]
        except FileNotFoundError:
            print(f"[Loader] File not found: {full_path}")
        except Exception as e:
            print(f"[Loader] Error: {e}")


# ─── ノーツ描画ヘルパー ──────────────────────────────────────

def note_color(ntype: str):
    return {
        "don": COL_DON, "don_big": COL_DON_BIG,
        "kat": COL_KAT, "kat_big": COL_KAT_BIG,
        "drumroll": COL_DRUMROLL, "drumroll_big": COL_DRUMROLL,
        "spinner": COL_SPINNER,
    }.get(ntype, COL_DON)

def note_radius(ntype: str) -> int:
    return NOTE_R_BIG if "big" in ntype else NOTE_R_SMALL


def build_note_surfs() -> dict:
    """don/kat/big をサーフェスにプリレンダ"""
    import pygame
    surfs = {}
    for ntype in ("don", "don_big", "kat", "kat_big"):
        r   = note_radius(ntype)
        col = note_color(ntype)
        s   = pygame.Surface((r*2+4, r*2+4), pygame.SRCALPHA)
        pygame.draw.circle(s, col,             (r+2, r+2), r)
        pygame.draw.circle(s, (255,255,255),   (r+2, r+2), r, 2)
        surfs[ntype] = s
    return surfs


def build_static_bg(surf, W, H, LY):
    """背景・グリッド・判定ラインを surf に描画"""
    import pygame
    surf.fill(COL_BG)
    pygame.draw.rect(surf, COL_LANE,
                     (0, LY - NOTE_R_BIG - 10, W, (NOTE_R_BIG+10)*2))
    for dt in range(0, LOOKAHEAD_MS+1, 500):
        gx  = HIT_CIRCLE_X + int(dt * SCROLL_SPEED)
        col = (60,60,60) if dt % 1000 == 0 else (45,45,45)
        pygame.draw.line(surf, col, (gx, 0), (gx, H), 1)
    for r, w in ((NOTE_R_BIG+14, 3), (NOTE_R_SMALL+10, 2)):
        pygame.draw.circle(surf, COL_HIT_RING, (HIT_CIRCLE_X, LY), r, w)


# ─── pygame レンダラー ────────────────────────────────────────

def run_renderer(window_w: int, window_h: int):
    global WINDOW_W, WINDOW_H, LANE_Y
    import pygame
    import ctypes
    import tkinter as tk

    # Windows タイマー精度 1ms
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

    WINDOW_W = window_w
    WINDOW_H = window_h
    LANE_Y   = window_h // 2

    # ── tkinter ウィンドウ（最前面・半透明を担当）────────────
    root = tk.Tk()
    root.title("osu!taiko BeatmapView")
    root.geometry(f"{window_w}x{window_h}")
    root.resizable(True, True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 220/255)

    # アイコン
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images.ico")
    if os.path.exists(icon_path):
        try:
            root.iconbitmap(icon_path)
            print("[BV] Icon loaded")
        except Exception as e:
            print(f"[BV] Icon load failed: {e}")

    embed = tk.Frame(root, width=window_w, height=window_h)
    embed.pack(fill=tk.BOTH, expand=True)
    embed.update()

    # ── pygame を tkinter Frame に埋め込む（windib なし）────
    os.environ["SDL_WINDOWID"] = str(embed.winfo_id())
    # SDL_VIDEODRIVER は設定しない（windib を使わない）

    pygame.init()
    screen = pygame.display.set_mode((window_w, window_h))
    clock  = pygame.time.Clock()

    font_sm = pygame.font.SysFont("monospace", 18)
    font_md = pygame.font.SysFont("monospace", 24, bold=True)

    static_bg  = pygame.Surface((WINDOW_W, WINDOW_H))
    build_static_bg(static_bg, WINDOW_W, WINDOW_H, LANE_Y)
    note_surfs = build_note_surfs()

    print("[BV] Always-on-top enabled (tkinter)")

    # ── pygame ループ（別スレッド）───────────────────────────
    _stop = [False]

    def pygame_loop():
        global WINDOW_W, WINDOW_H, LANE_Y
        nonlocal static_bg

        while not _stop[0]:
            # リサイズ検出（set_mode は呼ばず Surface だけ作り直す）
            W = embed.winfo_width()
            H = embed.winfo_height()
            if W > 1 and H > 1 and (W != WINDOW_W or H != WINDOW_H):
                WINDOW_W = W
                WINDOW_H = H
                LANE_Y   = H // 2
                static_bg = pygame.Surface((W, H))
                build_static_bg(static_bg, W, H, LANE_Y)

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    _stop[0] = True

            # 時刻補間
            interp_wall  = state.interp_wall
            interp_game  = state.interp_game
            interp_speed = state.interp_speed
            if interp_wall > 0:
                elapsed    = (time.perf_counter() - interp_wall) * 1000
                current_ms = int(interp_game + elapsed * interp_speed)
            else:
                current_ms = state.game_time_ms

            game_state = state.state_name
            notes_snap = state.notes
            mod_label  = state.mod_label

            # 描画
            screen.blit(static_bg, (0, 0))

            lo = bisect.bisect_left(note_times_cache, current_ms)
            hi = bisect.bisect_right(note_times_cache, current_ms + LOOKAHEAD_MS)

            for i in range(lo, min(hi, len(notes_snap))):
                note = notes_snap[i]
                dtt  = note.time_ms - current_ms
                nx   = HIT_CIRCLE_X + int(dtt * SCROLL_SPEED)
                r    = note_radius(note.note_type)
                col  = note_color(note.note_type)

                if note.note_type in ("drumroll", "drumroll_big"):
                    end_dtt = note.end_time_ms - current_ms
                    if end_dtt < 0:
                        continue
                    ex    = HIT_CIRCLE_X + int(end_dtt * SCROLL_SPEED)
                    bar_y = LANE_Y - r // 2
                    pygame.draw.rect(screen, col, (nx, bar_y, max(ex-nx, 4), r))
                    pygame.draw.circle(screen, col, (nx, LANE_Y), r)
                    pygame.draw.circle(screen, col, (ex, LANE_Y), r)
                elif note.note_type == "spinner":
                    end_dtt = note.end_time_ms - current_ms
                    if end_dtt < 0:
                        continue
                    ex = HIT_CIRCLE_X + int(end_dtt * SCROLL_SPEED)
                    pygame.draw.rect(screen, col, (nx, LANE_Y-6, max(ex-nx, 4), 12))
                    for cx in (nx, ex):
                        pygame.draw.circle(screen, col, (cx, LANE_Y), NOTE_R_SMALL+6, 4)
                else:
                    if dtt < 0:
                        continue
                    s = note_surfs.get(note.note_type)
                    if s:
                        screen.blit(s, (nx - r - 2, LANE_Y - r - 2))

            fps       = clock.get_fps()
            state_col = (100,255,100) if game_state == "Playing" else COL_WARN
            screen.blit(font_md.render(game_state, True, state_col), (10, 10))
            if mod_label:
                mc = {"DT":(255,200,50),"NC":(255,160,80),"HT":(80,180,255)}.get(mod_label, COL_TEXT)
                screen.blit(font_md.render(mod_label, True, mc), (160, 10))
            mins = current_ms // 60000
            secs = (current_ms % 60000) // 1000
            ms   = current_ms % 1000
            t_surf = font_sm.render(f"{mins:02d}:{secs:02d}.{ms:03d}", True, COL_TEXT)
            screen.blit(t_surf, (WINDOW_W - t_surf.get_width() - 10, 10))
            fps_surf = font_sm.render(f"{fps:.0f}fps", True, (120,120,120))
            screen.blit(fps_surf, (10, WINDOW_H - fps_surf.get_height() - 6))

            pygame.display.flip()
            clock.tick_busy_loop(FPS)

        pygame.quit()

    pg_thread = threading.Thread(target=pygame_loop, daemon=True)
    pg_thread.start()

    root.bind("<Escape>", lambda e: (root.destroy(), _stop.__setitem__(0, True)))
    root.mainloop()
    _stop[0] = True


# note_times キャッシュ（bisect用、beatmap_loader_thread が更新）
note_times_cache: list = []

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
