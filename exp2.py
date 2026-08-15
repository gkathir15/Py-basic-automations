import os
import shutil
import subprocess
import glob
import math
from datetime import datetime
from PIL import Image, ImageDraw

def get_video_info(video_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=0",
        video_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    info = {}
    for line in result.stdout.strip().split("\n"):
        if "=" in line:
            key, val = line.split("=", 1)
            info[key] = val
    if "duration" in info:
        info["duration"] = float(info["duration"])
    if "width" in info:
        info["width"] = int(info["width"])
    if "height" in info:
        info["height"] = int(info["height"])
    if "r_frame_rate" in info:
        num, den = map(int, info["r_frame_rate"].split("/"))
        info["fps"] = num / den
    return info

def create_gradient_mask(w, h, overlap):
    """Creates an alpha mask for smooth vertical blending between strips."""
    mask = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(mask)
    
    # Top fade in
    for y in range(overlap):
        alpha = int((y / overlap) * 255)
        draw.line([(0, y), (w, y)], fill=alpha)
        
    # Bottom fade out
    for y in range(h - overlap, h):
        alpha = int(((h - y) / overlap) * 255)
        draw.line([(0, y), (w, y)], fill=alpha)
        
    return mask

def main():
    video_path = "IMG_1732.mp4"
    temp_frames_dir = "temp_full_frames"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    print("Checking video metadata...")
    info = get_video_info(video_path)
    fps = info.get("fps", 25.0)
    duration = info.get("duration", 0.0)
    width = info.get("width", 1920)
    height = info.get("height", 1080)
    
    total_frames = int(duration * fps)
    
    print(f"--- Video Info ---")
    print(f"Resolution: {width}x{height}")
    print(f"Frame Rate: {fps} FPS (Matching Input)")
    print(f"Duration: {duration:.2f} sec")
    print(f"Total Frames: {total_frames}")
    print(f"------------------\n")

    # 1. EXTRACT ALL FULL FRAMES TO RAM
    if os.path.exists(temp_frames_dir):
        shutil.rmtree(temp_frames_dir)
    os.makedirs(temp_frames_dir)

    print("Extracting full frames to disk (this allows dynamic Python cropping)...")
    cmd_extract = [
        "ffmpeg", "-y", "-i", video_path,
        "-q:v", "2",
        os.path.join(temp_frames_dir, "frame_%05d.jpg")
    ]
    subprocess.run(cmd_extract, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    frame_paths = sorted(glob.glob(os.path.join(temp_frames_dir, "frame_*.jpg")))
    actual_frame_count = len(frame_paths)
    
    print(f"Loading {actual_frame_count} frames into RAM (Hope you have enough!)...")
    frames = []
    for p in frame_paths:
        with Image.open(p) as img:
            frames.append(img.copy().convert("RGBA"))
    print("RAM loading complete!\n")

    # 2. DEFINE THE EXPERIMENTS
    canvas_w = 3000 # Increased width
    canvas_h = 3508 # Standard A4 height
    
    experiments = [
        {"name": "01_Classic_Wide", "strips": 25, "overlap": 0, "time_map": "linear"},
        {"name": "02_Smooth_Blend", "strips": 25, "overlap": 30, "time_map": "linear"},
        {"name": "03_High_Density", "strips": 150, "overlap": 0, "time_map": "linear"},
        {"name": "04_Time_Wave", "strips": 50, "overlap": 10, "time_map": "sine"}
    ]

    # 3. RUN EXPERIMENTS
    for exp in experiments:
        exp_name = exp["name"]
        num_strips = exp["strips"]
        overlap = exp["overlap"]
        time_map = exp["time_map"]
        
        output_video = f"{timestamp}_Output_{exp_name}.mp4"
        exp_temp_dir = f"temp_render_{exp_name}"
        os.makedirs(exp_temp_dir, exist_ok=True)
        
        print(f"🎬 Starting Experiment: {exp_name}")
        
        # Calculate dimensions
        base_strip_h = int(canvas_h / num_strips)
        draw_strip_h = base_strip_h + (overlap * 2) # Add overlap to top and bottom
        
        # Corresponding crop height from the 1080p video
        crop_h = int(height * (draw_strip_h / canvas_h))
        crop_y = (height - crop_h) // 2
        
        # Pre-calculate gradient mask if blending
        mask = create_gradient_mask(canvas_w, draw_strip_h, overlap) if overlap > 0 else None

        for k in range(actual_frame_count):
            canvas = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 255))
            
            for idx in range(num_strips):
                # Where to place on canvas
                y_start = int(idx * base_strip_h) - overlap
                
                # Determine time offset based on mapping
                if time_map == "linear":
                    delay = int((idx / num_strips) * actual_frame_count)
                elif time_map == "sine":
                    # Wave flows between 0 and max frames based on a sine curve
                    wave = (math.sin((idx / num_strips) * math.pi * 2) + 1) / 2
                    delay = int(wave * (actual_frame_count / 2))
                
                src_idx = (k + delay) % actual_frame_count
                
                # Crop and resize
                src_img = frames[src_idx]
                cropped = src_img.crop((0, crop_y, width, crop_y + crop_h))
                resized = cropped.resize((canvas_w, draw_strip_h), Image.Resampling.BILINEAR)
                
                # Paste to canvas
                if overlap > 0:
                    canvas.paste(resized, (0, y_start), mask)
                else:
                    canvas.paste(resized, (0, y_start))
                    
            # Save frame
            out_path = os.path.join(exp_temp_dir, f"frame_{k:05d}.jpg")
            canvas.convert("RGB").save(out_path, "JPEG", quality=85)
            
            if (k + 1) % 100 == 0 or k == actual_frame_count - 1:
                print(f"  [>] Rendered frame {k + 1}/{actual_frame_count}")
                
        # Compile Video
        print(f"  [>] Compiling {output_video} via ffmpeg...")
        cmd_compile = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", os.path.join(exp_temp_dir, "frame_%05d.jpg"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            output_video
        ]
        subprocess.run(cmd_compile, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Cleanup experiment temp
        shutil.rmtree(exp_temp_dir)
        print(f"✅ Finished {output_video}\n")

    # Final Cleanup
    print("Cleaning up full frames from disk...")
    shutil.rmtree(temp_frames_dir)
    print("All experiments completed successfully! Enjoy the visual trip.")

if __name__ == "__main__":
    main()