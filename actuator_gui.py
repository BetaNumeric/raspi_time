#!/usr/bin/env python3
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import cv2
from PIL import Image, ImageTk
import gpiod
import os
import random

# --- Configuration ---
CHIPPATH = "/dev/gpiochip0"
RPWM = 18   # GPIO pin for retract PWM
LPWM = 19   # GPIO pin for extend PWM
REN  = 23   # GPIO pin for retract enable
LEN  = 24   # GPIO pin for extend enable
PWM_HZ = 200    # PWM frequency (200Hz is a good balance for smoothness and CPU load on Pi 4)
EXTEND_SEC = 23.75      # Time to fully extend (0% to 100%)
RETRACT_SEC = 22.0    # Time to fully retract (100% to 0%)
HOME_TIMEOUT_SEC = 25.0 # Max time to spend on homing (retracting fully) before giving up

# Momentary center-off SPDT switch (ON)-OFF-(ON)
# Recommended wiring: switch common -> GND, throws -> GPIO pins below.
SW_EXTEND_IN = 6   # GPIO pin for extend switch
SW_RETRACT_IN = 5   # GPIO pin for retract switch
SW_ACTIVE_LOW = True    # Set to True if your switch connects the pin to GND when active, False if it connects to VCC. With internal pull-ups, active low is common for simple switches.
SW_POLL_MS = 30 # Polling interval for switch state (ms)
SW_DEBOUNCE_SAMPLES = 2 # Number of consecutive stable readings required to confirm switch state change (with 30ms poll, 2 samples = 60ms debounce)

class ActuatorHardware:
    def __init__(self, req):
        self.req = req
        self.stop_flag = False
        self.position_pct = 0.0  # Estimated position
        self.is_moving = False
        self.is_paused = False
        self.paused_target = None
        self.current_duty = 1.0
        self.lock = threading.Lock()

    def enable(self, on=True):
        if not self.req: return
        v = gpiod.line.Value.ACTIVE if on else gpiod.line.Value.INACTIVE
        self.req.set_value(REN, v)
        self.req.set_value(LEN, v)

    def stop_pwm(self):
        self.stop_flag = True
        if not self.req: return
        self.req.set_value(RPWM, gpiod.line.Value.INACTIVE)
        self.req.set_value(LPWM, gpiod.line.Value.INACTIVE)
    
    def pause(self):
        """Pause current movement"""
        self.is_paused = True
        self.stop_pwm()
    
    def resume(self):
        """Resume from pause"""
        self.is_paused = False

    def _soft_pwm_loop(self, pin, duty, duration, update_callback=None, stop_condition=None):
        """
        Runs PWM loop. 
        update_callback(delta_time) is called to update estimation.
        """
        self.stop_flag = False
        self.is_moving = True
        self.enable(True)
        
        # Ensure other channel is off
        other_pin = LPWM if pin == RPWM else RPWM
        if self.req:
            self.req.set_value(other_pin, gpiod.line.Value.INACTIVE)

        period = 1.0 / PWM_HZ
        
        start_time = time.time()
        last_tick = start_time
        
        try:
            while time.time() - start_time < duration:
                if self.stop_flag:
                    break
                # Read current duty dynamically
                effective_duty = max(0.0, min(1.0, getattr(self, 'current_duty', duty)))
                t_on = period * effective_duty
                t_off = period - t_on

                # PWM Cycle (reads updated duty each loop)
                if self.req:
                    if t_on > 0:
                        self.req.set_value(pin, gpiod.line.Value.ACTIVE)
                        time.sleep(t_on)
                    self.req.set_value(pin, gpiod.line.Value.INACTIVE)
                    if t_off > 0:
                        time.sleep(t_off)
                else:
                    # Simulation mode sleep
                    time.sleep(period)

                # Update Position Estimation
                now = time.time()
                dt = now - last_tick
                last_tick = now
                
                if update_callback:
                    update_callback(dt)

                # Stop condition check (e.g., reached target)
                if stop_condition and stop_condition():
                    break
                    
        finally:
            self.stop_pwm()
            self.enable(False)
            self.is_moving = False

    def move_diff(self, delta_pct, duty=1.0):
        """
        Move by a percentage difference.
        delta_pct > 0: Extend
        delta_pct < 0: Retract
        """
        # Determine target absolute position
        with self.lock:
            start_pct = self.position_pct
        target_pct = max(0.0, min(100.0, start_pct + delta_pct))

        pin = RPWM if delta_pct > 0 else LPWM
        direction = 1 if delta_pct > 0 else -1
        stroke_sec = EXTEND_SEC if delta_pct > 0 else RETRACT_SEC

        def update_pos(dt):
            # use current duty dynamically for speed calculation
            effective_duty = max(0.1, getattr(self, 'current_duty', duty))
            speed_pct_per_sec = (100.0 / stroke_sec) * effective_duty * direction
            with self.lock:
                self.position_pct += speed_pct_per_sec * dt
                self.position_pct = max(0.0, min(100.0, self.position_pct))

        def stop_condition():
            with self.lock:
                cur = self.position_pct
            if direction > 0:
                return cur >= target_pct - 0.5
            else:
                return cur <= target_pct + 0.5

        # Run PWM loop until stop_condition met
        self._soft_pwm_loop(pin, duty, 3600.0, update_pos, stop_condition=stop_condition)

    def manual_move(self, direction, duty=1.0):
        """
        Moves continuously until stopped.
        direction: 1 (Extend), -1 (Retract)
        """
        pin = RPWM if direction > 0 else LPWM
        stroke_sec = EXTEND_SEC if direction > 0 else RETRACT_SEC

        def update_pos(dt):
            effective_duty = max(0.1, getattr(self, 'current_duty', duty))
            speed_pct_per_sec = (100.0 / stroke_sec) * effective_duty * direction
            with self.lock:
                self.position_pct += speed_pct_per_sec * dt
                self.position_pct = max(0.0, min(100.0, self.position_pct))

        # Run for a long time (1 hour) until stop_flag is set
        self._soft_pwm_loop(pin, duty, 3600.0, update_pos)

    def home(self):
        """Retracts until timeout to reset position."""
        self.stop_flag = False
        self.is_moving = True
        self.enable(True)
        
        pin = LPWM
        if self.req:
            self.req.set_value(RPWM, gpiod.line.Value.INACTIVE)

        print("Homing...")
        period = 1.0 / PWM_HZ
        duty = 1.0
        t_on = period * duty
        t_off = period - t_on
        
        start_t = time.time()
        
        try:
            while time.time() - start_t < HOME_TIMEOUT_SEC:
                if self.stop_flag:
                    break
                if self.req:
                    if t_on > 0:
                        self.req.set_value(pin, gpiod.line.Value.ACTIVE)
                        time.sleep(t_on)
                    self.req.set_value(pin, gpiod.line.Value.INACTIVE)
                    if t_off > 0:
                        time.sleep(t_off)
                else:
                    time.sleep(period)
        finally:
            self.stop_pwm()
            self.enable(False)
            with self.lock:
                self.position_pct = 0.0
            self.is_moving = False
            print("Homed.")


class App:
    def __init__(self, root, req):
        self.root = root
        self.root.title("Actuator Control Station")
        self.root.geometry("900x700")

        self.hardware = ActuatorHardware(req)
        if req:
            self.hardware.enable(False)
            self.hardware.stop_pwm()

        self.video_path = None
        self.cap = None
        self.total_frames = 0
        self.video_opened = False
        self.tk_img = None
        self.last_displayed_frame_idx = -1  # Track which frame we last showed
        self.cached_canvas_size = (0, 0)  # Track canvas size changes
        # Placeholder "video" when no real video loaded
        self.placeholder_frames = 100
        # Video folder playback
        self.video_folder = None
        self.video_list = []
        self.video_index = 0
        
        # Fullscreen video
        self.fullscreen_window = None
        self.fullscreen_canvas = None
        self.fullscreen_tk_img = None
        self.is_fullscreen = False
        
        # Cycle mode
        self.is_cycling = False
        self.cycle_random_var = tk.BooleanVar(value=False)
        
        # Target tracking for dynamic updates
        self.active_target = None  # The target we're currently moving to
        self.last_target_check = 0
        
        # Countdown state
        self.countdown_job = None
        self.is_counting_down = False

        # Physical switch state (debounced)
        self.switch_raw_dir = 0
        self.switch_stable_dir = 0
        self.switch_same_count = 0
        
        # --- UI Construction ---
        
        # 1. Video Section (Toggleable)
        self.video_frame = tk.Frame(root, bg="black")
        self.canvas = tk.Canvas(self.video_frame, bg="black")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        # 2. Main Controls
        ctrl_frame = tk.Frame(root, padx=10, pady=10)
        ctrl_frame.pack(side=tk.BOTTOM, fill=tk.X)

        # Status Line
        status_row = tk.Frame(ctrl_frame)
        status_row.pack(fill=tk.X, pady=5)
        self.lbl_pos = tk.Label(status_row, text="Position: 0.0%", font=("Arial", 16, "bold"))
        self.lbl_pos.pack(side=tk.LEFT)

        self.lbl_switch = tk.Label(status_row, text="Switch: OFF", font=("Arial", 11, "bold"), fg="#444")
        self.lbl_switch.pack(side=tk.LEFT, padx=12)
        
        tk.Button(status_row, text="⛶ Fullscreen (F)", command=self.toggle_fullscreen, font=("Arial", 10)).pack(side=tk.RIGHT, padx=5)
        
        self.show_video_var = tk.BooleanVar(value=True)
        self.chk_video = tk.Checkbutton(status_row, text="Show Video", variable=self.show_video_var, command=self.toggle_video)
        self.chk_video.pack(side=tk.RIGHT)

        # Target Control
        target_frame = tk.LabelFrame(ctrl_frame, text="Auto Positioning (Click on timeline to set target)")
        target_frame.pack(fill=tk.X, pady=5)
        
        self.target_var = tk.DoubleVar(value=0.0)
        
        # Custom Timeline
        self.timeline_h = 40
        self.timeline = tk.Canvas(target_frame, height=self.timeline_h, bg="#e0e0e0", bd=0, highlightthickness=0)
        self.timeline.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10, pady=5)
        self.timeline.bind("<Button-1>", self.on_timeline_click)
        self.timeline.bind("<B1-Motion>", self.on_timeline_click)
        self.timeline.bind("<Configure>", self.draw_timeline)
        
        # Countdown UI
        tk.Label(target_frame, text="Start Delay (s):").pack(side=tk.LEFT, padx=(10, 2))
        self.delay_var = tk.IntVar(value=0)
        tk.Spinbox(target_frame, from_=0, to=60, textvariable=self.delay_var, width=3).pack(side=tk.LEFT, padx=2)

        self.btn_play_pause = tk.Button(target_frame, text="▶ PLAY", command=self.on_play_pause, bg="#4CAF50", fg="white", font=("Arial", 12, "bold"), width=15)
        self.btn_play_pause.pack(side=tk.LEFT, padx=5)

        # Advanced / Manual Control
        manual_frame = tk.LabelFrame(ctrl_frame, text="Manual Control")
        manual_frame.pack(fill=tk.X, pady=5)
        
        # Speed Control
        tk.Label(manual_frame, text="Speed / Duty:").pack(side=tk.LEFT, padx=5)
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speed_scale = tk.Scale(manual_frame, variable=self.speed_var, from_=0.1, to=1.0, resolution=0.05, orient=tk.HORIZONTAL, length=150)
        self.speed_scale.pack(side=tk.LEFT, padx=5)
        # Apply speed changes immediately while moving
        try:
            # trace_add is preferred on newer Tk
            self.speed_var.trace_add('write', lambda *args: self.on_speed_change())
        except Exception:
            try:
                self.speed_var.trace('w', lambda *args: self.on_speed_change())
            except Exception:
                pass
        
        # Jog Buttons
        tk.Label(manual_frame, text="       Jog:").pack(side=tk.LEFT)
        
        self.btn_retract = tk.Button(manual_frame, text="<<< RETRACT", bg="#DDD")
        self.btn_retract.pack(side=tk.LEFT, padx=5)
        self.btn_retract.bind('<ButtonPress>', lambda e: self.start_jog(-1))
        self.btn_retract.bind('<ButtonRelease>', lambda e: self.stop_jog())

        self.btn_extend = tk.Button(manual_frame, text="EXTEND >>>", bg="#DDD")
        self.btn_extend.pack(side=tk.LEFT, padx=5)
        self.btn_extend.bind('<ButtonPress>', lambda e: self.start_jog(1))
        self.btn_extend.bind('<ButtonRelease>', lambda e: self.stop_jog())

        # Cycle Control
        cycle_frame = tk.LabelFrame(ctrl_frame, text="Cycle Mode (Extend/Retract Loop)")
        cycle_frame.pack(fill=tk.X, pady=5)
        
        tk.Label(cycle_frame, text="Pause at 100%:").pack(side=tk.LEFT, padx=5)
        self.cycle_pause_extend_var = tk.DoubleVar(value=2.0)
        tk.Spinbox(cycle_frame, from_=0, to=60, textvariable=self.cycle_pause_extend_var, width=5).pack(side=tk.LEFT, padx=2)
        tk.Label(cycle_frame, text="s").pack(side=tk.LEFT, padx=2)
        
        tk.Label(cycle_frame, text="       Pause at 0%:").pack(side=tk.LEFT, padx=5)
        self.cycle_pause_retract_var = tk.DoubleVar(value=2.0)
        tk.Spinbox(cycle_frame, from_=0, to=60, textvariable=self.cycle_pause_retract_var, width=5).pack(side=tk.LEFT, padx=2)
        tk.Label(cycle_frame, text="s").pack(side=tk.LEFT, padx=2)
        
        # Randomize videos in folder playback
        self.chk_random = tk.Checkbutton(cycle_frame, text="Randomize videos", variable=self.cycle_random_var)
        self.chk_random.pack(side=tk.LEFT, padx=8)

        self.btn_cycle = tk.Button(cycle_frame, text="▶ START CYCLE", command=self.on_cycle_toggle, bg="#2196F3", fg="white", font=("Arial", 11, "bold"), width=15)
        self.btn_cycle.pack(side=tk.RIGHT, padx=5)
        action_frame = tk.Frame(ctrl_frame, pady=10)
        action_frame.pack(fill=tk.X)

        tk.Button(action_frame, text="Load Video...", command=self.load_video).pack(side=tk.LEFT)
        tk.Button(action_frame, text="Load Folder...", command=self.load_video_folder).pack(side=tk.LEFT, padx=5)
        tk.Button(action_frame, text="Force Home (Zero)", command=self.on_home).pack(side=tk.LEFT, padx=10)
        
        self.btn_stop = tk.Button(action_frame, text="EMERGENCY STOP", command=self.on_stop, bg="red", fg="white", font=("Arial", 14, "bold"))
        self.btn_stop.pack(side=tk.RIGHT)
        
        # Initialize Layout
        self.toggle_video()
        
        # Start Update Loop
        self.update_ui_interval = 50
        self.root.after(self.update_ui_interval, self.update_loop)
        self.root.after(SW_POLL_MS, self.poll_toggle_switch)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Keyboard shortcuts
        self.root.bind('<f>', lambda e: self.toggle_fullscreen())
        self.root.bind('<F>', lambda e: self.toggle_fullscreen())

    def toggle_video(self):
        if self.show_video_var.get():
            self.video_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)
            # Re-order to make sure controls stay at bottom (pack order matters)
            # Actually since video is packed TOP and controls BOTTOM, it should be fine.
        else:
            self.video_frame.pack_forget()
    
    def toggle_fullscreen(self):
        if not self.video_opened:
            messagebox.showinfo("No Video", "Please load a video first.")
            return
        
        if self.is_fullscreen:
            # Exit fullscreen
            if self.fullscreen_window:
                self.fullscreen_window.destroy()
                self.fullscreen_window = None
                self.fullscreen_canvas = None
                self.fullscreen_tk_img = None
            self.is_fullscreen = False
        else:
            # Enter fullscreen
            self.is_fullscreen = True
            self.fullscreen_window = tk.Toplevel(self.root)
            self.fullscreen_window.title("Video - Fullscreen")
            self.fullscreen_window.attributes('-fullscreen', True)
            self.fullscreen_window.configure(bg='black', cursor="none") # Hide cursor
            
            # Create canvas for video
            self.fullscreen_canvas = tk.Canvas(self.fullscreen_window, bg="black", highlightthickness=0)
            self.fullscreen_canvas.pack(fill=tk.BOTH, expand=True)
            
            # Bind ESC and click to exit
            self.fullscreen_window.bind('<Escape>', lambda e: self.toggle_fullscreen())
            self.fullscreen_window.bind('<Button-1>', lambda e: self.toggle_fullscreen())
            self.fullscreen_window.bind('<f>', lambda e: self.toggle_fullscreen())
            self.fullscreen_window.bind('<F>', lambda e: self.toggle_fullscreen())
            
            # Force immediate update
            self.fullscreen_window.update()
            self.show_frame_at_pct(self.hardware.position_pct, force_update=True)

    def on_timeline_click(self, event):
        w = self.timeline.winfo_width()
        if w < 1: return
        x = max(0, min(w, event.x))
        pct = (x / w) * 100.0
        self.target_var.set(pct)
        # Immediate redraw not strictly necessary as loop handles it, but feels snappier
        self.draw_timeline(current_pct=self.hardware.position_pct)

    def on_speed_change(self):
        # Immediately apply slider duty to hardware so PWM loop reads it
        try:
            duty = float(self.speed_var.get())
        except Exception:
            duty = 1.0
        # clamp
        duty = max(0.0, min(1.0, duty))
        self.hardware.current_duty = duty

    def read_toggle_switch_direction(self):
        """
        Returns direction from physical SPDT switch:
          1 = extend, -1 = retract, 0 = center/off/invalid
        """
        req = self.hardware.req
        if not req:
            return 0

        try:
            ext_val = req.get_value(SW_EXTEND_IN)
            ret_val = req.get_value(SW_RETRACT_IN)
        except Exception:
            return 0

        ext_active = (ext_val == gpiod.line.Value.ACTIVE)
        ret_active = (ret_val == gpiod.line.Value.ACTIVE)

        if SW_ACTIVE_LOW:
            ext_pressed = not ext_active
            ret_pressed = not ret_active
        else:
            ext_pressed = ext_active
            ret_pressed = ret_active

        if ext_pressed and not ret_pressed:
            return 1
        if ret_pressed and not ext_pressed:
            return -1
        return 0

    def apply_switch_direction(self, direction):
        """Apply debounced switch command."""
        if direction == 0:
            self.hardware.stop_pwm()
            return

        # Physical switch takes control immediately.
        if self.is_counting_down:
            self.cancel_countdown()

        if self.is_cycling:
            self.is_cycling = False
            self.btn_cycle.config(text="▶ START CYCLE", bg="#2196F3")

        self.active_target = None
        self.hardware.stop_pwm()
        self.hardware.resume()

        duty = float(self.speed_var.get())
        t = threading.Thread(target=self.hardware.manual_move, args=(direction, duty))
        t.daemon = True
        t.start()

    def is_switch_override_active(self):
        return self.switch_stable_dir != 0

    def poll_toggle_switch(self):
        """Poll and debounce physical toggle switch state."""
        direction_now = self.read_toggle_switch_direction()

        if direction_now == self.switch_raw_dir:
            self.switch_same_count += 1
        else:
            self.switch_raw_dir = direction_now
            self.switch_same_count = 1

        if self.switch_same_count >= SW_DEBOUNCE_SAMPLES and direction_now != self.switch_stable_dir:
            self.switch_stable_dir = direction_now
            self.apply_switch_direction(direction_now)

        self.root.after(SW_POLL_MS, self.poll_toggle_switch)

    def draw_timeline(self, event=None, current_pct=None):
        w = self.timeline.winfo_width()
        h = self.timeline_h
        if w < 1: return

        if current_pct is None:
             current_pct = self.hardware.position_pct
        
        self.timeline.delete("all")
        
        # 1. Base Track
        y_center = h // 2
        self.timeline.create_line(10, y_center, w-10, y_center, fill="#bbb", width=4, capstyle=tk.ROUND)

        # 2. Current Position (Filled Bar)
        # Map 0..100 to 10..w-10
        def get_x(p):
            return 10 + (p / 100.0) * (w - 20)

        cur_x = get_x(current_pct)
        
        # Draw "Progress" style bar
        self.timeline.create_line(10, y_center, cur_x, y_center, fill="#4CAF50", width=4, capstyle=tk.ROUND)
        # Draw Handle for Current
        self.timeline.create_oval(cur_x-6, y_center-6, cur_x+6, y_center+6, fill="#4CAF50", outline="white", width=1)
        
        # 3. Target Position (Red Marker)
        tgt_pct = self.target_var.get()
        tgt_x = get_x(tgt_pct)
        
        # Target Pointer (Red Triangle)
        # Drawing it slightly above the track
        self.timeline.create_polygon(tgt_x, y_center-8, tgt_x-5, y_center-16, tgt_x+5, y_center-16, fill="red", outline="darkred")
        
        # Target text hint
        # self.timeline.create_text(tgt_x, y_center-25, text=f"{tgt_pct:.0f}%", fill="red", font=("Arial", 8))


    def load_video(self):
        path = filedialog.askopenfilename(filetypes=[("Video files", "*.mp4 *.avi *.mov *.mkv"), ("All files", "*.*")])
        if path:
            self.load_video_path(path)

    def load_video_path(self, path):
        if not path:
            return
        try:
            cap = cv2.VideoCapture(path)
            if cap.isOpened():
                if self.cap:
                    try:
                        self.cap.release()
                    except Exception:
                        pass
                self.cap = cap
                self.video_path = path
                self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
                self.video_opened = True
                self.last_displayed_frame_idx = -1  # Reset cache
                print(f"Video Loaded: {self.total_frames} frames - {path}")
                # Auto-show video if loaded
                self.show_video_var.set(True)
                self.toggle_video()
            else:
                cap.release()
                self.video_opened = False
                messagebox.showerror("Error", "Could not open video file.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def load_video_folder(self):
        folder = filedialog.askdirectory()
        if not folder:
            return
        # Collect supported video files
        exts = ('.mp4', '.avi', '.mov', '.mkv')
        files = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(exts)]
        files.sort()
        if not files:
            messagebox.showinfo("No Videos", "No supported video files found in folder.")
            return
        self.video_folder = folder
        self.video_list = files
        self.video_index = 0
        # Load first video on main thread
        self.load_video_path(self.video_list[self.video_index])

    def show_frame_at_pct(self, pct, force_update=False):
        if not self.video_opened or not self.cap:
            # Draw placeholder frame count mapped to position
            # Map pct (0..100) to frame index (0..placeholder_frames-1)
            pct = max(0.0, min(100.0, pct))
            frame_idx = int((pct / 100.0) * (self.placeholder_frames - 1))

            # Normal canvas
            if self.show_video_var.get():
                cw = self.canvas.winfo_width()
                ch = self.canvas.winfo_height()
                if cw >= 10 and ch >= 10:
                    canvas_size = (cw, ch)
                    size_changed = canvas_size != self.cached_canvas_size
                    if not force_update and frame_idx == self.last_displayed_frame_idx and not size_changed:
                        pass
                    else:
                        # draw placeholder
                        self.canvas.delete("all")
                        self.canvas.create_rectangle(0, 0, cw, ch, fill='black')
                        txt = f"F {frame_idx}"
                        self.canvas.create_text(cw//2, ch//2, text=txt, fill='white', font=("Arial", max(12, ch//6), "bold"))
                        self.last_displayed_frame_idx = frame_idx
                        self.cached_canvas_size = canvas_size

            # Fullscreen placeholder
            if self.is_fullscreen and self.fullscreen_canvas:
                fs_w = self.fullscreen_canvas.winfo_width()
                fs_h = self.fullscreen_canvas.winfo_height()
                if fs_w >= 10 and fs_h >= 10:
                    self.fullscreen_canvas.delete("all")
                    self.fullscreen_canvas.create_rectangle(0, 0, fs_w, fs_h, fill='black')
                    txt = f"Frame {frame_idx}"
                    self.fullscreen_canvas.create_text(fs_w//2, fs_h//2, text=txt, fill='white', font=("Arial", max(24, fs_h//8), "bold"))
            return
        
        pct = max(0.0, min(100.0, pct))
        frame_idx = int((pct / 100.0) * (self.total_frames - 1))
        
        # Update normal view
        if self.show_video_var.get():
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            if cw >= 10 and ch >= 10:
                canvas_size = (cw, ch)
                size_changed = canvas_size != self.cached_canvas_size
                
                # Skip if same frame and canvas size hasn't changed (optimization)
                if not force_update and frame_idx == self.last_displayed_frame_idx and not size_changed:
                    pass
                else:
                    self._draw_frame_on_canvas(frame_idx, self.canvas, cw, ch)
                    self.last_displayed_frame_idx = frame_idx
                    self.cached_canvas_size = canvas_size
        
        # Update fullscreen view
        if self.is_fullscreen and self.fullscreen_canvas:
            fs_w = self.fullscreen_canvas.winfo_width()
            fs_h = self.fullscreen_canvas.winfo_height()
            if fs_w >= 10 and fs_h >= 10:
                self._draw_frame_on_canvas(frame_idx, self.fullscreen_canvas, fs_w, fs_h, is_fullscreen=True)
    
    def _draw_frame_on_canvas(self, frame_idx, canvas, width, height, is_fullscreen=False):
        """Helper to draw a video frame on a canvas with optimizations"""
        # OPTIMIZATION 1: Smart Seek
        # Check if the next frame in the stream is exactly what we need.
        # This avoids the expensive 'seek' operation if frames are sequential.
        current_pos = self.cap.get(cv2.CAP_PROP_POS_FRAMES)
        if frame_idx != int(current_pos):
             self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)

        ret, frame = self.cap.read()
        if ret:
            # OPTIMIZATION 2: Resize BEFORE conversion
            # Resize using optimized C++ OpenCV function before creating Python PIL object
            h, w = frame.shape[:2]
            ratio = min(width/w, height/h)
            new_w = max(1, int(w * ratio))
            new_h = max(1, int(h * ratio))
            
            # Linear interpolation is fast enough for Pi 4
            frame_resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

            # Convert color space
            frame_rgb = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame_rgb)
            
            # Store image reference
            if is_fullscreen:
                self.fullscreen_tk_img = ImageTk.PhotoImage(img)
                img_ref = self.fullscreen_tk_img
            else:
                self.tk_img = ImageTk.PhotoImage(img)
                img_ref = self.tk_img
            
            # Update canvas
            canvas.delete("all")
            x_center = width // 2
            y_center = height // 2
            canvas.create_image(x_center, y_center, image=img_ref)

    def update_loop(self):
        with self.hardware.lock:
            current_pct = self.hardware.position_pct
            is_moving = self.hardware.is_moving
            is_paused = self.hardware.is_paused

        switch_dir = self.switch_stable_dir

        self.lbl_pos.config(text=f"Position: {current_pct:.1f}%")

        if switch_dir > 0:
            self.lbl_switch.config(text="Switch: EXTEND", fg="#2E7D32")
        elif switch_dir < 0:
            self.lbl_switch.config(text="Switch: RETRACT", fg="#C62828")
        else:
            self.lbl_switch.config(text="Switch: OFF", fg="#444")
        
        # Update button appearance
        if self.is_counting_down:
             self.btn_play_pause.config(text="✖ CANCEL", bg="#FF5722")
        elif is_moving and not is_paused:
            self.btn_play_pause.config(text="❚❚ PAUSE", bg="#FF9800")
        else:
            self.btn_play_pause.config(text="▶ PLAY", bg="#4CAF50")
        
        # Check if target changed during active movement (every 100ms to avoid excessive checks)
        current_time = time.time()
        if is_moving and not is_paused and self.active_target is not None and not self.is_switch_override_active():
            if (current_time - self.last_target_check) > 0.1:
                self.last_target_check = current_time
                new_target = self.target_var.get()
                # If target changed significantly, restart movement
                if abs(new_target - self.active_target) > 1.0:
                    print(f"Target changed from {self.active_target:.1f}% to {new_target:.1f}% - restarting movement")
                    self.restart_movement(new_target)
        
        # Redraw timeline with latest data
        self.draw_timeline(current_pct=current_pct)
        
        # Update video (normal or fullscreen) or placeholder when video area is visible
        if (self.show_video_var.get() or self.is_fullscreen):
            self.show_frame_at_pct(current_pct)
            
        self.root.after(self.update_ui_interval, self.update_loop)

    def restart_movement(self, new_target):
        """Safely restart movement to a new target"""
        if self.is_switch_override_active():
            self.active_target = None
            return

        # Stop current movement with brief pause for safety
        self.hardware.stop_pwm()
        time.sleep(0.1)  # Brief pause to allow motor to settle
        
        # Start new movement
        current = self.hardware.position_pct
        delta = new_target - current
        duty = self.speed_var.get()
        
        if abs(delta) > 0.5:
            self.active_target = new_target
            self.hardware.resume()
            t = threading.Thread(target=self.hardware.move_diff, args=(delta, duty))
            t.daemon = True
            t.start()
        else:
            self.active_target = None
    
    def on_play_pause(self):
        if self.is_switch_override_active():
            self.active_target = None
            return

        # 1. If currently counting down, cancel it
        if self.is_counting_down:
            self.cancel_countdown()
            return

        with self.hardware.lock:
            is_moving = self.hardware.is_moving
            is_paused = self.hardware.is_paused
        
        # 2. If currently moving (not paused), pause it
        if is_moving and not is_paused:
            target = self.target_var.get()
            self.hardware.paused_target = target
            self.hardware.pause()
            print("Paused")
            return
        
        # 3. Check for delay before starting (only if not resuming from pause)
        delay = self.delay_var.get()
        if delay > 0 and not is_paused: 
            self.start_countdown(delay)
            return
            
        # 4. If paused or not moving (and no delay), play/resume immediately
        self.execute_movement()

    def start_countdown(self, seconds):
        self.is_counting_down = True
        self.countdown_step(seconds)

    def countdown_step(self, seconds_left):
        if not self.is_counting_down:
            return
        
        if seconds_left > 0:
            self.btn_play_pause.config(text=f"Starting in {seconds_left}...", bg="#FFC107")
            self.countdown_job = self.root.after(1000, lambda: self.countdown_step(seconds_left - 1))
        else:
            self.is_counting_down = False
            self.execute_movement()

    def cancel_countdown(self):
        self.is_counting_down = False
        if self.countdown_job:
            self.root.after_cancel(self.countdown_job)
            self.countdown_job = None
        self.btn_play_pause.config(text="▶ PLAY", bg="#4CAF50")

    def execute_movement(self):
        if self.is_switch_override_active():
            self.active_target = None
            return

        target = self.target_var.get()
        current = self.hardware.position_pct
        delta = target - current
        duty = self.speed_var.get()
        
        if abs(delta) < 0.5:
            print("Target reached.")
            self.active_target = None
            return
        
        # Resume movement and track the target
        self.active_target = target
        self.hardware.resume()
        t = threading.Thread(target=self.hardware.move_diff, args=(delta, duty))
        t.daemon = True
        t.start()

    def on_cycle_toggle(self):
        if self.is_cycling:
            self.is_cycling = False
            self.hardware.stop_pwm()
            self.btn_cycle.config(text="▶ START CYCLE", bg="#2196F3")
        else:
            if self.is_switch_override_active():
                return
            self.is_cycling = True
            self.btn_cycle.config(text="❚❚ STOP CYCLE", bg="#FF5722")
            pause_extend = float(self.cycle_pause_extend_var.get())
            pause_retract = float(self.cycle_pause_retract_var.get())
            duty = float(self.speed_var.get())
            randomize_videos = bool(self.cycle_random_var.get())
            t = threading.Thread(target=self.cycle_loop, args=(pause_extend, pause_retract, duty, randomize_videos))
            t.daemon = True
            t.start()
    
    def cycle_loop(self, pause_extend, pause_retract, duty, randomize_videos):
        """Continuously cycle the actuator extend/retract"""
        while self.is_cycling:
            try:
                # Extend to 100%
                if not self.is_cycling:
                    break
                print("Cycle: Extending to 100%...")
                self.hardware.resume()
                self.hardware.move_diff(100.0, duty)
                
                # Pause at 100%
                for _ in range(int(pause_extend * 10)):
                    if not self.is_cycling:
                        break
                    time.sleep(0.1)
                
                # After pause at 100%, advance folder video (if any)
                if self.video_list:
                    try:
                        if randomize_videos:
                            # pick a random index different from current (if possible)
                            if len(self.video_list) == 1:
                                self.video_index = 0
                            else:
                                old = self.video_index
                                choices = list(range(len(self.video_list)))
                                choices.remove(old)
                                self.video_index = random.choice(choices)
                        else:
                            self.video_index = (self.video_index + 1) % len(self.video_list)
                        next_vid = self.video_list[self.video_index]
                        # Schedule load on main thread
                        self.root.after(0, lambda p=next_vid: self.load_video_path(p))
                    except Exception as e:
                        print(f"Error advancing video list: {e}")

                if not self.is_cycling:
                    break
                
                # Retract to 0%
                print("Cycle: Retracting to 0%...")
                self.hardware.resume()
                self.hardware.move_diff(-100.0, duty)
                
                # Pause at 0%
                for _ in range(int(pause_retract * 10)):
                    if not self.is_cycling:
                        break
                    time.sleep(0.1)
                # After pause at 0%, advance folder video (if any)
                if self.video_list:
                    try:
                        if randomize_videos:
                            if len(self.video_list) == 1:
                                self.video_index = 0
                            else:
                                old = self.video_index
                                choices = list(range(len(self.video_list)))
                                choices.remove(old)
                                self.video_index = random.choice(choices)
                        else:
                            self.video_index = (self.video_index + 1) % len(self.video_list)
                        next_vid = self.video_list[self.video_index]
                        self.root.after(0, lambda p=next_vid: self.load_video_path(p))
                    except Exception as e:
                        print(f"Error advancing video list: {e}")
                    
            except Exception as e:
                print(f"Cycle error: {e}")
                break
        
        # Clean up when done
        self.is_cycling = False
        self.hardware.stop_pwm()
        self.root.after(0, lambda: self.btn_cycle.config(text="▶ START CYCLE", bg="#2196F3"))
        print("Cycle stopped.")

    def start_jog(self, direction):
        if self.is_switch_override_active():
            return
        if self.hardware.is_moving: return
        duty = self.speed_var.get()
        t = threading.Thread(target=self.hardware.manual_move, args=(direction, duty))
        t.daemon = True
        t.start()
        
    def stop_jog(self):
        self.hardware.stop_pwm()

    def on_home(self):
        if self.is_switch_override_active():
            return
        if self.hardware.is_moving:
            return
            
        if messagebox.askyesno("Home", "RETRACT fully to find Zero position?"):
            t = threading.Thread(target=self.hardware.home)
            t.daemon = True
            t.start()

    def on_stop(self):
        if self.is_cycling:
            self.is_cycling = False
        if self.is_counting_down:
            self.cancel_countdown()
        self.hardware.stop_pwm()
        self.hardware.enable(False)

    def on_close(self):
        self.on_stop()
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        if self.fullscreen_window:
            try:
                self.fullscreen_window.destroy()
            except Exception:
                pass
            self.fullscreen_window = None
        self.root.quit()

def main():
    output_offsets = (RPWM, LPWM, REN, LEN)
    input_offsets = (SW_EXTEND_IN, SW_RETRACT_IN)
    
    # Try to open gpiod
    req = None
    try:
        req = gpiod.request_lines(
            CHIPPATH,
            consumer="actuator-gui",
            config={
                output_offsets: gpiod.LineSettings(
                    direction=gpiod.line.Direction.OUTPUT,
                    output_value=gpiod.line.Value.INACTIVE
                ),
                input_offsets: gpiod.LineSettings(
                    direction=gpiod.line.Direction.INPUT,
                    bias=gpiod.line.Bias.PULL_UP
                )
            }
        )
    except Exception as e:
        print(f"Warning: Could not open GPIO. Running in Simulation Mode. ({e})")
    
    try:
        root = tk.Tk()
        app = App(root, req)
        root.mainloop()
    finally:
        if req:
            req.release()

if __name__ == "__main__":
    main()
