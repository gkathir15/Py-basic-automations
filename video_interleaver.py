import os
import shutil
import subprocess
import glob
from PIL import Image

def get_video_info(video_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,duration",
        "-of", "default=noprint_wrappers=1:nokey=0",
        video_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    info = {}
    for line in result.stdout.strip().split("\n"):
        if "=" in line:
            key, val = line.split("=", 1)
            info[key] = val
    
    # Parse r_frame_rate
    if "r_frame_rate" in info:
        num, den = map(int, info["r_frame_rate"].split("/"))
        info["fps"] = num / den
    
    if "duration" in info:
        info["duration"] = float(info["duration"])
        
    return info

def main():
    video1 = "IMG_1730.mov"
    video2 = "IMG_1732.mp4"
    output_video = "output.mp4"
    
    print("Checking video metadata...")
    info1 = get_video_info(video1)
    info2 = get_video_info(video2)
    
    print(f"Video 1 ({video1}): {info1.get('width')}x{info1.get('height')}, {info1.get('fps')} fps, {info1.get('duration')}s")
    print(f"Video 2 ({video2}): {info2.get('width')}x{info2.get('height')}, {info2.get('fps')} fps, {info2.get('duration')}s")
    
    fps = info1.get("fps", 50.0)
    print(f"Using target frame rate: {fps} FPS")
    
    # Cut duration in seconds (e.g., 0.25s)
    cut_duration = 0.25
    # Each chunk corresponds to (fps * cut_duration) frames.
    chunk_size = int(fps * cut_duration)
    print(f"Each {cut_duration}s chunk represents {chunk_size} frames.")
    
    temp_dir_v1 = "temp_frames_v1"
    temp_dir_v2 = "temp_frames_v2"
    temp_dir_out = "temp_frames_output"
    
    # Clean and recreate directories
    for d in [temp_dir_v1, temp_dir_v2, temp_dir_out]:
        if os.path.exists(d):
            print(f"Cleaning existing directory {d}...")
            shutil.rmtree(d)
        os.makedirs(d)
        
    print("\nExtracting frames from Video 1 (IMG_1730.mov) to images...")
    subprocess.run([
        "ffmpeg", "-y", "-i", video1,
        "-q:v", "2",
        os.path.join(temp_dir_v1, "frame_%05d.png")
    ], check=True)
    
    print("\nExtracting frames from Video 2 (IMG_1732.mp4) to images...")
    subprocess.run([
        "ffmpeg", "-y", "-i", video2,
        "-q:v", "2",
        os.path.join(temp_dir_v2, "frame_%05d.png")
    ], check=True)
    
    # List and sort the frames
    frames_v1 = sorted(glob.glob(os.path.join(temp_dir_v1, "frame_*.png")))
    frames_v2 = sorted(glob.glob(os.path.join(temp_dir_v2, "frame_*.png")))
    
    num_frames = min(len(frames_v1), len(frames_v2))
    print(f"\nFrames extracted: Video 1 = {len(frames_v1)}, Video 2 = {len(frames_v2)}")
    print(f"Processing first {num_frames} frames (approx {num_frames / fps:.2f} seconds)...")
    
    # Target resolution
    target_width = 1920
    target_height = 1080
    
    for i in range(num_frames):
        chunk_idx = i // chunk_size
        # odd chunk is first video (chunk_idx = 0 is 1st chunk -> Video 1; chunk_idx = 1 is 2nd chunk -> Video 2)
        if chunk_idx % 2 == 0:
            src_path = frames_v1[i]
        else:
            src_path = frames_v2[i]
            
        dst_path = os.path.join(temp_dir_out, f"frame_{i+1:05d}.png")
        
        # Load, resize, and save using Pillow
        with Image.open(src_path) as img:
            # Check size, if different, resize it
            if img.size != (target_width, target_height):
                img_resized = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
                img_resized.save(dst_path)
            else:
                shutil.copy(src_path, dst_path)
                
        if (i + 1) % 100 == 0 or i == num_frames - 1:
            print(f"Processed {i + 1}/{num_frames} frames...")
            
    print("\nReassembling final video with interleaved audio...")
    # Audio interleaving filter complex
    # Video 1: odd chunks => volume is 1 if mod(floor(t/cut_duration), 2) == 0
    # Video 2: even chunks => volume is 1 if mod(floor(t/cut_duration), 2) == 1
    # We use normalize=0 in amix to keep normal volumes, and set duration=shortest.
    filter_complex = (
        f"[1:a]volume='if(eq(mod(floor(t/{cut_duration}),2),0),1,0)':eval=frame[a0]; "
        f"[2:a]volume='if(eq(mod(floor(t/{cut_duration}),2),1),1,0)':eval=frame[a1]; "
        "[a0][a1]amix=inputs=2:duration=shortest:normalize=0[aout]"
    )
    
    cmd_assemble = [
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", os.path.join(temp_dir_out, "frame_%05d.png"),
        "-i", video1,
        "-i", video2,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        output_video
    ]
    
    subprocess.run(cmd_assemble, check=True)
    print(f"\nSuccessfully created {output_video}!")
    
    # Cleaning up
    print("Cleaning up temporary directories...")
    for d in [temp_dir_v1, temp_dir_v2, temp_dir_out]:
        if os.path.exists(d):
            shutil.rmtree(d)
    print("Cleanup complete.")

if __name__ == "__main__":
    main()
