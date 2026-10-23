# webui — a usage demo for `opendevice` (needs iOS 27)

```bash
python examples/webui/run.py                  # launch the UI, default http://127.0.0.1:8800/ (no install; uses the repo's opendevice directly)
```

The flags (`-d` / `--port` / `--tunnel` …) match `python -m webui`; run with
`--help` to see them all.

The page has three parts:

* **Screen**: one click = tap, press-and-drag = swipe. When a prerequisite is
  missing (developer mode off / image not mounted) a banner drops down at the
  top explaining how to fix it.
* **This device** (top right): developer mode, disk image, wireless pairing —
  the current state of each and the matching action (push image / pair / unpair).
* **Bottom right**: the control list. Hover to highlight on the screen, click to
  tap that control.

Two prerequisites are required: **developer mode** on (screen mirroring / touch /
control tree all depend on it) and a **personalized image** mounted (the
`com.apple.coredevice.*` services live in the image). With developer mode on,
iOS 27 re-mounts the image by itself, so most of the time you do nothing; if you
really need to push an image yourself, use the `opendevice mounter` command.
