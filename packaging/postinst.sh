#!/bin/sh
# postinst — runs after the .deb's files are unpacked.
#
# Idempotent: safe to run on first install AND on upgrade.
#
# Steps:
#   1. Create the microjs8 system user/group if they don't exist
#   2. Add to required supplementary groups (audio dialout video i2c input)
#   3. Create /var/lib/microjs8 state directory
#   4. Install the default config to /etc/microjs8/config.toml on first
#      install only — we never overwrite an existing edited config
#   5. systemctl daemon-reload + enable + start

set -e

case "$1" in
    configure)
        # ── 1. System user/group ────────────────────────────────────
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

        # ── 2. Supplementary groups ─────────────────────────────────
        # adduser is idempotent — adding to a group the user is
        # already in is a silent no-op. Tolerate the case where a
        # group doesn't exist (e.g. on a stripped image without
        # ALSA): we don't want the install to fail.
        for g in audio dialout video i2c input; do
            if getent group "$g" >/dev/null 2>&1; then
                adduser microjs8 "$g" >/dev/null 2>&1 || true
            fi
        done

        # ── 3. State directory ──────────────────────────────────────
        # Owned by microjs8:microjs8, mode 0750 so only the daemon
        # and root can read it (mailbox.db has unread message
        # contents — better not world-readable).
        if [ ! -d /var/lib/microjs8 ]; then
            install -d -o microjs8 -g microjs8 -m 0750 /var/lib/microjs8
        else
            chown microjs8:microjs8 /var/lib/microjs8 || true
        fi

        # ── 4. Default config on first install ──────────────────────
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

        # ── 5. systemd ──────────────────────────────────────────────
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
