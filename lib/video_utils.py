"""Extract a 3×3 frame grid from a video using ffmpeg + Pillow."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile

from PIL import Image

GRID_COLS = 3
GRID_ROWS = 3
FRAME_COUNT = GRID_COLS * GRID_ROWS
FRAME_W = 384
FRAME_H = 216  # 16:9


def extract_frame_grid(video_bytes: bytes) -> bytes:
    """Return a JPEG 3×3 grid of evenly-spaced frames from the video."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "v")
        with open(src, "wb") as f:
            f.write(video_bytes)

        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                src,
            ],
            capture_output=True, text=True, timeout=30,
        )
        try:
            duration = float(result.stdout.strip())
        except ValueError:
            raise RuntimeError("ffprobe could not determine video duration")

        frames: list[Image.Image] = []
        for i in range(FRAME_COUNT):
            t = duration * (i + 0.5) / FRAME_COUNT
            dst = os.path.join(tmp, f"f{i}.jpg")
            subprocess.run(
                [
                    "ffmpeg", "-ss", f"{t:.3f}", "-i", src,
                    "-frames:v", "1",
                    "-vf", (
                        f"scale={FRAME_W}:{FRAME_H}:"
                        "force_original_aspect_ratio=decrease,"
                        f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2:black"
                    ),
                    "-q:v", "3", "-y", dst,
                ],
                capture_output=True, timeout=30,
            )
            if os.path.exists(dst):
                frames.append(Image.open(dst).copy())

        if not frames:
            raise RuntimeError("No frames could be extracted from video")

        grid = Image.new("RGB", (FRAME_W * GRID_COLS, FRAME_H * GRID_ROWS))
        for idx, frame in enumerate(frames):
            row, col = divmod(idx, GRID_COLS)
            grid.paste(frame, (col * FRAME_W, row * FRAME_H))

        buf = io.BytesIO()
        grid.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
