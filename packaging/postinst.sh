#!/bin/sh
# postinst — runs after the .deb's files are unpacked.
#
# Phase 17: rewritten to support both CardputerZero and bare Pi
# Zero 2 W host configurations, install sounddevice via pip (since
# Debian Bookworm does not ship python3-sounddevice), and produce
# a system where `sudo apt install ./microjs8_X.Y.Z-1_all.deb`
# results in a daemon ready to start once station identity is set.
#
# Idempotent: safe to run on first install AND on upgrade.
#
# Steps:
#   1. Pre-create state directories (BEFORE adduser, eliminates the
#      cosmetic "Warning: home dir can't be accessed" message)
#   2. Create the microjs8 system user/group if they don't exist
#   3. Add to required supplementary groups (audio dialout video i2c input)
#   4. Set ownership on state directories (now that user exists)
#   5. Install the default config to /etc/microjs8/config.toml on first
#      install only — we never overwrite an existing edited config
#   6. pip-install the sounddevice Python module (not packaged for
#      Bookworm; required by audio/capture.py)
#   7. Symlink /usr/local/bin/microjs8-doctor for convenient
#      command-line invocation matching project conventions
#   8. systemctl daemon-reload + enable + (re)start

set -e

case "$1" in
    configure)
        # ── 1. State directories (BEFORE adduser) ────────────────────
        # Creating these first means adduser won't log the cosmetic
        # "Warning: home dir can't be accessed" message. Use temporary
        # root:root ownership; step 4 fixes ownership once the user
        # exists.
        if [ ! -d /var/lib/microjs8 ]; then
            install -d -m 0750 /var/lib/microjs8
        fi
        if [ ! -d /var/lib/microjs8/log ]; then
            install -d -m 0750 /var/lib/microjs8/log
        fi

        # ── 2. System user/group ────────────────────────────────────
        if ! getent group microjs8 >/dev/null; then
            addgroup --system microjs8
        fi
        if ! getent passwd microjs8 >/dev/null; then
            adduser --system \
                    --ingroup microjs8 \
                    --no-create-home \
                    --home /var/lib/microjs8 \
                    --gecos "MicroJS8 daemon" \
                    --shell /usr/sbin/nologin \
                    --disabled-password \
                    microjs8
        fi

        # ── 3. Supplementary groups ─────────────────────────────────
        # adduser is idempotent — adding to a group the user is
        # already in is a silent no-op. Tolerate the case where a
        # group doesn't exist (e.g. on a stripped image without
        # ALSA): we don't want the install to fail.
        #
        # Phase 18: added 'spi' and 'gpio' so the daemon can drive
        # the userspace SPI display on bare Pi installs. Both groups
        # are created by Raspberry Pi OS; they don't exist on
        # generic Debian, hence the existence check.
        for g in audio dialout video i2c input spi gpio; do
            if getent group "$g" >/dev/null 2>&1; then
                adduser microjs8 "$g" >/dev/null 2>&1 || true
            fi
        done

        # ── 4. State directory ownership ────────────────────────────
        # Now that the microjs8 user exists, chown the dirs we
        # pre-created in step 1. Recursive so any files dropped
        # by upgrades inherit correct ownership.
        chown -R microjs8:microjs8 /var/lib/microjs8

        # ── 5. Default config on first install ──────────────────────
        # The default lives at /etc/microjs8/config.toml.default
        # (read-only, shipped by this package). On first install the
        # live copy at /etc/microjs8/config.toml is created from it.
        # On upgrades the live copy is preserved untouched — operator
        # edits survive package upgrades, and the operator can diff
        # against config.toml.default to see what new options exist.
        if [ ! -d /etc/microjs8 ]; then
            install -d -m 0755 /etc/microjs8
        fi
        if [ ! -f /etc/microjs8/config.toml ] && [ -f /etc/microjs8/config.toml.default ]; then
            install -m 0644 /etc/microjs8/config.toml.default /etc/microjs8/config.toml
        fi

        # ── 6. pip-install sounddevice ──────────────────────────────
        # Phase 17: the sounddevice Python module is not packaged
        # for Debian Bookworm (verified May 2026 — apt-cache search
        # returns nothing). It's an essential dependency of
        # audio/capture.py and audio/playback.py, so we install it
        # from PyPI. The C library (libportaudio2) is already pulled
        # in as a Depends in control.
        #
        # --break-system-packages is required by Bookworm's PEP 668
        # protection. We're knowingly installing into the system
        # site-packages because:
        #   (a) we ARE the system package — the .deb is the system
        #       integration point, not a userspace venv
        #   (b) a venv would complicate the daemon's launcher and
        #       systemd unit
        #   (c) the install is idempotent ("already satisfied" is
        #       a fast no-op on upgrades)
        if ! python3 -c "import sounddevice" >/dev/null 2>&1; then
            echo "microjs8: pip-installing sounddevice (not in Bookworm apt)..."
            # Suppress pip's progress chatter but keep errors visible.
            pip install --quiet --break-system-packages sounddevice || {
                echo "microjs8: WARNING — sounddevice pip install failed" >&2
                echo "microjs8: audio path will not work until you run:" >&2
                echo "  sudo pip install --break-system-packages sounddevice" >&2
                # Don't fail the install — the daemon's audio path
                # will log a clear error at startup and the doctor
                # will surface the missing module. Operator can
                # retry the pip install manually.
            }
        fi

        # ── 7. /usr/local/bin/microjs8-doctor symlink ───────────────
        # Phase 17: the doctor is invoked as `microjs8 --doctor` (a
        # flag, not a separate binary). Operators consistently typed
        # `microjs8-doctor` expecting a real command, so we ship a
        # symlink that invokes the launcher with the --doctor flag.
        # The wrapper at /usr/local/bin handles the flag because the
        # launcher's argparse accepts it from any argv position.
        if [ ! -e /usr/local/bin/microjs8-doctor ]; then
            cat > /usr/local/bin/microjs8-doctor << 'WRAPEOF'
#!/bin/sh
# Wrapper installed by microjs8.deb (Phase 17). Invokes the daemon
# launcher with --doctor for convenient diagnostic checking from
# any shell.
exec /usr/share/APPLaunch/bin/microjs8 --doctor "$@"
WRAPEOF
            chmod 0755 /usr/local/bin/microjs8-doctor
        fi

        # ── 8. systemd ──────────────────────────────────────────────
        # Only act on systemd if we're actually running under
        # systemd (not in a chroot, container without an init, etc).
        if [ -d /run/systemd/system ]; then
            systemctl daemon-reload || true
            systemctl enable microjs8.service || true
            # Restart (not start) — handles the upgrade case where
            # the service was already running with old code.
            systemctl restart microjs8.service || true
        fi
        ;;

    abort-upgrade|abort-remove|abort-deconfigure)
        ;;

    *)
        echo "postinst called with unknown argument \`$1'" >&2
        exit 1
        ;;
esac

exit 0
