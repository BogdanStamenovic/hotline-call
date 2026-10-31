#!/usr/bin/env bash
# The whole lifecycle of hotline-call, for ownbox: setup, update, remove, status.
#
#   ./ownbox.sh setup    fresh clone -> venv, config, systemd unit, running daemon
#   ./ownbox.sh update   rebuild the venv, re-render the unit, restart if running
#   ./ownbox.sh remove   stop and remove the unit; asks before deleting config
#   ./ownbox.sh status   what is installed, and whether the daemon answers
#   ./ownbox.sh cli ...  / fallback ...   the launchers ownbox.yaml points at
#
# Two roles, one checkout (ownbox has no notion of roles, so this script picks):
#
#   full       archserver: the call daemon, its venv (numpy, faster-whisper...)
#   fallback   pigion: hotline-call-fallback only -- system python3 plus
#              `cryptography`, no venv. Answers his calls while archserver is
#              down; see src/hotline_call/fallback.py.
#
#   HOTLINE_CALL_ROLE    full | fallback. Unset: the remembered role, else asked
#                        on a terminal, else guessed (fallback below 2 GiB RAM).
#
# Knobs, read at setup and remembered in ~/.config/hotline-call/install.env so
# that update renders the same unit again:
#
#   HOTLINE_CALL_UNIT    unit name (default hotline-calld)
#   HOTLINE_CALL_HOST    tailnet address to bind besides loopback (100.72.2.62)
#   HOTLINE_CALL_PORT    port (8792)
#   HOTLINE_SRC          hotline's source tree (~/data/hotline/src)
#   HOTLINE_CALL_ENV_FROM  where to copy credentials from on first setup
#                        (~/data/hotline-ios/.env)
#   HOTLINE_CALL_START   1 start now, 0 never, unset: decide (see should_start)
#   HOTLINE_IOS_RING, HOTLINE_IOS_INBOUND, HOTLINE_IOS_VOICE,
#   HOTLINE_IOS_ALLOW_FAKE, HOTLINE_CALL_APP_MIRROR, HOTLINE_CALL_APP_URL
#                        written into the unit as Environment= when set
#
# Fallback-role knobs (also remembered):
#
#   HOTLINE_CALL_ARCHSERVER      ssh host the credentials and clip come from
#                                (archserver)
#   HOTLINE_CALL_ARCH_DIR        hotline-call's checkout there, whose venv renders
#                                the clip (~/.local/share/ownbox/tools/hotline-call)
#   HOTLINE_CALL_FALLBACK_WOL_MAC  archserver's MAC (read over ssh when unset)
#   HOTLINE_CALL_FALLBACK_CALLD  calld's URL (http://100.72.2.62:8792)
#   HOTLINE_CALL_PYTHON          interpreter for the fallback (python3)
#
# Optional steps prompt on a TTY and take the safe default without one (ownbox
# runs agents with --yes), printing what was skipped.
set -euo pipefail

DIR="${OWNBOX_TOOL_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/hotline-call"
# The user manager reads ~/.config/systemd/user whatever XDG_CONFIG_HOME this
# shell has, so the unit path is not derived from it.
UNIT_DIR="$HOME/.config/systemd/user"
PY_VERSION=3.12   # hotline's floor, and what faster-whisper's wheels are built for

say() { printf 'hotline-call: %s\n' "$*"; }
ask() {  # ask "question" default(y|n) -> 0 for yes
    local q=$1 def=$2 reply
    if [ -t 0 ]; then
        read -r -p "hotline-call: $q [$( [ "$def" = y ] && echo Y/n || echo y/N )] " reply || reply=""
        reply=${reply:-$def}
    else
        reply=$def
        say "$q -> ${def} (no terminal; taking the default)"
    fi
    [[ $reply =~ ^[Yy] ]]
}

load_settings() {
    if [ -f "$CONFIG/install.env" ]; then
        # shellcheck disable=SC1091
        . "$CONFIG/install.env"
    fi
    UNIT="${HOTLINE_CALL_UNIT:-${UNIT:-hotline-calld}}"
    HOST="${HOTLINE_CALL_HOST:-${HOST:-100.72.2.62}}"
    PORT="${HOTLINE_CALL_PORT:-${PORT:-8792}}"
    HOTLINE_SRC_DIR="${HOTLINE_SRC:-${HOTLINE_SRC_DIR:-$HOME/data/hotline/src}}"
    EXTRA_ENV="${EXTRA_ENV:-}"
    local name
    for name in HOTLINE_IOS_RING HOTLINE_IOS_INBOUND HOTLINE_IOS_VOICE HOTLINE_IOS_ALLOW_FAKE \
                HOTLINE_CALL_APP_MIRROR HOTLINE_CALL_APP_URL; do
        if [ -n "${!name:-}" ]; then
            # A knob given now replaces the remembered one.
            EXTRA_ENV=$( { printf '%s\n' "$EXTRA_ENV" | grep -v "^$name=" || true; \
                           printf '%s=%s\n' "$name" "${!name}"; } | sed '/^$/d')
        fi
    done
    if ! printf '%s\n' "$EXTRA_ENV" | grep -q '^HOTLINE_IOS_RING='; then
        # What the live hotline-ios.service runs: SIP only.
        EXTRA_ENV=$(printf '%s\n%s\n' "$EXTRA_ENV" "HOTLINE_IOS_RING=sip" | sed '/^$/d')
    fi
}

save_settings() {
    mkdir -p "$CONFIG"
    {
        echo "# Written by hotline-call's ownbox.sh setup; read again by update."
        printf 'ROLE=%q\n' "$ROLE"
        printf 'UNIT=%q\nHOST=%q\nPORT=%q\nHOTLINE_SRC_DIR=%q\n' "$UNIT" "$HOST" "$PORT" "$HOTLINE_SRC_DIR"
        printf 'EXTRA_ENV=%q\n' "$EXTRA_ENV"
        if [ "$ROLE" = fallback ]; then
            printf 'ARCHSERVER=%q\nARCH_DIR=%q\nWOL_MAC=%q\nCALLD_URL=%q\nPYTHON=%q\n' \
                "$ARCHSERVER" "$ARCH_DIR" "$WOL_MAC" "$CALLD_URL" "$PYTHON"
        fi
    } > "$CONFIG/install.env"
}

# Which half this box runs. Asked only at setup on a terminal; an update keeps
# what setup chose, and a box set up before roles existed is a full install.
resolve_role() {  # resolve_role setup|update|other
    local guess mem_kb reply
    ROLE="${HOTLINE_CALL_ROLE:-${ROLE:-}}"
    if [ -z "$ROLE" ] && [ "$1" != setup ] && [ -f "$CONFIG/install.env" ]; then
        ROLE=full
    fi
    if [ -z "$ROLE" ]; then
        mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
        # The full role holds Whisper and a venv of several GB; nothing under
        # 2 GiB of RAM runs it, and the Pi this exists for has 415 MB.
        if [ "${mem_kb:-0}" -lt 2097152 ]; then guess=fallback; else guess=full; fi
        if [ -t 0 ]; then
            read -r -p "hotline-call: role for this box, full or fallback? [$guess] " reply || reply=""
            ROLE=${reply:-$guess}
        else
            ROLE=$guess
            say "role: $ROLE (no terminal; guessed from $((mem_kb / 1024)) MB of RAM;"
            say "  HOTLINE_CALL_ROLE=full|fallback overrides)"
        fi
    fi
    case "$ROLE" in
        full|fallback) ;;
        *) say "unknown role '$ROLE' (full or fallback)"; exit 2 ;;
    esac
    if [ "$ROLE" = fallback ]; then
        ARCHSERVER="${HOTLINE_CALL_ARCHSERVER:-${ARCHSERVER:-archserver}}"
        ARCH_DIR="${HOTLINE_CALL_ARCH_DIR:-${ARCH_DIR:-.local/share/ownbox/tools/hotline-call}}"
        WOL_MAC="${HOTLINE_CALL_FALLBACK_WOL_MAC:-${WOL_MAC:-}}"
        CALLD_URL="${HOTLINE_CALL_FALLBACK_CALLD:-${CALLD_URL:-http://100.72.2.62:8792}}"
        PYTHON="${HOTLINE_CALL_PYTHON:-${PYTHON:-python3}}"
        UNIT="${HOTLINE_CALL_UNIT:-hotline-call-fallback}"
    fi
}

build_venv() {
    command -v uv >/dev/null || { say "uv is required (https://docs.astral.sh/uv/)"; exit 1; }
    if [ ! -x "$DIR/.venv/bin/python" ]; then
        uv venv -q --python "$PY_VERSION" "$DIR/.venv"
    fi
    # dev is included so the suite can be run from an install (`status` says how).
    uv pip install -q --python "$DIR/.venv/bin/python" -e "$DIR[voice,telegram,hotline,dev]"
    say "venv ready: $("$DIR/.venv/bin/python" -V)"
}

check_neighbours() {
    if [ ! -f "$HOTLINE_SRC_DIR/hotline/httpd.py" ]; then
        say "WARNING: hotline's source is not at $HOTLINE_SRC_DIR; the daemon imports"
        say "  hotline.httpd and will not start. Set HOTLINE_SRC and run setup again."
    fi
    # faster-whisper on CUDA needs cuBLAS/cuDNN. They are not installed here (about
    # 1.5 GB); media/ears.py borrows cvoice's wheels at runtime, as hotline-ios did.
    if ! compgen -G "${HOTLINE_IOS_CUDA_LIBS:-$HOME/data/cvoice/.venv/lib/python*/site-packages}/nvidia/cublas" >/dev/null; then
        say "note: no CUDA wheels found to borrow (cvoice's venv, or HOTLINE_IOS_CUDA_LIBS)."
        say "  Whisper on cuda will fail to load; set HOTLINE_IOS_ASR_DEVICE=cpu in"
        say "  $CONFIG/env, or install cvoice."
    fi
}

write_env() {
    mkdir -p "$CONFIG"
    chmod 700 "$CONFIG"
    if [ -f "$CONFIG/env" ]; then
        say "credentials: keeping $CONFIG/env"
        return
    fi
    local from="${HOTLINE_CALL_ENV_FROM:-$HOME/data/hotline-ios/.env}"
    # Only the keys a call needs. hotline-ios's .env also carries its own store
    # settings, which mean nothing here.
    local keys='^(SIP_[A-Z_]*|TELEGRAM_[A-Z_]*|HOTLINE_API_KEY|HOTLINE_ALLOW_IPS|HOTLINE_IOS_RECORD_DIR|HOTLINE_IOS_BRIDGE_[A-Z_]*|HOTLINE_IOS_ASR_[A-Z_]*|HOTLINE_IOS_DEFAULT_ENGINE|HOTLINE_IOS_INBOUND_ENGINE|CVOICE_[A-Z_]*|ANTHROPIC_API_KEY)='
    # Copying is the default even without a terminal: the file stays on this box,
    # mode 600, and without it the daemon cannot ring anyone.
    if [ -f "$from" ] && ask "copy the call credentials from $from into $CONFIG/env?" y; then
        (umask 077; { echo "# Copied from $from by ownbox.sh setup on $(date -Iseconds)."; \
                      grep -E "$keys" "$from" || true; } > "$CONFIG/env")
        say "credentials: copied $(grep -cE "$keys" "$CONFIG/env") keys into $CONFIG/env"
    else
        (umask 077; cp "$DIR/.env.example" "$CONFIG/env")
        say "credentials: wrote a template to $CONFIG/env -- fill in SIP_USER,"
        say "  SIP_PASSWORD, SIP_PEER (and HOTLINE_API_KEY if hotline-iosd uses one)."
    fi
}

render_unit() {
    mkdir -p "$UNIT_DIR"
    local environment="" line
    while IFS= read -r line; do
        [ -n "$line" ] && environment+="Environment=$line"$'\n'
    done <<< "$EXTRA_ENV"
    local out="$UNIT_DIR/$UNIT.service" tmp
    tmp=$(mktemp)
    awk -v dir="$DIR" -v src="$HOTLINE_SRC_DIR" -v unit="$UNIT" -v host="$HOST" \
        -v port="$PORT" -v env="${environment%$'\n'}" '
        { gsub(/@DIR@/, dir); gsub(/@HOTLINE_SRC@/, src); gsub(/@UNIT@/, unit);
          gsub(/@HOST@/, host); gsub(/@PORT@/, port) }
        /^@ENVIRONMENT@$/ { if (env != "") print env; next }
        { print }' "$DIR/systemd/hotline-calld.service.in" > "$tmp"
    if [ -f "$out" ] && cmp -s "$tmp" "$out"; then
        rm -f "${tmp:?}"
        return 1   # unchanged
    fi
    mv "$tmp" "$out"
    systemctl --user daemon-reload
    say "unit: wrote $out"
    return 0
}

# Start now, or leave it for the cut-over? The one thing this must never do is
# run a second call daemon beside a hotline-iosd that still holds calls itself:
# two daemons ringing from one SIP account, and two inbound registrations
# answering the same INVITE. A hotline-iosd from after the split says where its
# call daemon is (`calld` on /health); one from before does not.
should_start() {
    case "${HOTLINE_CALL_START:-}" in
        1) return 0 ;;
        0) return 1 ;;
    esac
    local health
    health=$(curl -s --max-time 3 http://127.0.0.1:8789/health 2>/dev/null || true)
    if [ -z "$health" ]; then
        return 0     # nothing on 8789 holds calls
    fi
    if printf '%s' "$health" | grep -q '"calld"'; then
        return 0     # a split hotline-iosd, which forwards calls here
    fi
    say "NOT starting $UNIT: hotline-iosd on 8789 still holds calls itself."
    say "  At the cut-over: systemctl --user start $UNIT  (see README, 'Cut-over')."
    return 1
}

cmd_setup() {
    load_settings
    resolve_role setup
    if [ "$ROLE" = fallback ]; then fb_setup; return; fi
    build_venv
    check_neighbours
    write_env
    save_settings
    chmod +x "$DIR/systemd/wait-for-cvoiced"
    render_unit || say "unit: $UNIT_DIR/$UNIT.service already current"
    systemctl --user enable "$UNIT" >/dev/null 2>&1 && say "unit: enabled $UNIT"
    if should_start; then
        systemctl --user restart "$UNIT"
        if [ "$HOST" = 127.0.0.1 ]; then say "started $UNIT on 127.0.0.1:$PORT"
        else say "started $UNIT on 127.0.0.1:$PORT and $HOST:$PORT"; fi
        wait_healthy || true
    fi
}

cmd_update() {
    load_settings
    resolve_role update
    if [ "$ROLE" = fallback ]; then fb_update; return; fi
    build_venv
    check_neighbours
    [ -f "$CONFIG/env" ] || write_env
    save_settings
    chmod +x "$DIR/systemd/wait-for-cvoiced"
    render_unit || true
    # No data to migrate: calls are not persisted here (hotline-ios keeps the
    # record), so a rollback re-running an older update has nothing to undo.
    if systemctl --user is-active -q "$UNIT"; then
        systemctl --user restart "$UNIT"
        say "restarted $UNIT"
        wait_healthy || true
    else
        say "$UNIT is not running; not starting it (setup decides that)"
    fi
}

cmd_remove() {
    load_settings
    resolve_role remove
    if [ "$ROLE" = fallback ]; then fb_remove; return; fi
    if systemctl --user cat "$UNIT" >/dev/null 2>&1; then
        systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
        rm -f "${UNIT_DIR:?}/${UNIT:?}.service"
        systemctl --user daemon-reload
        say "removed $UNIT"
    fi
    if [ -d "$CONFIG" ]; then
        if ask "delete $CONFIG (SIP credentials and install settings)?" n; then
            rm -rf "${CONFIG:?}"
            say "deleted $CONFIG"
        else
            say "kept $CONFIG"
        fi
    fi
}

wait_healthy() {
    local i body
    for i in $(seq 1 30); do
        body=$(curl -s --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)
        if [ -n "$body" ]; then
            say "health: $body"
            return 0
        fi
        sleep 1
    done
    say "WARNING: $UNIT did not answer on 127.0.0.1:$PORT within 30 s; journalctl --user -u $UNIT"
    return 1
}

cmd_status() {
    load_settings
    resolve_role status
    if [ "$ROLE" = fallback ]; then fb_status; return; fi
    say "checkout: $DIR"
    say "config:   $CONFIG ($( [ -f "$CONFIG/env" ] && echo 'env present' || echo 'no env'))"
    say "unit:     $UNIT ($(systemctl --user is-active "$UNIT" 2>/dev/null || true)), port $PORT, host $HOST"
    curl -s --max-time 3 "http://127.0.0.1:$PORT/health" && echo || say "daemon not answering"
    say "tests:    PYTHONPATH=src:$HOTLINE_SRC_DIR $DIR/.venv/bin/python -m pytest -q tests"
}

# ---- the fallback role ---------------------------------------------------------
# Everything below runs on pigion. Nothing here builds a venv or installs a
# package: the fallback is the standard library plus `cryptography`.

FB_CLIP="${XDG_DATA_HOME:-$HOME/.local/share}/hotline-call/fallback-clip"
FB_STATE="${XDG_STATE_HOME:-$HOME/.local/state}/hotline-call"
FB_KEYS='^(SIP_USER|SIP_PASSWORD|SIP_DOMAIN|SIP_TRANSPORT|SIP_PORT|SIP_PEER|HOTLINE_API_KEY)='

fb_py() { PYTHONPATH="$DIR/src" "$PYTHON" "$@"; }
arch() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$ARCHSERVER" "$@"; }

fb_check_python() {
    if ! fb_py -c 'import sys, cryptography; assert sys.version_info >= (3, 11)' 2>/dev/null; then
        say "the fallback needs $PYTHON >= 3.11 with the cryptography package"
        say "  (Debian: python3-cryptography). Not installing system packages unasked."
        exit 1
    fi
    # The point of the role: the fallback imports without numpy or libopus.
    fb_py -c 'import sys; [sys.modules.__setitem__(m, None) for m in ("numpy", "soxr", "av")]
import hotline_call.fallback, hotline_call.ring.inbound' \
        || { say "hotline_call.fallback does not import on $PYTHON"; exit 1; }
    say "python: $("$PYTHON" -V), cryptography $(fb_py -c 'import cryptography; print(cryptography.__version__)')"
}

fb_write_env() {
    mkdir -p "$CONFIG"
    chmod 700 "$CONFIG"
    if [ -f "$CONFIG/env" ] && grep -q '^SIP_PASSWORD=.' "$CONFIG/env"; then
        say "credentials: keeping $CONFIG/env"
        return
    fi
    local tmp
    tmp=$(mktemp)
    # Only the keys a SIP registration and the callback need. NOT SIP_MEDIA_HOST:
    # archserver's is its own tailnet address; this box finds its own.
    if arch "grep -E '$FB_KEYS' ~/.config/hotline-call/env" > "$tmp" 2>/dev/null \
            && grep -q '^SIP_PASSWORD=.' "$tmp"; then
        (umask 077; { echo "# Copied from $ARCHSERVER:~/.config/hotline-call/env by ownbox.sh on $(date -Iseconds)."; \
                      cat "$tmp"; } > "$CONFIG/env")
        say "credentials: copied $(grep -c . "$tmp") keys from $ARCHSERVER"
    elif [ -t 0 ]; then
        local user password peer key
        read -r -p "hotline-call: SIP_USER (hotline's account): " user
        read -r -s -p "hotline-call: SIP_PASSWORD: " password; echo
        read -r -p "hotline-call: SIP_PEER (his account): " peer
        read -r -s -p "hotline-call: HOTLINE_API_KEY (blank if none): " key; echo
        (umask 077; printf 'SIP_USER=%s\nSIP_PASSWORD=%s\nSIP_PEER=%s\nSIP_DOMAIN=sip.linphone.org\nSIP_TRANSPORT=tls\nHOTLINE_API_KEY=%s\n' \
            "$user" "$password" "$peer" "$key" > "$CONFIG/env")
        say "credentials: written to $CONFIG/env"
    else
        (umask 077; printf '# Fill in, then: systemctl --user restart %s\nSIP_USER=\nSIP_PASSWORD=\nSIP_PEER=\nSIP_DOMAIN=sip.linphone.org\nSIP_TRANSPORT=tls\nHOTLINE_API_KEY=\n' "$UNIT" > "$CONFIG/env")
        say "WARNING: could not reach $ARCHSERVER for credentials and there is no terminal;"
        say "  wrote a template to $CONFIG/env. The fallback cannot register until it is filled."
    fi
    rm -f "${tmp:?}"
}

fb_find_mac() {
    if [ -n "$WOL_MAC" ]; then
        say "wol: archserver's MAC $WOL_MAC (remembered)"
        return
    fi
    # The NIC carrying archserver's default route: the one Wake-on-LAN has to
    # reach, and the one wake's own wol tasks target.
    # shellcheck disable=SC2016  # expanded on archserver
    WOL_MAC=$(arch 'dev=$(ip route show default | awk "{print \$5; exit}"); cat /sys/class/net/$dev/address' 2>/dev/null || true)
    if [ -n "$WOL_MAC" ]; then
        say "wol: archserver's MAC $WOL_MAC (read over ssh)"
    else
        say "WARNING: could not read archserver's MAC; the fallback cannot wake it."
        say "  Set HOTLINE_CALL_FALLBACK_WOL_MAC and run ownbox update hotline-call."
    fi
}

# Render the clip on archserver -- cvoiced is there, and so are numpy and
# libopus -- from THIS checkout's code, so the clip always matches the code
# that plays it whatever version archserver's own install is on. The source
# goes over as a tar on stdin and the clip comes back as a tar on stdout.
fb_clip() {
    local want have
    want=$(fb_py -c 'from hotline_call.fallback import CLIP_TEXT, CLIP_FORMAT; import hashlib; print(CLIP_FORMAT, hashlib.sha256(CLIP_TEXT.encode()).hexdigest()[:16])')
    have=$("$PYTHON" -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m["format"], m["text_sha"])' \
        "$FB_CLIP/manifest.json" 2>/dev/null || true)
    if [ "$want" = "$have" ] && [ "${HOTLINE_CALL_RERENDER:-0}" != 1 ]; then
        say "clip: current ($FB_CLIP)"
        return 0
    fi
    say "clip: rendering on $ARCHSERVER with cvoiced (${have:+replacing $have}${have:-none yet})"
    local tmp
    tmp=$(mktemp -d)
    # shellcheck disable=SC2016  # expanded on archserver, not here
    if tar -C "$DIR" --exclude=__pycache__ -c src | arch "set -e
        d=\$(mktemp -d); trap 'rm -rf \"\${d:?}\"' EXIT
        tar -x -C \"\$d\"
        py=\"\$HOME/$ARCH_DIR/.venv/bin/python\"
        run=\"\$py\"
        if command -v systemd-run >/dev/null; then
            run=\"systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \$py\"
        fi
        PYTHONPATH=\"\$d/src\" \$run -m hotline_call.fallback render-clip --out \"\$d/out\" >&2
        tar -c -C \"\$d/out\" ." | tar -x -C "$tmp" && [ -f "$tmp/manifest.json" ]; then
        mkdir -p "$(dirname "$FB_CLIP")"
        rm -rf "${FB_CLIP:?}.old"
        [ -d "$FB_CLIP" ] && mv "$FB_CLIP" "$FB_CLIP.old"
        mv "$tmp" "$FB_CLIP"
        rm -rf "${FB_CLIP:?}.old"
        chmod 700 "$FB_CLIP"
        say "clip: $(fb_py -c 'from hotline_call.fallback import Clip; import sys; print(Clip(__import__("pathlib").Path(sys.argv[1])).describe())' "$FB_CLIP")"
    else
        rm -rf "${tmp:?}"
        if [ -f "$FB_CLIP/manifest.json" ]; then
            say "WARNING: rendering failed; keeping the previous clip"
        else
            say "WARNING: rendering failed and there is no clip: he will be answered with"
            say "  silence. Run ownbox update hotline-call once $ARCHSERVER is up."
        fi
    fi
}

fb_render_unit() {
    mkdir -p "$UNIT_DIR"
    local environment="" line python
    python=$(command -v "$PYTHON")
    for line in "HOTLINE_CALL_FALLBACK_CALLD=$CALLD_URL" ${WOL_MAC:+"HOTLINE_CALL_FALLBACK_WOL_MAC=$WOL_MAC"}; do
        environment+="Environment=$line"$'\n'
    done
    while IFS= read -r line; do
        case "$line" in HOTLINE_CALL_FALLBACK_*) environment+="Environment=$line"$'\n' ;; esac
    done <<< "$EXTRA_ENV"
    local out="$UNIT_DIR/$UNIT.service" tmp
    tmp=$(mktemp)
    awk -v dir="$DIR" -v py="$python" -v env="${environment%$'\n'}" '
        { gsub(/@DIR@/, dir); gsub(/@PYTHON@/, py) }
        /^@ENVIRONMENT@$/ { if (env != "") print env; next }
        { print }' "$DIR/systemd/hotline-call-fallback.service.in" > "$tmp"
    if [ -f "$out" ] && cmp -s "$tmp" "$out"; then
        rm -f "${tmp:?}"
        return 1
    fi
    mv "$tmp" "$out"
    systemctl --user daemon-reload
    say "unit: wrote $out"
}

fb_wait() {
    local _
    for _ in $(seq 1 15); do
        if systemctl --user is-active -q "$UNIT"; then
            sleep 2
            systemctl --user is-active -q "$UNIT" && { say "$UNIT is running"; return 0; }
        fi
        sleep 1
    done
    say "WARNING: $UNIT is not running; journalctl --user -u $UNIT"
    return 1
}

fb_setup() {
    fb_check_python
    fb_write_env
    fb_find_mac
    save_settings
    fb_clip
    fb_render_unit || say "unit: $UNIT_DIR/$UNIT.service already current"
    systemctl --user enable "$UNIT" >/dev/null 2>&1 && say "unit: enabled $UNIT"
    systemctl --user restart "$UNIT"
    fb_wait || true
}

fb_update() {
    fb_check_python
    [ -f "$CONFIG/env" ] || fb_write_env
    fb_find_mac
    save_settings
    # The clip and the callback state are data: the clip is re-rendered only
    # when the text or its format changed (and kept if that fails), the state
    # file is never touched. An older revision's update after a rollback finds
    # both in a shape it reads, because neither format has changed yet; a
    # future format bump must keep reading format 1.
    fb_clip
    fb_render_unit || true
    if systemctl --user is-enabled -q "$UNIT" 2>/dev/null; then
        systemctl --user restart "$UNIT"
        say "restarted $UNIT"
        fb_wait || true
    else
        say "$UNIT is not enabled; not starting it (setup does that)"
    fi
}

fb_remove() {
    if systemctl --user cat "$UNIT" >/dev/null 2>&1; then
        # Stopping it drops its SIP registration (REGISTER Expires: 0).
        systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
        rm -f "${UNIT_DIR:?}/${UNIT:?}.service"
        systemctl --user daemon-reload
        say "removed $UNIT"
    fi
    if [ -d "$FB_CLIP" ] || [ -d "$FB_STATE" ]; then
        if ask "delete the clip ($FB_CLIP, his cloned voice) and the callback state ($FB_STATE)?" n; then
            rm -rf "${FB_CLIP:?}" "${FB_STATE:?}"
            say "deleted the clip and the state"
        else
            say "kept $FB_CLIP and $FB_STATE"
        fi
    fi
    if [ -d "$CONFIG" ]; then
        if ask "delete $CONFIG (SIP credentials and install settings)?" n; then
            rm -rf "${CONFIG:?}"
            say "deleted $CONFIG"
        else
            say "kept $CONFIG"
        fi
    fi
}

fb_status() {
    say "role:     fallback (checkout $DIR)"
    say "unit:     $UNIT ($(systemctl --user is-active "$UNIT" 2>/dev/null || true))"
    fb_py -m hotline_call.fallback status
    say "tests:    PYTHONPATH=src $PYTHON -m pytest -q tests/test_fallback.py (pytest not installed here)"
}

# Launchers. Neither touches settings, so neither ever prompts.
cmd_cli() {
    if [ -x "$DIR/.venv/bin/hotline-call" ]; then
        exec "$DIR/.venv/bin/hotline-call" "$@"
    fi
    PYTHONPATH="$DIR/src" exec python3 -m hotline_call.call_cli "$@"
}

cmd_fallback() {
    local py=python3
    [ -x "$DIR/.venv/bin/python" ] && py="$DIR/.venv/bin/python"
    PYTHONPATH="$DIR/src" exec "$py" -m hotline_call.fallback "$@"
}

case "${1:-}" in
    setup) cmd_setup ;;
    update) cmd_update ;;
    remove) cmd_remove ;;
    status) cmd_status ;;
    cli) shift; cmd_cli "$@" ;;
    fallback) shift; cmd_fallback "$@" ;;
    *) echo "usage: $0 setup|update|remove|status|cli|fallback" >&2; exit 2 ;;
esac
