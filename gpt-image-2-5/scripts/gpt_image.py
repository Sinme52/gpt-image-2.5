#!/usr/bin/env python3
"""OpenAI GPT Image 2.5 (gpt-image-2.5-*) client for the gpt-image-2-5 skill.

Two subcommands:
  generate  Draft image from a prompt (default quality: medium).
  release   Take a draft image and re-render it at quality xhigh via the edits
            endpoint, keeping the composition of the draft.

Standard library only, so it runs in sandboxes where pip is unavailable.
The API key is read from OPENAI_IMAGE_API_KEY, falling back to a .env file in
the skill root. The key is never printed.
"""

import argparse
import base64
import datetime as _dt
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
API_KEY_ENV = "OPENAI_IMAGE_API_KEY"
BASE_URL_ENV = "OPENAI_IMAGE_BASE_URL"  # override for proxies / local testing
DEFAULT_BASE_URL = "https://api.openai.com/v1"

DEFAULT_MODEL = "gpt-image-2.5-sunburst"
MODELS = ("gpt-image-2.5-sunburst", "gpt-image-2.5-flare")
QUALITIES = ("low", "medium", "high", "xhigh", "max", "auto")
DRAFT_QUALITY = "medium"
RELEASE_QUALITY = "xhigh"
FORMATS = ("png", "jpeg", "webp")
REQUEST_TIMEOUT = 600  # xhigh renders can take minutes

RELEASE_INSTRUCTION = (
    "Re-render this exact image as a final, release-quality version. "
    "Keep the composition, framing, subjects, poses, colors, lighting, style "
    "and any text exactly as they are. Do not add, remove or move anything. "
    "Only increase detail, sharpness and clean up artifacts."
)


class SkillError(Exception):
    pass


# ---------------------------------------------------------------- config

def _key_from_env_file(env_file):
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        if name == API_KEY_ENV:
            value = value.strip().strip('"').strip("'")
            if value:
                return value
    return None


def load_api_key():
    key = os.environ.get(API_KEY_ENV, "").strip()
    if key:
        return key
    # Skill folder first (the copy bundled into the zip), then the directory
    # the script is run from (e.g. a project root in Claude Code).
    candidates = [SKILL_ROOT / ".env", Path.cwd() / ".env"]
    for env_file in candidates:
        key = _key_from_env_file(env_file)
        if key:
            return key
    raise SkillError(
        f"API key not found. Set the environment variable {API_KEY_ENV}, "
        f"or put '{API_KEY_ENV}=sk-...' in one of: "
        + ", ".join(str(p) for p in candidates)
    )


def base_url():
    return os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/")


def default_out_dir():
    # claude.ai exposes files written here to the user for download.
    claude_ai_outputs = Path("/mnt/user-data/outputs")
    if claude_ai_outputs.is_dir():
        return claude_ai_outputs
    return Path.cwd() / "gpt-image-outputs"


# ---------------------------------------------------------------- HTTP

def _post(url, body, content_type, api_key):
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", content_type)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            request_id = resp.headers.get("x-request-id")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        raise SkillError(f"OpenAI API error {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise SkillError(
            f"Could not reach {url}: {e.reason}. If this runs on claude.ai, "
            "allow network egress to api.openai.com in Settings > Capabilities."
        ) from None
    return payload, request_id, time.time() - started


def _multipart(fields, files):
    """fields: [(name, value)], files: [(name, filename, bytes, mime)]."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n".encode("utf-8")
        )
    for name, filename, data, mime in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode("utf-8")
            + data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- images

def image_size(path):
    """Return (width, height) for PNG/JPEG/WebP, or None if unknown."""
    data = Path(path).read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + seg_len
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L":
            b = data[21:25]
            w = 1 + (((b[1] & 0x3F) << 8) | b[0])
            h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            return w, h
    return None


def valid_api_size(w, h):
    return (
        w % 16 == 0 and h % 16 == 0
        and max(w, h) <= 3840
        and 1 / 3 <= w / h <= 3
        and 655_360 <= w * h <= 8_294_400
    )


def mime_for(path):
    ext = Path(path).suffix.lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp"}.get(ext, "application/octet-stream")


def sidecar_path(image_path):
    return Path(image_path).with_suffix(".json")


def read_sidecar(image_path):
    p = sidecar_path(image_path)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def save_results(payload, out_dir, stem, fmt, meta):
    out_dir.mkdir(parents=True, exist_ok=True)
    items = payload.get("data") or []
    if not items:
        raise SkillError(f"API returned no images: {json.dumps(payload)[:500]}")
    saved = []
    for idx, item in enumerate(items):
        b64 = item.get("b64_json")
        if not b64:
            raise SkillError("API response did not include b64_json image data.")
        suffix = f"_{idx + 1}" if len(items) > 1 else ""
        ext = "jpg" if fmt == "jpeg" else fmt
        path = out_dir / f"{stem}{suffix}.{ext}"
        path.write_bytes(base64.b64decode(b64))
        record = dict(meta)
        if item.get("revised_prompt"):
            record["revised_prompt"] = item["revised_prompt"]
        sidecar_path(path).write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        saved.append(path)
    return saved


def timestamp():
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------- commands

def cmd_generate(args):
    api_key = load_api_key()
    body = {
        "model": args.model,
        "prompt": args.prompt,
        "quality": args.quality,
        "size": args.size,
        "output_format": args.format,
        "n": args.n,
    }
    if args.background:
        body["background"] = args.background
    payload, request_id, elapsed = _post(
        f"{base_url()}/images/generations",
        json.dumps(body).encode("utf-8"),
        "application/json",
        api_key,
    )
    meta = {
        "kind": "draft" if args.quality == DRAFT_QUALITY else "generate",
        "prompt": args.prompt,
        "model": args.model,
        "quality": args.quality,
        "size": args.size,
        "output_format": args.format,
        "background": args.background,
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "request_id": request_id,
        "elapsed_sec": round(elapsed, 1),
        "usage": payload.get("usage"),
    }
    stem = args.name or f"draft-{timestamp()}"
    saved = save_results(payload, Path(args.out_dir), stem, args.format, meta)
    report(saved, meta)


def cmd_release(args):
    api_key = load_api_key()
    src = Path(args.image)
    if not src.is_file():
        raise SkillError(f"Image not found: {src}")
    draft = read_sidecar(src)

    prompt = args.prompt or draft.get("prompt")
    if not prompt:
        raise SkillError(
            "No prompt for this image: no sidecar JSON was found next to it. "
            "Pass --prompt with a description of the image."
        )
    model = args.model or draft.get("model") or DEFAULT_MODEL
    fmt = args.format or draft.get("output_format") or "png"
    background = args.background or draft.get("background")

    size = args.size
    if not size:
        dims = image_size(src)
        if dims and valid_api_size(*dims):
            size = f"{dims[0]}x{dims[1]}"
        else:
            size = draft.get("size") or "auto"

    if args.mode == "edit":
        fields = [
            ("model", model),
            ("prompt", f"{RELEASE_INSTRUCTION}\n\nImage description: {prompt}"),
            ("quality", args.quality),
            ("size", size),
            ("output_format", fmt),
        ]
        if background:
            fields.append(("background", background))
        files = [("image[]", src.name, src.read_bytes(), mime_for(src))]
        body, ctype = _multipart(fields, files)
        url = f"{base_url()}/images/edits"
    else:
        payload_body = {
            "model": model, "prompt": prompt, "quality": args.quality,
            "size": size, "output_format": fmt,
        }
        if background:
            payload_body["background"] = background
        body, ctype = json.dumps(payload_body).encode("utf-8"), "application/json"
        url = f"{base_url()}/images/generations"

    payload, request_id, elapsed = _post(url, body, ctype, api_key)
    meta = {
        "kind": "release",
        "mode": args.mode,
        "source_image": str(src),
        "prompt": prompt,
        "model": model,
        "quality": args.quality,
        "size": size,
        "output_format": fmt,
        "background": background,
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "request_id": request_id,
        "elapsed_sec": round(elapsed, 1),
        "usage": payload.get("usage"),
    }
    out_dir = Path(args.out_dir) if args.out_dir else src.parent
    if args.name:
        stem = args.name
    elif src.stem.startswith("draft-"):
        stem = f"release-{src.stem[len('draft-'):]}"
    else:
        stem = f"{src.stem}-release"
    saved = save_results(payload, out_dir, stem, fmt, meta)
    report(saved, meta)


def report(saved, meta):
    print(json.dumps({
        "ok": True,
        "files": [str(p) for p in saved],
        "model": meta["model"],
        "quality": meta["quality"],
        "size": meta["size"],
        "elapsed_sec": meta["elapsed_sec"],
        "usage": meta.get("usage"),
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(description="GPT Image 2.5 generator")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="create a draft image (default: medium)")
    g.add_argument("--prompt", required=True)
    g.add_argument("--quality", default=DRAFT_QUALITY, choices=QUALITIES)
    g.add_argument("--model", default=DEFAULT_MODEL, choices=MODELS)
    g.add_argument("--size", default="1024x1024",
                   help="1024x1024 | 1536x1024 | 1024x1536 | auto | WxH")
    g.add_argument("--format", default="png", choices=FORMATS)
    g.add_argument("--background", choices=("transparent", "opaque", "auto"))
    g.add_argument("--n", type=int, default=1)
    g.add_argument("--name", help="output file stem (no extension)")
    g.add_argument("--out-dir", default=str(default_out_dir()))
    g.set_defaults(func=cmd_generate)

    r = sub.add_parser("release", help="re-render a draft image at xhigh")
    r.add_argument("--image", required=True, help="path to the draft image")
    r.add_argument("--prompt", help="defaults to the prompt in the draft's sidecar JSON")
    r.add_argument("--mode", default="edit", choices=("edit", "regenerate"),
                   help="edit: refine the draft image itself (keeps composition); "
                        "regenerate: run the prompt again from scratch")
    r.add_argument("--quality", default=RELEASE_QUALITY, choices=QUALITIES)
    r.add_argument("--model", choices=MODELS)
    r.add_argument("--size", help="defaults to the draft image's size")
    r.add_argument("--format", choices=FORMATS)
    r.add_argument("--background", choices=("transparent", "opaque", "auto"))
    r.add_argument("--name", help="output file stem (no extension)")
    r.add_argument("--out-dir", help="defaults to the draft image's folder")
    r.set_defaults(func=cmd_release)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except SkillError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
