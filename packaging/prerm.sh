#!/bin/sh
# prerm — runs BEFORE the .deb's files are removed (or replaced on upgrade).
#
# We stop the service so files we're about to remove aren't in use.
# We do NOT remove the user/group here — postrm purge handles that.

set -e

case "$1" in
    remove|upgrade|deconfigure)
        if [ -d /run/systemd/system ]; then
            systemctl stop microjs8.service >/dev/null 2>&1 || true
            # On upgrade we keep the unit enabled so postinst's
            # restart will bring the new version up. On remove
            # we'll disable in postrm.
            if [ "$1" = "remove" ]; then
                systemctl disable microjs8.service >/dev/null 2>&1 || true
            fi
        fi
        ;;

    failed-upgrade)
        ;;

    *)
        echo "prerm called with unknown argument \`$1'" >&2
        exit 1
        ;;
esac

exit 0
