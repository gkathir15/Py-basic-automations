import cv2
import numpy as np
import random
import math
from datetime import datetime

class SparkParticle:
    def __init__(self, x, y, angle):
        self.x = x
        self.y = y
        # Velocity pushes outward from the circle with some randomness
        speed = random.uniform(2.0, 8.0)
        self.vx = math.cos(angle) * speed + random.uniform(-1, 1)
        self.vy = math.sin(angle) * speed + random.uniform(-1, 1)
        # Sparks live for a random number of frames
        self.life = random.randint(10, 30)
        self.max_life = self.life
        # Color: Bright Yellow to Orange
        self.color = (0, random.randint(100, 200), 255) # BGR
        self.size = random.randint(1, 3)

    def update(self):
        self.x += self.vx
        self.y += self.vy
        self.life -= 1
        
        # Add a little "gravity" pulling sparks down
        self.vy += 0.2

    def draw(self, frame):
        if self.life > 0:
            # Fade out color as it dies
            alpha = self.life / self.max_life
            b, g, r = self.color
            fade_color = (int(b * alpha), int(g * alpha), int(r * alpha))
            cv2.circle(frame, (int(self.x), int(self.y)), self.size, fade_color, -1)

def main():
    # 1. LOAD YOUR FOOTAGE
    bg_path = "base_reality.mp4"   # The room you are standing in
    dest_path = "portal_world.mp4" # Where the portal leads
    
    bg_cap = cv2.VideoCapture(bg_path)
    dest_cap = cv2.VideoCapture(dest_path)
    
    if not bg_cap.isOpened() or not dest_cap.isOpened():
        print("Error: Could not load one or both videos. Check filenames.")
        return

    # Grab metadata from background video
    width = int(bg_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(bg_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = bg_cap.get(cv2.CAP_PROP_FPS)
    
    output_file = f"SlingRing_Render_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    out = cv2.VideoWriter(output_file, cv2.VideoWriter_fourcc(*'mp4v'), fps, (width, height))
    
    # Portal Settings
    portal_center = (int(width/2), int(height/2))
    max_radius = int(height * 0.35)
    current_radius = 0
    particles = []
    
    print("🪄 Conjuring portal... (Rendering frames)")
    frame_count = 0
    
    while True:
        ret_bg, bg_frame = bg_cap.read()
        ret_dest, dest_frame = dest_cap.read()
        
        # Loop the destination video if it's shorter than the background
        if not ret_dest and ret_bg:
            dest_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret_dest, dest_frame = dest_cap.read()
            
        if not ret_bg:
            break
            
        # Resize destination to match background exactly
        dest_frame = cv2.resize(dest_frame, (width, height))
        
        # 2. ANIMATE PORTAL OPENING
        # Portal grows until it hits max radius
        if current_radius < max_radius:
            current_radius += 4
            
        # 3. CREATE THE ALPHA MASK
        mask = np.zeros((height, width), dtype=np.uint8)
        cv2.circle(mask, portal_center, current_radius, 255, -1)
        
        # Feather the edge of the mask slightly
        mask = cv2.GaussianBlur(mask, (21, 21), 0)
        mask_3d = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR) / 255.0
        
        # 4. COMPOSITE THE VIDEOS
        # Background where mask is 0, Destination where mask is 1
        composite = (bg_frame * (1.0 - mask_3d)) + (dest_frame * mask_3d)
        composite = composite.astype(np.uint8)
        
        # 5. GENERATE AND DRAW SPARKS
        # Spawn new particles along the circumference of the circle
        if current_radius > 10:
            for _ in range(40): # Number of sparks spawned per frame
                angle = random.uniform(0, math.pi * 2)
                spawn_x = portal_center[0] + math.cos(angle) * current_radius
                spawn_y = portal_center[1] + math.sin(angle) * current_radius
                particles.append(SparkParticle(spawn_x, spawn_y, angle))
                
        # Draw sparks to a blank black layer (so we can make them glow)
        spark_layer = np.zeros_like(composite)
        
        # Update and draw living particles
        for p in particles:
            p.update()
            p.draw(spark_layer)
            
        # Remove dead particles to save memory
        particles = [p for p in particles if p.life > 0]
        
        # Add a Gaussian blur to the spark layer to create a "bloom/glow" effect
        glow = cv2.GaussianBlur(spark_layer, (15, 15), 0)
        final_sparks = cv2.add(spark_layer, glow)
        
        # 6. FINAL BLEND
        final_frame = cv2.add(composite, final_sparks)
        
        out.write(final_frame)
        frame_count += 1
        
        if frame_count % 30 == 0:
            print(f"  [>] Rendered {frame_count} frames...")

    bg_cap.release()
    dest_cap.release()
    out.release()
    print(f"✅ Magic complete! Saved to {output_file}")

if __name__ == "__main__":
    main()