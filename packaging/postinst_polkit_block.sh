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
