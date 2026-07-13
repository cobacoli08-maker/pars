# ============================================================
# app.py — Karaoke MIDI Processing Server v7.0
# "Full Expression" Track-Header JSON Format (V2 wire, additive)
#
# WHAT'S NEW vs v6.0 (mid12.py):
#   The old parser only emitted N (note), B (pitch bend), X (expression).
#   It THREW AWAY sustain, modulation, aftertouch, attack/release, and it
#   only stored volume/pan/reverb as a single STATIC header value.
#   v7 keeps 100% backward compatibility (wire "version" stays 2, decoder
#   silently ignores event chars it doesn't know) but now ALSO streams:
#     S = Sustain pedal   (CC64)   0/127, on-change only
#     M = Modulation      (CC1)    0-127  → vibrato depth
#     V = Volume          (CC7)    0-127  time-series (was static header only)
#     P = Pan             (CC10)   0-127  time-series (was static header only)
#     E = Reverb send     (CC91)   0-127  time-series (was static header only)
#     C = Chorus send     (CC93)   0-127  time-series (NEW)
#     F = Brightness/Cutoff (CC74) 0-127  → filter timbre
#     A = Attack time     (CC73)   0-127  (client already consumes A!)
#     R = Release time    (CC72)   0-127  (client already consumes R!)
#     D = Channel aftertouch       0-127  → pressure → vol/vibrato
#   Existing N / B / X are emitted byte-for-byte identically.
#   Headers p/v/r kept as INITIAL values (back-compat); added c = initial chorus.
#
# Install: pip install flask mido requests
# Run:     python app.py
# ============================================================

from flask import Flask, request, Response
import mido
import requests
import io
import json
import traceback
import re
import os

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════
# DOWNLOAD
# ═══════════════════════════════════════════════════════════════


def download_midi(url: str) -> bytes:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    print(f"[MIDI] Downloading: {url[:120]}...")
    try:
        response = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    except requests.exceptions.Timeout:
        raise ValueError("Download timed out after 30 seconds")
    except requests.exceptions.ConnectionError as e:
        raise ValueError(f"Connection failed: {str(e)}")

    print(f"[MIDI] HTTP {response.status_code} | {len(response.content)} bytes")

    if response.status_code == 403:
        raise ValueError(
            "Discord returned 403 — CDN link expired, re-copy from Discord."
        )
    if response.status_code == 404:
        raise ValueError("File not found (404). Check the URL.")

    response.raise_for_status()
    content = response.content

    if len(content) < 4:
        raise ValueError(f"Response too small ({len(content)} bytes)")
    if content[:4] != b"MThd":
        raise ValueError(f"Not a valid MIDI file. First bytes: {content[:8].hex()}")

    print(f"[MIDI] Valid MIDI confirmed")
    return content


# ═══════════════════════════════════════════════════════════════
# TEMPO MAP
# ═══════════════════════════════════════════════════════════════


def build_tempo_map(mid):
    ticks_per_beat = mid.ticks_per_beat
    raw_tempos = []
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "set_tempo":
                raw_tempos.append((abs_tick, msg.tempo))

    raw_tempos.sort(key=lambda x: x[0])
    deduped = {}
    for tick, tempo in raw_tempos:
        deduped[tick] = tempo
    raw_tempos = sorted(deduped.items())

    cur_tempo = 500000
    cur_tick = 0
    cur_second = 0.0
    tempo_map = [(0, 0.0, cur_tempo)]

    for abs_tick, tempo in raw_tempos:
        elapsed = mido.tick2second(abs_tick - cur_tick, ticks_per_beat, cur_tempo)
        cur_second += elapsed
        cur_tick = abs_tick
        cur_tempo = tempo
        tempo_map.append((abs_tick, cur_second, cur_tempo))

    return tempo_map, ticks_per_beat


def ticks_to_seconds(abs_tick, tempo_map, ticks_per_beat):
    last_entry = tempo_map[0]
    for entry in tempo_map:
        if entry[0] <= abs_tick:
            last_entry = entry
        else:
            break
    base_tick, base_second, tempo = last_entry
    return base_second + mido.tick2second(abs_tick - base_tick, ticks_per_beat, tempo)


def get_true_duration(mid, tempo_map, ticks_per_beat):
    max_ticks = 0
    for track in mid.tracks:
        track_ticks = sum(msg.time for msg in track)
        if track_ticks > max_ticks:
            max_ticks = track_ticks
    return ticks_to_seconds(max_ticks, tempo_map, ticks_per_beat)


# ═══════════════════════════════════════════════════════════════
# DYNAMICS CONFIG — decimation thresholds keep payload small
# ═══════════════════════════════════════════════════════════════

# Minimum change (in 0-127 units) required to record a new event of each type.
DYN_THRESHOLD = {
    "X": 4,   # expression CC11
    "V": 2,   # volume CC7
    "P": 3,   # pan CC10
    "M": 4,   # modulation CC1
    "E": 4,   # reverb send CC91
    "C": 4,   # chorus send CC93
    "F": 4,   # brightness/cutoff CC74
    "A": 4,   # attack CC73
    "R": 4,   # release CC72
    "D": 4,   # channel aftertouch
}
# Types that should also always record at the 0 / 127 extremes.
DYN_RECORD_EXTREMES = set(DYN_THRESHOLD.keys())

# Which CC number maps to which event char (7-bit CCs only).
CC_TO_EVENT = {
    1:  "M",   # modulation
    7:  "V",   # channel volume
    10: "P",   # pan
    11: "X",   # expression
    64: "S",   # sustain pedal
    72: "R",   # release time
    73: "A",   # attack time
    74: "F",   # brightness / filter cutoff
    91: "E",   # reverb send
    93: "C",   # chorus send
}

SILENCE_THRESHOLD = 1.5  # trim leading silence longer than this
TARGET_SILENCE_SEC = 1.5  # after trim, song begins at this offset


# ═══════════════════════════════════════════════════════════════
# LYRICS — unchanged from v6.0
# ═══════════════════════════════════════════════════════════════


def _fix_text(s):
    if not s:
        return ""
    try:
        b = s.encode("latin-1")
    except Exception:
        return s
    for enc in ("utf-8", "shift_jis", "cp932", "euc-jp"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return s


def extract_lyrics(mid, tempo_map, ticks_per_beat, offset_sec):
    raw = []  # (t, track_idx, type, text)
    has_lyric = False
    for track_idx, track in enumerate(mid.tracks):
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.is_meta and msg.type in ("lyrics", "text"):
                if msg.type == "lyrics":
                    has_lyric = True
                t = ticks_to_seconds(abs_tick, tempo_map, ticks_per_beat)
                raw.append((t, track_idx, msg.type, msg.text or ""))
    if not raw:
        return []

    text_blob = "".join(x[3] for x in raw if x[2] == "text")
    is_soft_karaoke = "@" in text_blob

    if has_lyric:
        wanted = "lyrics"
    elif is_soft_karaoke:
        wanted = "text"
    else:
        return []

    lyric_tracks = sorted({tr for (t, tr, ty, txt) in raw if ty == wanted})
    track_rank = {tr: i + 1 for i, tr in enumerate(lyric_tracks)}
    print(f"[MIDI] Lyric tracks={len(lyric_tracks)} -> {track_rank}")

    out = []
    for tr in lyric_tracks:
        events = [(t, txt) for (t, etr, ty, txt) in raw if ty == wanted and etr == tr]
        events.sort(key=lambda e: e[0])
        tnum = track_rank[tr]

        pending_nl = False
        for t, txt in events:
            txt = _fix_text(txt)
            if txt.startswith("@"):
                continue
            nl = pending_nl
            pending_nl = False

            mt = re.match(r"^\s*\[\s*[Tt]\s*:?\s*(\d+)\s*\]\s*", txt)
            this_tnum = tnum
            if mt:
                this_tnum = int(mt.group(1))
                txt = txt[mt.end():]

            while txt[:1] in ("\\", "/"):
                nl = True
                txt = txt[1:]
            if txt.endswith("\n") or txt.endswith("\r"):
                pending_nl = True
            clean = txt.replace("\r", "").replace("\n", "").replace("\x00", "").strip()
            if clean == "":
                if nl:
                    pending_nl = True
                continue
            t_adj = round(t - offset_sec, 3)
            if t_adj < 0:
                t_adj = 0.0
            out.append({"t": t_adj, "text": clean, "nl": nl, "tr": this_tnum})

    out.sort(key=lambda e: e["t"])
    return out


# ═══════════════════════════════════════════════════════════════
# PARSE — v7 Full Expression
# ═══════════════════════════════════════════════════════════════


def parse_midi(midi_bytes: bytes) -> dict:
    mid = mido.MidiFile(file=io.BytesIO(midi_bytes))
    ticks_per_beat = mid.ticks_per_beat
    tempo_map, _ = build_tempo_map(mid)

    print(f"[MIDI] TPB={ticks_per_beat} | TempoChanges={len(tempo_map)}")

    # ── Stage 1: first note_on for silence detection ──
    first_note_time = None
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                t = ticks_to_seconds(abs_tick, tempo_map, ticks_per_beat)
                if first_note_time is None or t < first_note_time:
                    first_note_time = t

    # ── Stage 2: silence trim offset ──
    offset_sec = 0.0
    if first_note_time is not None and first_note_time > SILENCE_THRESHOLD:
        offset_sec = round(first_note_time - TARGET_SILENCE_SEC, 3)
        print(
            f"[MIDI] Auto-trim: first note @{first_note_time:.3f}s -> offset={offset_sec:.3f}s"
        )

    # ── Stage 3: full parse pass ──
    channel_cc = {}

    def get_cc(ch):
        if ch not in channel_cc:
            channel_cc[ch] = {"vol": 100, "pan": 64, "rev": 0, "cho": 0}
        return channel_cc[ch]

    channel_programs = {}
    active_notes = {}
    raw_notes = []
    channel_dynamics = {}  # ch -> [(abs_time, type_char, value)]
    sustain_state = {}     # ch -> bool (last emitted sustain state)

    def add_dyn(ch, t, tc, val):
        channel_dynamics.setdefault(ch, []).append((t, tc, val))

    for track_idx, track in enumerate(mid.tracks):
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            t = ticks_to_seconds(abs_tick, tempo_map, ticks_per_beat)

            if msg.type == "program_change":
                channel_programs[msg.channel] = msg.program + 1
                print(
                    f"[MIDI] Track {track_idx} Ch {msg.channel+1} -> GM {msg.program+1}"
                )

            elif msg.type == "pitchwheel":
                add_dyn(msg.channel, t, "B", msg.pitch)

            elif msg.type == "aftertouch":
                # channel pressure -> D
                add_dyn(msg.channel, t, "D", msg.value)

            elif msg.type == "control_change":
                cc = get_cc(msg.channel)
                ctrl = msg.control
                val = msg.value

                # keep initial header values (back-compat)
                if ctrl == 7:
                    cc["vol"] = val
                elif ctrl == 10:
                    cc["pan"] = val
                elif ctrl == 91:
                    cc["rev"] = val
                elif ctrl == 93:
                    cc["cho"] = val

                # time-series streams
                if ctrl == 64:
                    # sustain pedal: emit only on state change (>=64 = down)
                    down = val >= 64
                    if sustain_state.get(msg.channel) != down:
                        sustain_state[msg.channel] = down
                        add_dyn(msg.channel, t, "S", 127 if down else 0)
                else:
                    tc = CC_TO_EVENT.get(ctrl)
                    if tc is not None:
                        add_dyn(msg.channel, t, tc, val)

            elif msg.type in ("note_on", "note_off"):
                channel = msg.channel
                pitch = msg.note
                velocity = msg.velocity
                key = (channel, pitch)
                is_on = msg.type == "note_on" and velocity > 0

                if is_on:
                    active_notes[key] = (t, velocity, channel)
                else:
                    if key in active_notes:
                        note_start, note_vel, note_ch = active_notes.pop(key)
                        note_dur = max(t - note_start, 0.05)
                        is_drum = note_ch == 9
                        gm_prog = 0 if is_drum else channel_programs.get(note_ch, 1)
                        raw_notes.append(
                            (note_ch, gm_prog, pitch, note_start, note_vel, note_dur)
                        )

    for (channel, pitch), (note_start, note_vel, note_ch) in active_notes.items():
        is_drum = note_ch == 9
        gm_prog = 0 if is_drum else channel_programs.get(note_ch, 1)
        raw_notes.append((note_ch, gm_prog, pitch, note_start, note_vel, 0.3))

    raw_notes.sort(key=lambda n: n[3])

    # ── Stage 4: duration & BPM ──
    true_dur = get_true_duration(mid, tempo_map, ticks_per_beat)
    note_end = max((n[3] + n[5] for n in raw_notes), default=0) + 0.5
    duration = round(max(true_dur, note_end), 3)
    adj_duration = round(max(duration - offset_sec, 0.1), 3)

    if len(tempo_map) <= 1:
        primary_tempo = tempo_map[0][2]
    else:
        best_tempo = tempo_map[0][2]
        best_coverage = 0.0
        for i, (tick, sec, tempo) in enumerate(tempo_map):
            seg_end = tempo_map[i + 1][1] if i + 1 < len(tempo_map) else duration
            coverage = seg_end - sec
            if coverage > best_coverage:
                best_coverage = coverage
                best_tempo = tempo
        primary_tempo = best_tempo
    bpm = round(60_000_000 / primary_tempo) if primary_tempo > 0 else 120

    # ── Stage 5: group notes by track, build event strings ──
    track_note_events = {}
    track_headers = {}

    for ch, gm_prog, pitch, time_sec, vel, dur in raw_notes:
        ch1 = ch + 1
        track_key = f"{ch1}_{gm_prog}"
        if track_key not in track_note_events:
            cc = get_cc(ch)
            track_headers[track_key] = {
                "p": cc["pan"], "v": cc["vol"], "r": cc["rev"], "c": cc["cho"],
            }
            track_note_events[track_key] = []
        t_adj = round(time_sec - offset_sec, 3)
        if t_adj >= -0.001:
            track_note_events[track_key].append(
                (max(t_adj, 0.0), "N", int(pitch), int(vel), round(dur, 3))
            )

    tracks_out = {}
    total_notes = 0
    total_dyn = 0

    for track_key, note_events in track_note_events.items():
        ch1_str, _ = track_key.split("_")
        ch = int(ch1_str) - 1  # 0-based

        dyn_raw = []
        for t_abs, tc, val in channel_dynamics.get(ch, []):
            t_adj = round(t_abs - offset_sec, 3)
            if t_adj >= 0:
                dyn_raw.append((t_adj, tc, val))

        # decimate per type
        dyn_events = []
        last_dyn_val = {}
        for t_adj, tc, val in dyn_raw:
            last_val = last_dyn_val.get(tc, None)
            if tc == "B":
                should_record = (
                    last_val is None or abs(val - last_val) >= 250 or val == 0
                )
            elif tc == "S":
                # sustain already change-filtered at parse time
                should_record = (last_val is None or val != last_val)
            else:
                thr = DYN_THRESHOLD.get(tc, 4)
                should_record = (
                    last_val is None
                    or abs(val - last_val) >= thr
                    or (val in (0, 127) and tc in DYN_RECORD_EXTREMES)
                )
            if should_record:
                dyn_events.append((t_adj, tc, val))
                last_dyn_val[tc] = val

        all_events = list(note_events)
        for t, tc, val in dyn_events:
            all_events.append((t, tc, val))
        all_events.sort(key=lambda e: e[0])

        prev_time = 0.0
        parts = []
        for event in all_events:
            t_ev = event[0]
            etype = event[1]
            t_ev_rounded = round(t_ev, 3)
            delta = round(t_ev_rounded - prev_time, 3)
            if delta < 0:
                delta = 0.0
            prev_time = t_ev_rounded

            if etype == "N":
                pitch, vel, dur = event[2], event[3], event[4]
                parts.append(f"N,{delta},{pitch},{vel},{dur}")
                total_notes += 1
            else:
                # generic single-value event: B/X/A/R/M/S/V/P/E/C/F/D
                parts.append(f"{etype},{delta},{event[2]}")
                total_dyn += 1

        hdr = track_headers[track_key]
        tracks_out[track_key] = {
            "p": hdr["p"],
            "v": hdr["v"],
            "r": hdr["r"],
            "c": hdr["c"],
            "n": "|".join(parts),
        }

    print(
        f"[MIDI] v7 Notes={total_notes} | Dyn={total_dyn} | BPM={bpm} "
        f"| Duration={adj_duration}s | Tracks={len(tracks_out)} | Offset={offset_sec}s"
    )

    lyrics = extract_lyrics(mid, tempo_map, ticks_per_beat, offset_sec)

    return {
        "version": 2,  # wire version stays 2 (additive, decoder-compatible)
        "_lyrics": lyrics,
        "_bpm": bpm,
        "_duration": adj_duration,
        "_tracks": tracks_out,
        "_offset_sec": offset_sec,
    }


# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════


def minified_json(data):
    return Response(
        json.dumps(data, separators=(",", ":")), status=200, mimetype="application/json"
    )


def minified_json_err(data, status=400):
    return Response(
        json.dumps(data, separators=(",", ":")),
        status=status,
        mimetype="application/json",
    )


@app.route("/", methods=["GET"])
def index():
    return minified_json(
        {
            "status": "ok",
            "service": "Karaoke MIDI Parser",
            "version": "7.0",
            "format": "V2-FullExpression",
            "endpoints": {
                "POST /parse-midi": "Parse MIDI -> Track-Header JSON",
                "POST /convert": "Alias for /parse-midi",
                "GET  /health": "Health check",
            },
        }
    )


@app.route("/health", methods=["GET"])
def health():
    return minified_json({"status": "ok", "version": "7.0"})


@app.route("/parse-midi", methods=["POST"])
def parse_midi_endpoint():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return minified_json_err({"error": "Request body must be JSON"}, 400)

        url = data.get("url", "").strip()
        if not url:
            return minified_json_err({"error": "Missing 'url' field"}, 400)

        song_name = data.get("name", url.split("/")[-1].split("?")[0])

        print(f"\n[MIDI] === New Request ===")
        print(f"[MIDI] URL: {url[:120]}")

        try:
            midi_bytes = download_midi(url)
        except ValueError as e:
            return minified_json_err({"error": str(e)}, 400)
        except Exception as e:
            return minified_json_err({"error": f"Download failed: {str(e)}"}, 400)

        try:
            result = parse_midi(midi_bytes)
        except Exception as e:
            print(f"[MIDI] Parse error:\n{traceback.format_exc()}")
            return minified_json_err({"error": f"Parse failed: {str(e)}"}, 500)

        response_data = {
            "version": result.get("version", 2),
            "Metadata": {
                "Name": song_name,
                "BPM": result["_bpm"],
                "Duration": result["_duration"],
            },
            "Tracks": result["_tracks"],
        }
        if result.get("_offset_sec", 0) > 0:
            response_data["Metadata"]["Offset"] = result["_offset_sec"]
        if result.get("_lyrics"):
            response_data["Lyrics"] = result["_lyrics"]

        print(
            f"[MIDI] Response ready: {len(result['_tracks'])} tracks | offset={result.get('_offset_sec',0):.3f}s"
        )
        return minified_json(response_data)

    except Exception as e:
        print(f"[MIDI] FATAL:\n{traceback.format_exc()}")
        return minified_json_err({"error": f"Server error: {str(e)}"}, 500)


@app.route("/convert", methods=["POST"])
def convert_legacy():
    return parse_midi_endpoint()


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Karaoke MIDI Parser v7.0 — Full Expression")
    print("  Sustain | Modulation | Aftertouch | Vol/Pan/Reverb/Chorus")
    print("  Attack/Release | Brightness | (V2 wire, backward-compatible)")
    print("=" * 60)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
