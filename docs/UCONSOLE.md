# Running MicroJS8 on the ClockworkPi uConsole CM4

v0.0.17+ supports the ClockworkPi uConsole CM4 (and CM4 Lite) as a
deployment platform. MicroJS8 renders its UI on the uConsole's 5-inch
720p IPS panel via the kernel's vc4drmfb framebuffer compatibility
shim, using the built-in QWERTY keyboard for input.

Hardware requirements:
  - ClockworkPi uConsole with RPI-CM4 or CM4-Lite compute module
  - microSD card with Raspberry Pi OS Bookworm (or ClockworkOS)
  - QDX / G90 / DigiRig / similar radio + USB audio interface
    (connected via the external USB 2.0 port)

This document covers:
  - First-time install
  - How MicroJS8 looks on the uConsole's screen
  - Operator workflow
  - Troubleshooting

## What you'll see

MicroJS8's UI is designed for a 320 x 170 panel (the Waveshare and
M5Stack CardputerZero rigs). On the uConsole the same UI is rotated
90 degrees, scaled 4x with nearest-neighbor (crisp pixels, no
smoothing), and centered on the 720 x 1280 panel:

    +--------- 720 x 1280 panel ---------+
    |                                    |
    |  20px black margin (left)          |
    |                                    |
    |  +--- 680 x 1280 MicroJS8 UI ---+  |
    |  |                              |  |
    |  |  (4x scaled, rotated 90deg)  |  |
    |  |                              |  |
    |  |  HEADER bar (large)          |  |
    |  |  BODY area                   |  |
    |  |  FOOTER bar                  |  |
    |  |                              |  |
    |  +------------------------------+  |
    |                                    |
    |  20px black margin (right)         |
    |                                    |
    +------------------------------------+

The 4x scale produces a deliberately chunky, retro-pixel-art look.
This is the Path A "quick-and-dirty pixel double" port -- a future
release (v0.0.18+) may add a native 1280x720 layout with denser
information and proportional fonts sized for the panel. For now,
operators get a working MicroJS8 station on the uConsole with zero
manual configuration.

## Install

1. Boot the uConsole into Raspberry Pi OS Bookworm (or ClockworkOS).
   Either an SSH session from another machine or a local terminal
   works for the install.

2. SCP the .deb to the uConsole:

       scp microjs8_0.0.17-1_all.deb dan@<uconsole-ip>:/tmp/

3. Install:

       sudo apt install -y /tmp/microjs8_0.0.17-1_all.deb

4. Watch the install transcript. On a uConsole the postinst should
   print:

       microjs8: detected uConsole-style framebuffer signature
         (vc4drmfb 720x1280 16bpp). The v0.0.17+ uConsole
         display backend will be used.
       microjs8: switched default systemd target
         Was:  graphical.target  (X11 / desktop boots automatically)
         Now:  multi-user.target (text console; MicroJS8 owns the FB)
         ...
       REBOOT REQUIRED for the change to take effect.

5. Reboot:

       sudo reboot

6. After reboot the uConsole comes up to a text console. SSH back in
   (or use the local console) and start the daemon:

       sudo systemctl start microjs8

   MicroJS8's UI should appear on the panel within a couple seconds.

## Operator workflow

The MicroJS8 daemon runs as a systemd service named `microjs8`. The
HOME EXIT button (since v0.0.13) gracefully powers off the Pi via
`systemctl poweroff --ignore-inhibitors`.

To run MicroJS8 as your primary station-on-power-up workflow:

    sudo systemctl enable microjs8

The daemon then starts automatically every boot. Since the default
target is multi-user.target (set by v0.0.17 install on uConsole),
there's no desktop competing for the framebuffer.

The built-in QWERTY keyboard is recognized automatically by the USB
keyboard backend -- no config edits needed. Arrow keys navigate
screens; Enter selects; the alphanumeric keys type messages.

The uConsole's trackball, gamepad, and mouse are NOT used by MicroJS8
in v0.0.17 (the UI was designed for keyboard-only operation).

### Returning to the desktop temporarily

If you need to use the uConsole as a normal computer (web browsing,
file management, etc.):

    sudo systemctl stop microjs8
    sudo systemctl isolate graphical.target

You can then run X11 / desktop normally. To return to MicroJS8 mode:

    sudo systemctl isolate multi-user.target
    sudo systemctl start microjs8

### Permanently reverting (uninstall or roll back)

To fully revert the uConsole to graphical-target boot:

    sudo systemctl set-default graphical.target
    sudo reboot

(This change is independent of installing/removing the microjs8
package -- removing the .deb does NOT restore graphical.target.)

## Audio + radio peripherals

The uConsole has a 3.5mm audio jack. MicroJS8's audio capture and
playback path goes through ALSA, same as on the Pi Zero rig --
plug your radio's USB audio device (DigiRig, QDX with built-in audio,
etc.) into the external USB 2.0 port.

The uConsole's internal stereo speakers are not used by MicroJS8
audio in v0.0.17. They remain available to other applications.

To verify the radio's audio device is discovered:

    aplay -L | head
    arecord -L | head

The daemon's auto-config picks up the first USB audio device. See
the standard MicroJS8 docs for radio-specific configuration.

## Troubleshooting

### Screen stays black after `systemctl start microjs8`

1. Check the daemon's journal for the display detection log line:

       sudo journalctl -u microjs8 --since "1 min ago" | grep -i display

   Expected:

       open_display: uConsole signature detected, using
       UConsoleFramebufferDevice (fb0 vc4drmfb)

   If you see "uConsole signature matched but open failed" or the
   line is missing entirely, the framebuffer signature doesn't
   match exactly. Check the sysfs values manually:

       cat /sys/class/graphics/fb0/name           # expect vc4drmfb
       cat /sys/class/graphics/fb0/virtual_size   # expect 720,1280
       cat /sys/class/graphics/fb0/bits_per_pixel # expect 16

2. Verify the multi-user target is active:

       systemctl get-default          # expect multi-user.target
       systemctl is-active graphical  # expect inactive (not running)

3. If X11 or another display server is running, it owns the
   framebuffer. Stop it:

       sudo systemctl stop lightdm gdm sddm 2>/dev/null
       sudo systemctl isolate multi-user.target

### Image appears upside down or rotated

The default rotation is 90 degrees CCW. If the orientation looks
wrong on your specific uConsole hardware revision, edit the
ROTATION_DEGREES constant in
/usr/share/APPLaunch/lib/microjs8/microjs8/ui/display_uconsole.py
from 90 to -90 (or 180), then restart the daemon. Note: this edit
is overwritten on the next package install; if -90 works for your
hardware, please open a GitHub issue with the uConsole revision
number so we can fix the default upstream.

### Built-in keyboard not responding

The keyboard enumerates as a USB HID device. Check:

    ls -la /dev/input/by-id/ | grep ClockworkPI
    # Expect: usb-ClockworkPI_uConsole_*-event-kbd -> ../eventN

The MicroJS8 USB keyboard backend discovers any device matching
"*-event-kbd". If the symlink is missing, the kernel's input-event
udev rules may not be loaded -- try:

    sudo udevadm trigger
    sudo systemctl restart microjs8

### Display flickers or partially overdraws

This typically means X11 (or another display server) is still
running alongside the MicroJS8 daemon. Both are writing to the
framebuffer and fighting each other. Verify with:

    pgrep -a X xinit lightdm gdm
    # If anything is listed, that's your culprit

Stop the offending service and restart microjs8.
