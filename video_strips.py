import os
import shutil
import subprocess
import glob
import math
from PIL import Image

def get_video_info(video_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
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
    return info

def main():
    video_path = "IMG_1732.mp4"
    output_jpg = "a4_strips.jpg"
    temp_dir = "temp_strips"
    
    print("Checking video metadata...")
    info = get_video_info(video_path)
    duration = info.get("duration", 0.0)
    width = info.get("width", 1920)
    height = info.get("height", 1080)
    print(f"Video: {video_path}")
    print(f"Dimensions: {width}x{height}")
    print(f"Duration: {duration:.2f} seconds")
    
    # We take every second from t = 0 to floor(duration)
    seconds_to_extract = list(range(0, math.floor(duration) + 1))
    num_strips = len(seconds_to_extract)
    print(f"Extracting {num_strips} frames (one for every second from 0s to {seconds_to_extract[-1]}s)...")
    
    # Clean and recreate temp directory
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir)
    
    # Extract frames using ffmpeg fast seeking
    for sec in seconds_to_extract:
        out_img = os.path.join(temp_dir, f"frame_{sec:03d}.png")
        # Place -ss BEFORE -i for fast seek
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(sec),
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            out_img
        ]
        # Run silently
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if (sec + 1) % 5 == 0 or sec == seconds_to_extract[-1]:
            print(f"Extracted frame for second {sec}...")
            
    # List the extracted frames
    frames = sorted(glob.glob(os.path.join(temp_dir, "frame_*.png")))
    if not frames:
        print("Error: No frames extracted!")
        return
        
    print("\nAssembling A4 sheet canvas...")
    # A4 dimensions at 300 DPI: 2480 x 3508 pixels
    canvas_w = 2480
    canvas_h = 3508
    
    print(f"A4 Canvas: {canvas_w}x{canvas_h} pixels")
    print("Layout: No margins, no spacing between strips.")
    
    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
    
    for idx, frame_path in enumerate(frames):
        # Determine exact pixel bounds for this strip to avoid rounding gaps
        y_start = int(idx * canvas_h / num_strips)
        y_end = int((idx + 1) * canvas_h / num_strips)
        current_strip_h = y_end - y_start
        
        with Image.open(frame_path) as img:
            img_w, img_h = img.size
            
            # Crop a thin horizontal slice from the center matching the aspect ratio of the target strip
            # target aspect ratio = canvas_w / current_strip_h
            # crop_h = img_w * current_strip_h / canvas_w
            crop_h = int(img_w * current_strip_h / canvas_w)
            
            # Clamp crop_h if it exceeds image height
            crop_h = min(crop_h, img_h)
            
            left = 0
            top = (img_h - crop_h) // 2
            right = img_w
            bottom = top + crop_h
            
            cropped = img.crop((left, top, right, bottom))
            resized = cropped.resize((canvas_w, current_strip_h), Image.Resampling.LANCZOS)
            
            canvas.paste(resized, (0, y_start))
            
    print(f"\nSaving high-quality JPEG to {output_jpg}...")
    # Save as JPEG with maximum/very high quality (95)
    canvas.save(output_jpg, "JPEG", quality=95)
    
    # Cleaning up
    print("Cleaning up temporary directory...")
    shutil.rmtree(temp_dir)
    print("Success! Created A4 sheet JPEG representation.")

if __name__ == "__main__":
    main()
