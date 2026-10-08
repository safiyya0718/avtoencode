#!/usr/bin/env bash
# Post kodlash (kodlash botining "Post kodlash" bo'limi, `post.py` chaqiradi).
#
# `anime` repodagi `scripts/encode.sh` ("Encode (H265)", VIDEO_CODEC=h265)
# bilan AYNAN BIR XIL natija: boshida 3 soniyalik cover-rasm, keyin asosiy video, intro
# tugagach o'ng yuqori burchakda logotip. Audio — AAC, stereo, 44.1 kHz, 128k.
# Farqi faqat kirish: papka o'rniga fayl yo'llari beriladi.
#
# Ishlatilishi: encode.sh <video> <cover.png> <logo.png> <natija.mp4>
#   <video> istalgan konteyner bo'lishi mumkin (mp4, mkv, ...) — ffmpeg
#   formatni faylning o'zidan aniqlaydi.

set -uo pipefail

SRC_MAIN="${1:-}"
COVER_IMG="${2:-}"
LOGO="${3:-}"
OUTPUT="${4:-}"
if [ -z "$SRC_MAIN" ] || [ -z "$COVER_IMG" ] || [ -z "$LOGO" ] || [ -z "$OUTPUT" ]; then
    echo "::error::Ishlatilishi: encode.sh <video> <cover.png> <logo.png> <natija.mp4>"
    exit 1
fi
for f in "$SRC_MAIN" "$COVER_IMG" "$LOGO"; do
    [ -s "$f" ] || { echo "::error::Fayl topilmadi: $f"; exit 1; }
done
LABEL="$(basename "$OUTPUT" .mp4)"

w=$(ffprobe -v error -select_streams v:0 -show_entries stream=width -of csv=p=0 "$SRC_MAIN" | head -n 1 | tr -d '\r')
h=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$SRC_MAIN" | head -n 1 | tr -d '\r')
if [ -z "$w" ] || [ -z "$h" ]; then
    echo "::error::video o'lchamini aniqlab bo'lmadi"
    exit 1
fi
# libx265 / yuv420p juft o'lcham talab qiladi.
w=$(( w / 2 * 2 )); h=$(( h / 2 * 2 ))

fps_val=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of default=noprint_wrappers=1:nokey=1 "$SRC_MAIN" | head -n 1 | tr -d '\r')
if [ -z "$fps_val" ] || [ "$fps_val" = "0/0" ]; then
    fps_val=$(ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of default=noprint_wrappers=1:nokey=1 "$SRC_MAIN" | head -n 1 | tr -d '\r')
fi
{ [ -z "$fps_val" ] || [ "$fps_val" = "0/0" ]; } && fps_val="25/1"

total_sec=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$SRC_MAIN" 2>/dev/null | cut -d. -f1)
[[ "$total_sec" =~ ^[0-9]+$ ]] || total_sec=0
[ "$total_sec" -lt 1 ] && total_sec=1

# Audiosiz video ham bo'lishi mumkin — unda jimlik qo'yiladi.
if ffprobe -v error -select_streams a:0 -show_entries stream=index -of csv=p=0 "$SRC_MAIN" | grep -q .; then
    AMAIN="[0:a]"
    AEXTRA=()
else
    AMAIN="[4:a]"
    AEXTRA=(-f lavfi -t "$total_sec" -i anullsrc=r=44100:cl=stereo)
fi

echo "=== $LABEL ==="
echo "    Manba : $(basename "$SRC_MAIN") | ${w}x${h} | ${fps_val} fps | ${total_sec}s"
# H.265: sof CRF (bitrate chegarasi yo'q). Foydalanuvchi talabi: CRF har
# qanday sifatda 30 (`H265_CRF`). Katta qiymat = kichik fayl.
crf="${H265_CRF:-30}"
echo "    Kodek : H.265 (libx265, ${h}p, CRF $crf, preset ${H265_PRESET:-medium}, audio 128k)"

FILTER="[1:v]scale=$w:$h:force_original_aspect_ratio=increase,crop=$w:$h,setsar=1,fps=$fps_val[c_v];\
[0:v]scale=$w:$h,setsar=1,fps=$fps_val[main_v];\
[2:v]scale=200:-1[l];\
[c_v][3:a][main_v]${AMAIN}concat=n=2:v=1:a=1[full_v][full_a];\
[full_v][l]overlay=main_w-overlay_w-20:20:enable='gte(t,3)',format=yuv420p[out_v]"

run_progress() {
    local total_ref="$1"
    local last_ms=0 f=0 fps_now=0 br="0kbits/s" sz=0 tm="00:00:00" sp="?" us=0
    while IFS='=' read -r key value; do
        value="${value//$'\r'/}"
        case "$key" in
            frame)        f="$value" ;;
            fps)          fps_now="$value" ;;
            bitrate)      br="$value" ;;
            total_size)   sz="$value" ;;
            out_time_us)  us="$value" ;;
            out_time)     tm="${value:0:8}" ;;
            speed)        sp="$value" ;;
            progress)
                now_ms=$(date +%s%3N)
                if [ "$value" = "end" ] || [ $((now_ms - last_ms)) -ge 5000 ]; then
                    last_ms=$now_ms
                    fmt_sz=$(awk "BEGIN {printf \"%.1f\", ${sz:-0}/1048576}")
                    clean_br=$(echo "${br:-0kbits/s}" | tr -d 'kbits/s' | xargs)
                    [[ "$us" =~ ^[0-9]+$ ]] || us=0
                    pct=$(awk "BEGIN {p=(${us:-0}/1000000)/$total_ref*100; if(p>100)p=100; if(p<0)p=0; printf \"%.1f\", p}")
                    echo "🎬 [$LABEL] ${pct}% | frm:${f:-0} | vaqt:${tm:-00:00:00} | fps:${fps_now:-0} | br:${clean_br}kbps | ${fmt_sz}MB | tezlik:${sp:-?}"
                fi
                ;;
        esac
    done
}

stdbuf -oL ffmpeg -i "$SRC_MAIN" \
    -loop 1 -t 3 -i "$COVER_IMG" \
    -i "$LOGO" \
    -f lavfi -t 3 -i anullsrc=r=44100:cl=stereo \
    "${AEXTRA[@]}" \
    -filter_complex "$FILTER" \
    -map "[out_v]" -map "[full_a]" \
    -c:v libx265 -preset "${H265_PRESET:-medium}" -crf "$crf" \
    -x265-params log-level=error -tag:v hvc1 \
    -pix_fmt yuv420p -c:a aac -ac 2 -b:a 128k -ar 44100 \
    -movflags +faststart \
    -progress pipe:1 -nostats -y -loglevel error "$OUTPUT" | run_progress "$(( total_sec + 3 ))"
status=${PIPESTATUS[0]}

if [ "$status" -eq 0 ] && [ -s "$OUTPUT" ]; then
    echo ">>> $LABEL tayyor! ($(awk -v b="$(stat -c%s "$OUTPUT")" 'BEGIN {printf "%.1f", b/1048576}') MB) <<<"
    exit 0
fi
echo "::error::$LABEL uchun kodlash muvaffaqiyatsiz tugadi (ffmpeg exit=$status)"
rm -f "$OUTPUT"
exit 1
