# Running MicroJS8 on the ClockworkPi uConsole CM4

v0.0.18+ supports the ClockworkPi uConsole CM4 (and CM4 Lite) as a
deployment platform. MicroJS8 renders its UI on the uConsole's 5-inch
720p IPS panel via the kernel's vc4drmfb framebuffer compatibility
shim, using the built-in QWERTY keyboard for input.

The v0.0.18 workflow:
  1. Boot the uConsole into its normal desktop
  2. Open LXTerminal
  3. Run: `sudo microjs8-launch`
  4. The desktop disappears, MicroJS8 UI appears on the panel
  5. Operate JS8 (TX, RX, decode, beacon)
  6. Press HOME EXIT on the panel -> uConsole powers off
  7. Next boot returns to the desktop

This is different from v0.0.17, which auto-switched the default
systemd target to text-console-only. The v0.0.18 model keeps the
desktop intact and uses MicroJS8 as a launchable application.

Hardware requirements:
  - ClockworkPi uConsole with RPI-CM4 or CM4-Lite compute module
  - microSD card with Raspberry Pi OS Bookworm (or ClockworkOS)
  - QDX / G90 / DigiRig / similar radio + USB audio interface
    (connected via the external USB 2.0 port)
  - Optional: u-Blox 7 (or similar) USB GPS for time/position sync

## What you'll see

MicroJS8's UI is designed for a 320 x 170 panel (the Waveshare and
M5Stack CardputerZero rigs). On the uConsole the same UI is rotated
90 degrees clockwise, scaled 4x with nearest-neighbor (crisp pixels,
no smoothing), and centered on the 720 x 1280 panel:

    +--------- 720 x 1280 panel ---------+
    |                                    |
    |  20px black margin (left)          |
    |                                    |
    |  +--- 680 x 1280 MicroJS8 UI ---+  |
    |  |                              |  |
    |  |  (4x scaled, rotated -90)    |  |
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

## Install

1. Boot the uConsole into Raspberry Pi OS Bookworm (or ClockworkOS).

2. SCP the .deb to the uConsole:

       scp microjs8_0.0.18-1_all.deb dan@<uconsole-ip>:/tmp/

3. Install:

       sudo apt install -y /tmp/microjs8_0.0.18-1_all.deb

4. Watch the install transcript. On a uConsole, the postinst should
   print:

       ============================================================
       microjs8 v0.0.18: uConsole detected
       ============================================================

       Run MicroJS8 from a desktop terminal (LXTerminal) with:

           sudo microjs8-launch
       ...

5. No reboot needed. Open LXTerminal and run `sudo microjs8-launch`.

## Operator workflow

### Launching from the desktop (primary)

```
Open LXTerminal
sudo microjs8-launch
```

You will see:

```
microjs8-launch: starting (v4)
microjs8-launch: invocation context: desktop
microjs8-launch: detaching via systemd-run as microjs8-runtime.service
microjs8-launch: microjs8-runtime.service queued; this terminal can close.
```

LXTerminal returns to a prompt. Within ~3 seconds:
  - Desktop disappears
  - Panel lights up with MicroJS8 UI

You can close LXTerminal at any time. The launcher continues running
inside the systemd unit `microjs8-runtime.service`.

### Powering off

Press HOME EXIT on the panel. The uConsole powers off gracefully.
Next boot returns to the desktop.

### Aborting without powering off (rare)

If you need to return to the desktop without powering off:

```
sudo systemctl stop microjs8-runtime
```

The cleanup trap restarts lightdm and the desktop returns.

### Following the launcher's progress

```
sudo journalctl -u microjs8-runtime -f
```

Or check the persistent log:

```
sudo tail -50 /var/log/microjs8/launch.log
```

### Over SSH (debug)

For debugging from another machine over SSH:

```
ssh dan@<uconsole-ip>
sudo microjs8-launch --foreground
```

The `--foreground` flag keeps the script attached to your SSH session
so you see all output and can Ctrl+C to abort (which restarts lightdm
to return to the desktop). Without `--foreground`, SSH gets the same
detach behavior as LXTerminal.

## Audio + radio peripherals

The uConsole has a 3.5mm audio jack. MicroJS8's audio capture and
playback path goes through ALSA, same as on the Pi Zero rig --
plug your radio's USB audio device (DigiRig, QDX with built-in audio,
etc.) into the external USB 2.0 port.

The uConsole's internal stereo speakers are not used by MicroJS8
audio in v0.0.18. They remain available to other applications.

## GPS coexistence

If you also use a USB GPS receiver (u-Blox 7, etc.) for time and
position, see `docs/GPSD_COEXIST.md` for the recommended gpsd
configuration. The postinst auto-detects the common conflict
condition (gpsd + DigiRig + USBAUTO=true) and applies a fix, but
operators with non-default GPS setups may need to verify the
configuration manually.

## Troubleshooting

### Launcher refuses with "framebuffer signature does not match uConsole"

The launcher has a hardware-signature check at the top to refuse
running on non-uConsole hardware. If you see this on an actual
uConsole, check:

    cat /sys/class/graphics/fb0/name           # expect vc4drmfb
    cat /sys/class/graphics/fb0/virtual_size   # expect 720,1280
    cat /sys/class/graphics/fb0/bits_per_pixel # expect 16

Common cause: a kernel update changed the framebuffer driver name
or geometry. Report to the GitHub issue tracker with the actual
values seen.

### Launcher returned to prompt but panel stays black

Check the launcher log:

    sudo tail -20 /var/log/microjs8/launch.log

And the runtime journal:

    sudo journalctl -u microjs8-runtime -n 30 --no-pager
    sudo journalctl -u microjs8 -n 30 --no-pager

Common causes:
  - lightdm didn't fully release the framebuffer in 1 second (rare,
    usually a one-time install-time blip; retry)
  - microjs8 crashed during startup (look in the microjs8 journal)
  - audio device contention (the audio backend may have crashed if
    no USB audio device is plugged in)

To recover the desktop:

    sudo systemctl stop microjs8-runtime
    sudo systemctl start lightdm

### Image appears upside down

v0.0.18 sets the rotation default to -90 (CW), verified on real
hardware. If your specific uConsole revision needs a different
rotation, edit `/usr/share/APPLaunch/lib/microjs8/microjs8/ui/display_uconsole.py`
and change `ROTATION_DEGREES` to one of: `-90`, `90`, `180`. Restart
microjs8 (`sudo systemctl restart microjs8` or `sudo systemctl restart microjs8-runtime`).

If a different value works on your hardware, please open a GitHub
issue with the uConsole revision number so we can investigate the
default upstream.

### Built-in keyboard not responding

The keyboard enumerates as a USB HID device. Check:

    ls -la /dev/input/by-id/ | grep ClockworkPI
    # Expect: usb-ClockworkPI_uConsole_*-event-kbd -> ../eventN

The MicroJS8 USB keyboard backend discovers any device matching
"*-event-kbd". If the symlink is missing, the kernel's input-event
udev rules may not be loaded -- try:

    sudo udevadm trigger
    sudo systemctl restart microjs8-runtime

### Radio stays in TX after launching

This usually means gpsd has the DigiRig open with RTS asserted.
See `docs/GPSD_COEXIST.md` for the fix.

Quick check: `sudo lsof /dev/digirig`. If `gpsd` is listed, run the
gpsd config fix from GPSD_COEXIST.md.

### Reverting to v0.0.17 desktop-disabled boot

If you specifically want the v0.0.17 behavior (boot to text console,
microjs8 service starts at boot):

    sudo systemctl set-default multi-user.target
    sudo systemctl enable microjs8
    sudo reboot

To go back to v0.0.18 (desktop boot + launcher):

    sudo systemctl set-default graphical.target
    sudo systemctl disable microjs8
    sudo reboot

## Related docs

  - `docs/I2C_KEYBOARD.md` -- using a CardKB v1.1 via I2C (not
    applicable to the uConsole's built-in keyboard, but documents
    the I2C backend in case operators add a CardKB)
  - `docs/GPSD_COEXIST.md` -- coexisting with a USB GPS receiver
  - `docs/CARDPUTER_LINK.md` -- the predecessor M5Stack rig (for
    historical reference; uConsole uses different hardware)
