"""Report the readiness stage actually proven, independently of frame delivery."""

import argparse
import json
from pathlib import Path
import socket
import time
from service_runner import process_start_time


def check(directory, name, port=0, host="127.0.0.1", *, camera_domain=None, bulk_limit=None):
    directory = Path(directory)
    try:
        state = json.loads((directory / f"{name}.service.json").read_text())
        if state.get("phase") != "running":
            return {"ready": False, "stage": state.get("phase", "starting")}
        child = state.get("child_pid")
        if (not isinstance(child, int) or not state.get("child_start") or
                process_start_time(child) != state["child_start"]):
            return {"ready": False, "stage": "child_not_running"}
        if port:
            host = host.removeprefix("[").removesuffix("]")
            host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
            with socket.create_connection((host, port), timeout=.1):
                return {"ready": True, "stage": "listening", "data_ready": False}
        if name.startswith("webrtc") or name in {"teammate", "media-relay", "source-media", "cloud-media"}:
            media = json.loads((directory / f"{name}.media.json").read_text())
            if media.get("pid") != child or not 0 <= time.time() - media.get("updated_wall_sec", 0) <= 3:
                return {"ready": False, "stage": "waiting_for_media_status"}
            if camera_domain is not None and media.get("camera_domain_id") != camera_domain:
                return {"ready": False, "stage": "camera_domain_not_active"}
            if bulk_limit is not None and media.get("bulk_limit_kbps") != bulk_limit:
                return {"ready": False, "stage": "bulk_limit_not_active"}
            return {"ready": True, "stage": "media_initialized",
                    "data_ready": bool(media.get("tracks")) and all(
                        track.get("state") == "flowing" for track in media["tracks"].values())}
        return {"ready": True, "stage": "process_running", "data_ready": False}
    except (OSError, ValueError, TypeError):
        return {"ready": False, "stage": "starting"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--camera-domain", type=int)
    parser.add_argument("--bulk-limit", type=int)
    args = parser.parse_args()
    result = check(args.run_dir, args.name, args.port, args.host,
                   camera_domain=args.camera_domain, bulk_limit=args.bulk_limit)
    print(json.dumps(result))
    raise SystemExit(0 if result["ready"] else 1)


if __name__ == "__main__":
    main()
