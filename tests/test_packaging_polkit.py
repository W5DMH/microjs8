"""Tests for v0.0.15 polkit rule packaging.

The polkit rule at polkit/50-microjs8-poweroff.rules authorizes the
microjs8 user to invoke 'systemctl poweroff --ignore-inhibitors'.
Pre-v0.0.15 the rule was a manual operator install (see
docs/CARDPUTER_LINK.md history). v0.0.15 ships it inside the .deb
so a fresh install needs zero manual polkit setup.

These tests verify three properties:
  1. The source file polkit/50-microjs8-poweroff.rules exists in
     the repo (catches accidental removal).
  2. The rule has the required polkit actions (catches partial
     edits that would silently re-introduce the v0.0.13 bug).
  3. The build_deb.py script wires it into the staging tree at
     etc/polkit-1/rules.d/ (catches build-script regressions).

We don't try to actually build a .deb here -- that needs root and
dpkg-deb. The unit-level checks above catch the failure modes that
would prevent the rule from ending up where it belongs.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# Source-of-truth paths relative to the repo root. Resolved from the
# test file location so the tests work regardless of pytest's cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
POLKIT_RULE = REPO_ROOT / "polkit" / "50-microjs8-poweroff.rules"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_deb.py"


# The full set of polkit action IDs the rule MUST authorize. If any
# of these is missing the daemon's systemctl_poweroff() call will
# be silently denied (lesson learned the hard way in v0.0.13/v0.0.14).
REQUIRED_ACTIONS = (
    "org.freedesktop.login1.power-off",
    "org.freedesktop.login1.power-off-multiple-sessions",
    "org.freedesktop.login1.power-off-ignore-inhibit",
    "org.freedesktop.login1.reboot",
    "org.freedesktop.login1.reboot-multiple-sessions",
    "org.freedesktop.login1.reboot-ignore-inhibit",
    "org.freedesktop.login1.halt",
    "org.freedesktop.login1.halt-multiple-sessions",
    "org.freedesktop.login1.halt-ignore-inhibit",
)


class TestPolkitRuleSource:
    """The repo contains the polkit rule with the expected shape."""

    def test_polkit_rule_file_exists(self) -> None:
        assert POLKIT_RULE.exists(), (
            f"polkit rule file missing at {POLKIT_RULE}; the .deb "
            "will not authorize HOME EXIT button poweroff if this "
            "file is absent at build time"
        )

    def test_polkit_rule_targets_microjs8_user(self) -> None:
        # Defensive: the rule must scope authorization to the
        # microjs8 user, not all users. Catches a copy-paste of
        # the wrong subject filter.
        content = POLKIT_RULE.read_text(encoding="utf-8")
        assert 'subject.user === "microjs8"' in content, (
            "polkit rule must scope authorization to subject.user "
            '=== "microjs8"; got file content without that check'
        )

    def test_polkit_rule_returns_yes(self) -> None:
        content = POLKIT_RULE.read_text(encoding="utf-8")
        assert "polkit.Result.YES" in content, (
            "polkit rule must return polkit.Result.YES to grant "
            "authorization; the rule appears to deny or be incomplete"
        )

    @pytest.mark.parametrize("action_id", REQUIRED_ACTIONS)
    def test_required_action_present(self, action_id: str) -> None:
        # Every action the helper might trigger must be authorized.
        # Missing power-off-ignore-inhibit was the v0.0.14 bug --
        # this parametrized test catches the same class of error
        # at build time.
        content = POLKIT_RULE.read_text(encoding="utf-8")
        assert f'"{action_id}"' in content, (
            f"polkit rule is missing required action: {action_id}. "
            "Without this action authorized, the daemon's "
            "systemctl_poweroff call path will be silently denied "
            "by polkit at runtime."
        )

    def test_polkit_rule_is_ascii(self) -> None:
        # The v0.0.14 development cycle was bitten by a paste-encoding
        # mishap where em-dashes became invalid bytes. The shipped
        # rule is ASCII-only by policy so it round-trips cleanly
        # through any text editor or pipeline.
        raw = POLKIT_RULE.read_bytes()
        non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
        assert not non_ascii, (
            f"polkit rule contains {len(non_ascii)} non-ASCII bytes "
            f"(first at offset {non_ascii[0][0]} = 0x{non_ascii[0][1]:02x}); "
            "rule must be pure ASCII to survive paste/encoding "
            "round-trips per the v0.0.14 incident report"
        )


class TestBuildScriptInstallsPolkitRule:
    """build_deb.py wires the polkit rule into the staging tree."""

    def test_build_script_references_polkit_path(self) -> None:
        # Catches accidental rename / removal of the install block.
        content = BUILD_SCRIPT.read_text(encoding="utf-8")
        assert "POLKIT_RULES_PATH" in content, (
            "build_deb.py must define POLKIT_RULES_PATH constant; "
            "the polkit rule won't be staged into the .deb without it"
        )
        assert "etc/polkit-1/rules.d" in content, (
            "build_deb.py must target etc/polkit-1/rules.d as the "
            "polkit install path; that's where polkitd looks for "
            "JavaScript rule files on Debian"
        )

    def test_build_script_reads_polkit_source(self) -> None:
        content = BUILD_SCRIPT.read_text(encoding="utf-8")
        assert '"polkit"' in content or "'polkit'" in content, (
            "build_deb.py must read from a 'polkit' subdirectory "
            "of the repo root; otherwise the rule file won't be found"
        )
        assert "50-microjs8-poweroff.rules" in content, (
            "build_deb.py must reference the rule filename "
            "50-microjs8-poweroff.rules so the file ends up in the .deb"
        )
