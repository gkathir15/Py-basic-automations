import os
import shutil
import subprocess
import glob
import math
from datetime import datetime
from PIL import Image

# --- CONFIGURATION ---
VIDEO_PATH = "IMG_1732.mp4"
CANVAS_W = 2800  # Increased width slightly from 2480 for a wider field of view
CANVAS_H = 3508
NUM_STRIPS = 32  # 32 horizontal time-slices

TEMP_SLICES = "temp_raw_slices"
TEMP_FRAMES = "temp_render_workspace"


def get_video_info(video_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,r_frame_rate,nb_frames",
        "-of", "default=noprint_wrappers=1:nokey=0",
        video_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    info = {}
    for line in result.stdout.strip().split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v

    num, den = map(int, info["r_frame_rate"].split("/"))
    fps = num / den
    duration = float(info.get("duration", 0.0))
    
    # Fallback frame counter if container metadata misses nb_frames
    total_frames = int(info["nb_frames"]) if "nb_frames" in info and info["nb_frames"] != "N/A" else int(duration * fps)

    return {
        "width": int(info["width"]),
        "height": int(info["height"]),
        "fps": fps,
        "duration": duration,
        "total_frames": total_frames
    }


def get_subframe_slice(slices, exact_float_idx, target_size):
    """ Grabs two adjacent frames and blends them mathematically to kill frame-stutter """
    total = len(slices)
    idx_a = int(exact_float_idx) % total
    idx_b = (idx_a + 1) % total
    alpha = exact_float_idx - math.floor(exact_float_idx)

    if alpha < 0.02:
        base_img = slices[idx_a]
    else:
        base_img = Image.blend(slices[idx_a], slices[idx_b], alpha)

    return base_img.resize(target_size, Image.Resampling.BILINEAR)


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Interrogating {VIDEO_PATH}...")
    meta = get_video_info(VIDEO_PATH)
    
    print(f" -> Resolution:   {meta['width']}x{meta['height']}")
    print(f" -> Native FPS:   {meta['fps']:.2f}")
    print(f" -> Total Frames: {meta['total_frames']} ({meta['duration']:.2f}s)")

    # Calculate exact proportional crop to prevent pixel stretching
    strip_h_on_canvas = CANVAS_H / NUM_STRIPS
    crop_h = max(4, int((meta['width'] * strip_h_on_canvas) / CANVAS_W))
    crop_y = (meta['height'] - crop_h) // 2

    # 1. HARVEST SLICES (Done once for all experiments)
    if os.path.exists(TEMP_SLICES): shutil.rmtree(TEMP_SLICES)
    os.makedirs(TEMP_SLICES)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Slicing source video into {meta['width']}x{crop_h} strips...")
    cmd_extract = [
        "ffmpeg", "-y", "-i", VIDEO_PATH,
        "-vf", f"crop={meta['width']}:{crop_h}:0:{crop_y}",
        "-q:v", "2",
        os.path.join(TEMP_SLICES, "slice_%06d.png")
    ]
    subprocess.run(cmd_extract, check=True)

    slice_files = sorted(glob.glob(os.path.join(TEMP_SLICES, "slice_*.png")))
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Loading {len(slice_files)} uncompressed slices into RAM...")
    
    slices = [Image.open(p).copy() for p in slice_files]
    print(" -> RAM Population complete.")

    # Define the batch experiments
    experiments = [
        {"id": "EXP1_Buttery_Glide", "type": "glide"},
        {"id": "EXP2_Chrono_Ripple", "type": "ripple"},
        {"id": "EXP3_The_Accordion", "type": "accordion"}
    ]

    time_step_per_strip = meta['total_frames'] / NUM_STRIPS

    # 2. RUN EXPERIMENTAL GENERATOR
    for exp in experiments:
        print(f"\n=======================================================")
        print(f" Starting Render: {exp['id']}")
        print(f"=======================================================")

        if os.path.exists(TEMP_FRAMES): shutil.rmtree(TEMP_FRAMES)
        os.makedirs(TEMP_FRAMES)

        for k in range(meta['total_frames']):
            # Dark charcoal canvas makes slit-scan pop way harder than pure white
            canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (18, 18, 18))

            for s in range(NUM_STRIPS):
                y_start = int(s * CANVAS_H / NUM_STRIPS)
                y_end = int((s + 1) * CANVAS_H / NUM_STRIPS)
                strip_target_size = (CANVAS_W, y_end - y_start)

                # --- TOPOLOGY EXPERIMENTS ---
                if exp["type"] == "glide":
                    sample_point = (s * time_step_per_strip) + k

                elif exp["type"] == "ripple":
                    # Sine wave undulating through time
                    wave = math.sin((s / NUM_STRIPS) * 4 * math.pi + (k / meta['total_frames']) * 2 * math.pi) * (meta['fps'] * 0.6)
                    sample_point = (s * time_step_per_strip) + k + wave

                elif exp["type"] == "accordion":
                    # Dead center is present moment; edges lag behind
                    center_strip = NUM_STRIPS / 2.0
                    distance_from_now = abs(s - center_strip)
                    sample_point = k - (distance_from_now * (meta['fps'] * 0.35))

                # Grab interpolated sub-frame
                strip_img = get_subframe_slice(slices, sample_point, strip_target_size)
                canvas.paste(strip_img, (0, y_start))

            canvas.save(os.path.join(TEMP_FRAMES, f"frame_{k:06d}.jpg"), "JPEG", quality=90)

            if (k + 1) % 50 == 0 or k == meta['total_frames'] - 1:
                print(f" -> [{exp['id']}] Rendered frame {k + 1}/{meta['total_frames']}")

        # 3. COMPILE
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"{exp['id']}_{stamp}.mp4"
        
        print(f" -> Encoding {output_filename} at native {meta['fps']:.2f} FPS...")
        cmd_compile = [
            "ffmpeg", "-y",
            "-framerate", str(meta['fps']),
            "-i", os.path.join(TEMP_FRAMES, "frame_%06d.jpg"),
            "-c:v", "libx264",
            "-crf", "17",  # Visually lossless
            "-pix_fmt", "yuv420p",
            output_filename
        ]
        subprocess.run(cmd_compile, check=True)
        print(f" [SUCCESS] Written to disk: {output_filename}")

    # Cleanup
    print("\nSweeping temporary workspace...")
    shutil.rmtree(TEMP_SLICES)
    shutil.rmtree(TEMP_FRAMES)
    print("All experiments compiled safely.")


if __name__ == "__main__":
    main()