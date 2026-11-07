#!/usr/bin/env bash
# Hand the LCD device (default 3633:0027) from the Linux host to a running
# libvirt Windows VM. Adjust VM name / paths / device id for your setup.
set -euo pipefail

VM="${1:-win11}"
USB_ID="${SPARTACUS_USB_ID:-3633:0027}"
XML="${SPARTACUS_HOSTDEV_XML:-$HOME/vm-usb/spartacus-aio.xml}"

systemctl stop spartacusd spartacus-web || true
virsh -c qemu:///system attach-device "$VM" "$XML" --live
echo "LCD $USB_ID handed to VM '$VM'. Restart the DeepCool app in Windows."
