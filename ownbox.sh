#!/usr/bin/env bash
# The whole lifecycle of hotline-call, for ownbox: setup, update, remove, status.
#
#   ./ownbox.sh setup    fresh clone -> venv, config, systemd unit, running daemon
#   ./ownbox.sh update   rebuild the venv, re-render the unit, restart if running
#   ./ownbox.sh remove   stop and remove the unit; asks before deleting config
#   ./ownbox.sh status   what is installed, and whether the daemon answers
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
        printf 'UNIT=%q\nHOST=%q\nPORT=%q\nHOTLINE_SRC_DIR=%q\n' "$UNIT" "$HOST" "$PORT" "$HOTLINE_SRC_DIR"
        printf 'EXTRA_ENV=%q\n' "$EXTRA_ENV"
    } > "$CONFIG/install.env"
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
        rm -f "$tmp"
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
    if systemctl --user cat "$UNIT" >/dev/null 2>&1; then
        systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
        rm -f "$UNIT_DIR/$UNIT.service"
        systemctl --user daemon-reload
        say "removed $UNIT"
    fi
    if [ -d "$CONFIG" ]; then
        if ask "delete $CONFIG (SIP credentials and install settings)?" n; then
            rm -rf "$CONFIG"
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
    say "checkout: $DIR"
    say "config:   $CONFIG ($( [ -f "$CONFIG/env" ] && echo 'env present' || echo 'no env'))"
    say "unit:     $UNIT ($(systemctl --user is-active "$UNIT" 2>/dev/null || true)), port $PORT, host $HOST"
    curl -s --max-time 3 "http://127.0.0.1:$PORT/health" && echo || say "daemon not answering"
    say "tests:    PYTHONPATH=src:$HOTLINE_SRC_DIR $DIR/.venv/bin/python -m pytest -q tests"
}

case "${1:-}" in
    setup) cmd_setup ;;
    update) cmd_update ;;
    remove) cmd_remove ;;
    status) cmd_status ;;
    *) echo "usage: $0 setup|update|remove|status" >&2; exit 2 ;;
esac
