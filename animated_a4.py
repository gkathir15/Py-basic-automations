import os
import shutil
import subprocess
import glob
from PIL import Image

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

def main():
    video_path = "IMG_1732.mp4"
    output_video = "animated_a4.mp4"
    temp_slices = "temp_slices"
    temp_frames = "temp_frames"
    
    print("Checking video metadata...")
    info = get_video_info(video_path)
    duration = info.get("duration", 0.0)
    width = info.get("width", 1920)
    height = info.get("height", 1080)
    fps_in = info.get("fps", 50.0)
    
    print(f"Video: {video_path}")
    print(f"Dimensions: {width}x{height}")
    print(f"Frame Rate: {fps_in} FPS")
    print(f"Duration: {duration:.2f} seconds")
    
    # Define A4 layout
    canvas_w = 2480
    canvas_h = 3508
    num_strips = 25  # representing seconds 0 to 24
    
    # Calculate crop height:
    # Each strip on A4 page is canvas_h / num_strips = 3508 / 25 = 140.32 pixels
    # Crop height in original 1920x1080: crop_h = width * (canvas_h / num_strips) / canvas_w
    # crop_h = 1920 * 140.32 / 2480 = 108.6 -> 108 pixels.
    crop_h = 108
    crop_y = (height - crop_h) // 2  # Center crop vertically
    
    print(f"Cropping slices of size {width}x{crop_h} from original frames...")
    
    # Recreate temp_slices directory
    if os.path.exists(temp_slices):
        shutil.rmtree(temp_slices)
    os.makedirs(temp_slices)
    
    # Extract cropped frames using ffmpeg
    cmd_extract = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"crop={width}:{crop_h}:0:{crop_y}",
        "-q:v", "2",
        os.path.join(temp_slices, "slice_%05d.png")
    ]
    print("Extracting all frame slices via ffmpeg...")
    subprocess.run(cmd_extract, check=True)
    
    slice_paths = sorted(glob.glob(os.path.join(temp_slices, "slice_*.png")))
    num_slices = len(slice_paths)
    print(f"Extracted {num_slices} frame slices.")
    
    if num_slices == 0:
        print("Error: No slices extracted!")
        return
        
    print("Loading slices into memory...")
    slices = []
    for p in slice_paths:
        with Image.open(p) as img:
            slices.append(img.copy())
            
    print("Memory loading complete.")
    
    # Recreate temp_frames directory
    if os.path.exists(temp_frames):
        shutil.rmtree(temp_frames)
    os.makedirs(temp_frames)
    
    # Generation settings
    fps_out = 25.0
    num_frames = 1200  # 1200 frames = 48 seconds
    step = fps_in / fps_out  # mapping step size
    
    print(f"Generating {num_frames} frames at {fps_out} FPS...")
    
    for k in range(num_frames):
        canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
        
        for idx in range(num_strips):
            # Calculate strip vertical bounds
            y_start = int(idx * canvas_h / num_strips)
            y_end = int((idx + 1) * canvas_h / num_strips)
            current_strip_h = y_end - y_start
            
            # Source frame index calculation
            start_frame = idx * fps_in
            src_idx = int((start_frame + k * step) % num_slices)
            
            src_img = slices[src_idx]
            # Resize cropped slice to fit the strip bounds
            resized = src_img.resize((canvas_w, current_strip_h), Image.Resampling.BILINEAR)
            
            canvas.paste(resized, (0, y_start))
            
        # Save output frame
        out_path = os.path.join(temp_frames, f"frame_{k:05d}.jpg")
        canvas.save(out_path, "JPEG", quality=85)
        
        if (k + 1) % 100 == 0 or k == num_frames - 1:
            print(f"Generated {k + 1}/{num_frames} frames...")
            
    print("Compiling video using ffmpeg...")
    cmd_compile = [
        "ffmpeg", "-y",
        "-framerate", str(fps_out),
        "-i", os.path.join(temp_frames, "frame_%05d.jpg"),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        output_video
    ]
    subprocess.run(cmd_compile, check=True)
    print(f"Successfully compiled {output_video}!")
    
    # Clean up
    print("Cleaning up temporary directories...")
    shutil.rmtree(temp_slices)
    shutil.rmtree(temp_frames)
    print("Cleanup complete.")

if __name__ == "__main__":
    main()
