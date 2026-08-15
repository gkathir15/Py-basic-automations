import cv2
import numpy as np
import math
from datetime import datetime
import os

def create_neon_edges(frame):
    # Convert to grayscale and find edges
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    
    # Thicken the edges slightly
    kernel = np.ones((3,3), np.uint8)
    edges = cv2.dilate(edges, kernel, iterations=1)
    
    # Create a neon cyan/blue glow for the edges
    neon = np.zeros_like(frame)
    neon[edges > 0] = [255, 200, 50] # BGR format: bright cyan
    
    # Blend the neon edges over the darkened original frame
    dark_bg = cv2.addWeighted(frame, 0.4, np.zeros_like(frame), 0, 0)
    return cv2.add(dark_bg, neon)

def create_rgb_pulse(frame, frame_idx, wave_speed=0.1, max_shift=15):
    # Calculate the shift amount using a sine wave
    shift = int(math.sin(frame_idx * wave_speed) * max_shift)
    
    # Split channels (OpenCV uses BGR order)
    b, g, r = cv2.split(frame)
    
    # Roll (shift) the Red and Blue channels in opposite directions
    r_shifted = np.roll(r, shift, axis=1)
    b_shifted = np.roll(b, -shift, axis=1)
    
    # Merge back together
    return cv2.merge([b_shifted, g, r_shifted])

def main():
    video_path = "IMG_1732.mp4"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Open the video to read properties
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print("Error: Could not open video.")
        return
        
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    print(f"Video loaded: {width}x{height} at {fps} FPS")
    
    experiments = ["01_Neon_Edges", "02_Ghost_Trails", "03_RGB_Pulse"]
    
    for exp_name in experiments:
        print(f"🎬 Rendering: {exp_name}...")
        
        # Reset video pointer to the beginning
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        
        output_file = f"{timestamp}_VFX_{exp_name}.mp4"
        # mp4v is the standard codec for mp4 output in OpenCV
        fourcc = cv2.VideoWriter_fourcc(*'mp4v') 
        out = cv2.VideoWriter(output_file, fourcc, fps, (width, height))
        
        # Variables for specific experiments
        trail_buffer = []
        trail_length = 15
        frame_count = 0
        
        while True:
            ret, frame = cap.read()
            if not ret:
                break
                
            result_frame = frame
            
            if exp_name == "01_Neon_Edges":
                result_frame = create_neon_edges(frame)
                
            elif exp_name == "02_Ghost_Trails":
                trail_buffer.append(frame)
                if len(trail_buffer) > trail_length:
                    trail_buffer.pop(0)
                # Compute the maximum pixel value across all frames in the buffer
                result_frame = np.max(np.array(trail_buffer), axis=0).astype(np.uint8)
                
            elif exp_name == "03_RGB_Pulse":
                result_frame = create_rgb_pulse(frame, frame_count, wave_speed=0.15, max_shift=25)
            
            out.write(result_frame)
            frame_count += 1
            
            if frame_count % 100 == 0:
                print(f"  [>] Processed {frame_count}/{total_frames} frames")
                
        out.release()
        print(f"✅ Saved {output_file}\n")
        
    cap.release()
    print("All VFX experiments rendered successfully!")

if __name__ == "__main__":
    main()