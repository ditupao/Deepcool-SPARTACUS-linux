#!/usr/bin/env bash
# Take the LCD device back from a running libvirt VM and start the
# Linux-native daemon again. Adjust VM name / paths / device id as needed.
set -euo pipefail

VM="${1:-win11}"
XML="${SPARTACUS_HOSTDEV_XML:-$HOME/vm-usb/spartacus-aio.xml}"

virsh -c qemu:///system detach-device "$VM" "$XML" --live || true
sleep 1
systemctl start spartacusd spartacus-web
echo "LCD back on Linux; spartacusd is serving the screen."
