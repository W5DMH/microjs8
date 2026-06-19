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
        #
        # v0.0.8 hardening (Phase 19): the May 21, 2026 PI-2W-TEST
        # bring-up surfaced this failure mode — pip's underlying TLS
        # negotiation can fail intermittently during apt-install
        # (probably contention with apt's own network usage), and
        # the resulting WARNING was easy to miss in the install
        # log scroll. The daemon then ran without a working audio
        # path and the cause wasn't obvious.
        #
        # We now:
        #   1. Retry the pip install up to 3 times with backoff
        #      before giving up
        #   2. Explicitly verify the import works after install
        #   3. Emit a much louder warning if it didn't take, with
        #      an exact recovery command
        if ! python3 -c "import sounddevice" >/dev/null 2>&1; then
            echo "microjs8: pip-installing sounddevice (not in Bookworm apt)..."
            installed=0
            for attempt in 1 2 3; do
                if pip install --quiet --break-system-packages sounddevice 2>/dev/null; then
                    # Verify the import actually works post-install.
                    if python3 -c "import sounddevice" >/dev/null 2>&1; then
                        echo "microjs8: sounddevice installed and import verified"
                        installed=1
                        break
                    fi
                fi
                if [ "$attempt" -lt 3 ]; then
                    echo "microjs8: sounddevice install attempt ${attempt} failed; retrying in $((attempt * 2))s..."
                    sleep $((attempt * 2))
                fi
            done
            if [ "$installed" = "0" ]; then
                echo "" >&2
                echo "  ════════════════════════════════════════════════════════════════" >&2
                echo "  microjs8: ERROR — sounddevice install FAILED after 3 retries"     >&2
                echo "  ════════════════════════════════════════════════════════════════" >&2
                echo "  The microjs8 daemon's audio path WILL NOT WORK until you run:"    >&2
                echo "" >&2
                echo "    sudo pip install --break-system-packages sounddevice"           >&2
                echo "" >&2
                echo "  Then restart the daemon:"                                          >&2
                echo "    sudo systemctl restart microjs8.service"                         >&2
                echo "  ════════════════════════════════════════════════════════════════" >&2
                echo "" >&2
                # Still don't fail the install — operator can finish
                # the recovery manually. But the warning is now
                # impossible to miss in the install transcript.
            fi
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

        # ── 7.4. SPI bus auto-enable (Phase 19 / v0.0.8) ────────────
        # The Phase 18 userspace SPI display driver needs /dev/spidev0.0
        # which only appears when ``dtparam=spi=on`` is set in the Pi's
        # boot config. On a fresh Bookworm Lite install this defaults to
        # OFF, so v0.0.7 .deb installs would log "no SPI device at
        # /dev/spidev0.0" and continue headless until the operator
        # discovered they needed to enable SPI manually.
        #
        # v0.0.8 detects this case at install time and adds the line to
        # the appropriate config file. The change requires a reboot to
        # take effect (kernel rereads device-tree overlays at boot
        # only), and we log a clear notice telling the operator.
        #
        # We only touch the config when:
        #   (a) we're on a Pi (the config files exist)
        #   (b) SPI isn't already enabled (so we don't double-write)
        #   (c) the operator hasn't EXPLICITLY commented it out
        #       (``#dtparam=spi=on`` — they made a decision; we respect)
        #
        # Bookworm Pi config lives at /boot/firmware/config.txt; older
        # PiOS lives at /boot/config.txt. Check both.
        for cfg in /boot/firmware/config.txt /boot/config.txt; do
            if [ ! -w "$cfg" ]; then
                continue
            fi
            if grep -qE '^[[:space:]]*dtparam=spi=on' "$cfg"; then
                # Already enabled — nothing to do.
                continue
            fi
            if grep -qE '^[[:space:]]*#[[:space:]]*dtparam=spi=on' "$cfg"; then
                # Operator explicitly disabled it via comment — leave alone.
                echo "microjs8: SPI is commented out in $cfg; leaving as-is (operator preference)"
                continue
            fi
            # Append the line — under the [all] section if present, else at EOF.
            ts="$(date +%Y%m%d-%H%M%S)"
            cp "$cfg" "${cfg}.pre-microjs8-${ts}"
            if grep -q '^\[all\]' "$cfg"; then
                # Insert just after [all]
                awk '/^\[all\]/{print; print "dtparam=spi=on  # added by microjs8 (Phase 19 / v0.0.8)"; next}1' \
                    "$cfg" > "${cfg}.tmp" && mv "${cfg}.tmp" "$cfg"
            else
                echo "" >> "$cfg"
                echo "# Added by microjs8 (Phase 19 / v0.0.8) — required for the userspace SPI display driver." >> "$cfg"
                echo "dtparam=spi=on" >> "$cfg"
            fi
            echo "microjs8: enabled SPI in $cfg (backup: ${cfg}.pre-microjs8-${ts}) — REBOOT REQUIRED for the display"
            break
        done


        # -- 7.4b. I2C bus auto-enable (v0.0.16) ---------------------
        # The v0.0.16 I2C keyboard backend (M5Stack CardKB at 0x5F)
        # needs /dev/i2c-1 which only appears when both:
        #   (a) ``dtparam=i2c_arm=on`` is set in the Pi's boot config
        #   (b) ``i2c-dev`` kernel module is loaded
        # On a fresh Bookworm Lite install neither is true by default,
        # so v0.0.15 .deb installs would produce a working USB / UART
        # keyboard path but the CardKB would never enumerate -- the
        # operator would discover this only after wiring up the keyboard
        # and seeing nothing at /dev/i2c-1.
        #
        # We detect both gaps at install time and fix them. The
        # dtparam change requires a reboot to take effect (kernel
        # rereads device-tree overlays at boot only). The i2c-dev
        # change in /etc/modules also takes effect at next boot, but
        # we also modprobe it immediately so a "service restart"
        # without a full reboot works on already-up systems.
        #
        # We only touch the boot config when:
        #   (a) the file is writable (we're on a Pi)
        #   (b) I2C isn't already enabled
        #   (c) the operator hasn't EXPLICITLY commented it out
        #       (``#dtparam=i2c_arm=on`` -- they made a decision)
        for cfg in /boot/firmware/config.txt /boot/config.txt; do
            if [ ! -w "$cfg" ]; then
                continue
            fi
            if grep -qE '^[[:space:]]*dtparam=i2c_arm=on' "$cfg"; then
                # Already enabled -- nothing to do for this file.
                continue
            fi
            if grep -qE '^[[:space:]]*#[[:space:]]*dtparam=i2c_arm=on' "$cfg"; then
                # Operator explicitly disabled it via comment -- leave alone.
                echo "microjs8: I2C is commented out in $cfg; leaving as-is (operator preference)"
                continue
            fi
            # Append the line. Same pattern as the SPI block above.
            ts="$(date +%Y%m%d-%H%M%S)"
            cp "$cfg" "${cfg}.pre-microjs8-${ts}"
            if grep -q '^\[all\]' "$cfg"; then
                # Insert just after [all] so it applies to all Pi models.
                awk '/^\[all\]/{print; print "dtparam=i2c_arm=on  # added by microjs8 (v0.0.16) for CardKB"; next}1' \
                    "$cfg" > "${cfg}.tmp" && mv "${cfg}.tmp" "$cfg"
            else
                echo "" >> "$cfg"
                echo "# Added by microjs8 (v0.0.16) -- required for the I2C keyboard backend." >> "$cfg"
                echo "dtparam=i2c_arm=on" >> "$cfg"
            fi
            echo "microjs8: enabled I2C in $cfg (backup: ${cfg}.pre-microjs8-${ts}) -- REBOOT REQUIRED for /dev/i2c-1"
            break
        done

        # Add i2c-dev to /etc/modules so it auto-loads on every boot.
        # The kernel module i2c_bcm2835 (which we already have) is the
        # hardware controller; i2c-dev is the userspace interface that
        # creates /dev/i2c-1 as a character device. Without it, even
        # with the dtparam enabled, the bus is invisible to userspace.
        if [ -w /etc/modules ]; then
            if ! grep -qE '^[[:space:]]*i2c-dev[[:space:]]*$' /etc/modules; then
                echo "i2c-dev" >> /etc/modules
                echo "microjs8: added i2c-dev to /etc/modules"
            fi
            # Also load now so a restart without reboot works on
            # already-up systems where dtparam was previously enabled.
            modprobe i2c-dev 2>/dev/null || true
        fi

        # -- 7.4c. smbus2 pip install (v0.0.16) ----------------------
        # smbus2 is the Python library the I2C backend uses. Bookworm
        # may ship python3-smbus2 in some repos but it's not reliably
        # available, so we pip-install the same way we handle
        # sounddevice above. --break-system-packages is required by
        # Bookworm's PEP 668 protection -- we ARE the system package.
        # Same 3-retry + verify pattern as sounddevice for resilience
        # against the May 2026 pip-TLS-during-apt-install issue.
        if ! python3 -c "import smbus2" >/dev/null 2>&1; then
            echo "microjs8: pip-installing smbus2 (Python I2C library)..."
            installed=0
            for attempt in 1 2 3; do
                if pip install --quiet --break-system-packages smbus2 2>/dev/null; then
                    if python3 -c "import smbus2" >/dev/null 2>&1; then
                        echo "microjs8: smbus2 installed and import verified"
                        installed=1
                        break
                    fi
                fi
                if [ "$attempt" -lt 3 ]; then
                    echo "microjs8: smbus2 install attempt ${attempt} failed; retrying in $((attempt * 2))s..."
                    sleep $((attempt * 2))
                fi
            done
            if [ "$installed" = "0" ]; then
                echo "" >&2
                echo "  ============================================================" >&2
                echo "  microjs8: WARNING -- smbus2 install FAILED after 3 retries" >&2
                echo "  ============================================================" >&2
                echo "  The microjs8 daemon's I2C keyboard backend (CardKB) WILL" >&2
                echo "  NOT WORK until you run:" >&2
                echo "" >&2
                echo "    sudo pip install --break-system-packages smbus2" >&2
                echo "" >&2
                echo "  Then restart the daemon:" >&2
                echo "    sudo systemctl restart microjs8.service" >&2
                echo "" >&2
                echo "  USB and UART keyboards continue to work without this." >&2
                echo "  ============================================================" >&2
                echo "" >&2
                # Don't fail the install -- USB / UART paths still work
                # and the operator can complete the smbus2 install later.
            fi
        fi

        # ── 7.5. gpsd configuration (Phase 18.3) ────────────────────
        # If gpsd is installed (it's in Recommends, so usually is),
        # ensure it's NOT configured to auto-grab USB serial devices.
        # The default Debian config has USBAUTO=true, which causes
        # gpsd to open every CP210x device looking for NMEA — and
        # on CP210x chips, opening the port asserts RTS, keying the
        # radio's PTT. We saw this failure mode on PI-2W-TEST during
        # bring-up (May 20, 2026).
        #
        # We only edit if:
        #   - the file exists (gpsd is installed)
        #   - the value differs from what we want (so we don't
        #     gratuitously rewrite the file on every install)
        #
        # We back up to .pre-microjs8-X.Y.Z so the operator can
        # revert if they had a custom GPS setup we just stomped.
        if [ -r /etc/default/gpsd ]; then
            need_change=0
            if grep -q '^USBAUTO="true"' /etc/default/gpsd; then
                need_change=1
            fi
            # Also catch the empty-DEVICES case which combined with
            # USBAUTO=true means "grab anything".
            if grep -q '^DEVICES=""' /etc/default/gpsd; then
                need_change=1
            fi
            if [ "$need_change" = "1" ]; then
                ts="$(date +%Y%m%d-%H%M%S)"
                cp /etc/default/gpsd "/etc/default/gpsd.pre-microjs8-${ts}"
                sed -i 's|^USBAUTO=.*|USBAUTO="false"|' /etc/default/gpsd
                # Constrain DEVICES to the u-blox-style CDC-ACM path
                # if it's currently empty. Operators with non-default
                # GPS hardware should re-edit after install.
                if grep -q '^DEVICES=""' /etc/default/gpsd; then
                    sed -i 's|^DEVICES=.*|DEVICES="/dev/ttyACM0"|' /etc/default/gpsd
                fi
                echo "microjs8: edited /etc/default/gpsd to set USBAUTO=false (backup: /etc/default/gpsd.pre-microjs8-${ts})"
                # Restart gpsd so the new config takes effect — but only
                # if it's currently running; don't start it just to
                # restart it.
                if [ -d /run/systemd/system ] && systemctl is-active gpsd.socket >/dev/null 2>&1; then
                    systemctl restart gpsd.socket gpsd.service || true
                fi
            fi
        fi

        # ── 7.6. udev rules (Phase 18.3) ────────────────────────────
        # Reload udev rules so 99-microjs8-digirig.rules takes
        # effect immediately — operators shouldn't need to reboot
        # to get /dev/digirig and the ID_GPSD_IGNORE flag.
        if command -v udevadm >/dev/null 2>&1; then
            udevadm control --reload-rules || true
            udevadm trigger || true
        fi

        # -- 7.6b. polkit rules (v0.0.15) ----------------------------
        # The package now ships /etc/polkit-1/rules.d/50-microjs8-poweroff.rules
        # which authorizes the microjs8 user to invoke systemctl
        # poweroff --ignore-inhibitors. polkitd watches the rules
        # directory via inotify and reloads automatically when files
        # change, so this reload is purely defensive (catches the
        # edge case where polkitd isn't yet running at first install
        # or where inotify is unavailable, e.g. in some containers).
        #
        # Without the polkit rule + reload, the HOME EXIT button
        # (v0.0.13+) silently fails to power off the Pi because
        # polkit denies the systemctl call from a non-interactive
        # session. See docs/CARDPUTER_LINK.md for background.
        if [ -d /run/systemd/system ] && systemctl is-active polkit >/dev/null 2>&1; then
            systemctl reload polkit >/dev/null 2>&1 || true
        fi

        # ── 7.7. Reset any latched CP210x devices (Phase 18.3) ──────
        # If the Digirig was previously opened by gpsd or another
        # process and left in RTS-high state, the chip stays latched
        # until USB device reset. Cycle the authorized flag on every
        # CP210x device to clear any stale state from the previous
        # install state. This is best-effort: if no Digirig is
        # plugged in, the loop is a no-op.
        for cp210x in /sys/bus/usb/devices/*/idVendor; do
            if [ ! -r "$cp210x" ]; then
                continue
            fi
            if [ "$(cat "$cp210x" 2>/dev/null)" = "10c4" ]; then
                dev_dir="$(dirname "$cp210x")"
                if [ "$(cat "$dev_dir/idProduct" 2>/dev/null)" = "ea60" ]; then
                    if [ -w "$dev_dir/authorized" ]; then
                        echo 0 > "$dev_dir/authorized" 2>/dev/null || true
                        sleep 0.2
                        echo 1 > "$dev_dir/authorized" 2>/dev/null || true
                    fi
                fi
            fi
        done

        # ── 7.8. rigctld.service (Phase 18.3 / v0.0.8 update) ───────
        # Reload systemd so the unit file is known; do NOT enable it
        # to start at boot. rigctld pulls in via microjs8.service's
        # ``Wants=rigctld.service`` whenever microjs8 itself starts.
        # Auto-enabling at boot meant the launcher's "no matching
        # radio attached" exit-code-1 loop kept restarting forever
        # on systems where the operator hadn't yet completed Setup —
        # observed during the May 21, 2026 PI-2W-TEST bring-up.
        if [ -d /run/systemd/system ] && [ -f /lib/systemd/system/rigctld.service ]; then
            systemctl daemon-reload >/dev/null 2>&1 || true
        fi

        # ── 8. systemd (Phase 19 / v0.0.8 update) ───────────────────
        # Only act on systemd if we're actually running under systemd
        # (not in a chroot, container without an init, etc).
        #
        # v0.0.8 deliberately does NOT enable microjs8.service on
        # install. Both supported platforms — bare Pi Zero 2 W and
        # M5Stack CardputerZero — launch microjs8 on-demand via the
        # APPLaunch tile (which does ``systemctl start microjs8.service``).
        # This way:
        #   - Operators choose when to run a JS8 station vs. when to
        #     use the Pi for other tasks
        #   - The HOME-screen Exit button (new in v0.0.8) maps to a
        #     real "leave the app" action, not just "stop and restart
        #     next reboot"
        #   - Bare Pi installs that DO want unattended-radio behavior
        #     can opt in explicitly: ``sudo systemctl enable microjs8.service``
        #
        # We still daemon-reload + restart-if-already-running to handle
        # the upgrade case: an operator on v0.0.7 that had the service
        # enabled will keep it enabled (systemctl enable is idempotent
        # one way only — never disables what's already enabled), and a
        # running daemon needs the new code.
        if [ -d /run/systemd/system ]; then
            systemctl daemon-reload || true
            # Restart only if currently active. Don't start if it
            # wasn't running — that would surprise a fresh install
            # operator who hasn't tapped the APPLaunch tile yet.
            if systemctl is-active microjs8.service >/dev/null 2>&1; then
                systemctl restart microjs8.service || true
            fi
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
