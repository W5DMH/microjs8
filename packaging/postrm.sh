#!/bin/sh
# postrm — runs AFTER the .deb's files are removed.
#
# On 'remove' we leave the user, /etc/microjs8, and /var/lib/microjs8
# in place — a subsequent reinstall preserves the operator's config
# and mailbox. On 'purge' we tear everything down.

set -e

case "$1" in
    purge)
        if [ -d /run/systemd/system ]; then
            systemctl daemon-reload >/dev/null 2>&1 || true
        fi

        # Remove operator data — explicit purge intent.
        rm -rf /var/lib/microjs8
        rm -rf /etc/microjs8

        # Remove the system user. deluser is from the 'adduser'
        # package, which we Depend on. It refuses to remove a user
        # who's still logged in (won't happen for a system user),
        # and is a no-op if the user doesn't exist.
        if getent passwd microjs8 >/dev/null 2>&1; then
            deluser --system microjs8 >/dev/null 2>&1 || true
        fi
        if getent group microjs8 >/dev/null 2>&1; then
            delgroup --system microjs8 >/dev/null 2>&1 || true
        fi
        ;;

    remove|upgrade|failed-upgrade|abort-install|abort-upgrade|disappear)
        ;;

    *)
        echo "postrm called with unknown argument \`$1'" >&2
        exit 1
        ;;
esac

exit 0
