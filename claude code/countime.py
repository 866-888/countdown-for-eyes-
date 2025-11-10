import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image  # for tray icon creation only
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# Optional: system tray & toast & video
try:
    import pystray
    from PIL import Image as PILImage
    TRAY_AVAILABLE = True
except Exception:
    TRAY_AVAILABLE = False

try:
    from win10toast import ToastNotifier
    TOAST_AVAILABLE = True
except Exception:
    TOAST_AVAILABLE = False

# Video playback backends
try:
    from tkintervideo import TkinterVideo
    TKINTERVIDEO_AVAILABLE = True
except Exception:
    TKINTERVIDEO_AVAILABLE = False

try:
    import vlc
    VLC_AVAILABLE = True
except Exception:
    VLC_AVAILABLE = False

try:
    import cv2
    OPENCV_AVAILABLE = True
except Exception:
    OPENCV_AVAILABLE = False

# --- NEW IMPORTS FOR LOCK SCREEN ---
import subprocess # For macOS lock screen
try:
    import ctypes # For Windows lock screen
    WINDOWS_LOCK_AVAILABLE = True
except Exception:
    WINDOWS_LOCK_AVAILABLE = False

# For checking screen lock status on Windows
if sys.platform.startswith("win"):
    try:
        import win32event
        import win32gui
        import win32con
        import win32api
        WINDOWS_SCREEN_STATUS_AVAILABLE = True
    except ImportError:
        WINDOWS_SCREEN_STATUS_AVAILABLE = False
else:
    WINDOWS_SCREEN_STATUS_AVAILABLE = False
# --- END NEW IMPORTS ---


def prepare_vlc_on_windows() -> bool:
    """Try to make python-vlc find libvlc on Windows by adding DLL dirs.
    Returns True if a candidate directory was added.
    """
    if not sys.platform.startswith("win"):
        return False
    candidates = [
        os.environ.get("VLC_HOME"),
        r"C:\\Program Files\\VideoLAN\\VLC",
        r"C:\\Program Files (x86)\\VideoLAN\\VLC",
    ]
    for base in candidates:
        if not base:
            continue
        libvlc = os.path.join(base, "libvlc.dll")
        if os.path.exists(libvlc):
            try:
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(base)
                os.environ["PATH"] = base + os.pathsep + os.environ.get("PATH", "")
                plugins = os.path.join(base, "plugins")
                if os.path.isdir(plugins):
                    os.environ["VLC_PLUGIN_PATH"] = plugins
                return True
            except Exception:
                pass
    return False


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def read_config():
    if not os.path.exists(CONFIG_PATH):
        return {
            "use_seconds": 1500,  # 25min
            "rest_seconds": 300,   # 5min
            "popup_width": 600,
            "popup_height": 400,
            "video_path": "",
            "auto_start_countdown": True,
            "windows_autostart": False,
            "enable_tray": True,
            "enable_toast": True,
            "fullscreen_rest": False,
            "enable_lock_screen": False, # 新增
        }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            # 兼容旧配置，如果缺少新字段则添加
            if "enable_lock_screen" not in cfg:
                cfg["enable_lock_screen"] = False
            return cfg
    except Exception:
        return {
            "use_seconds": 1500,
            "rest_seconds": 300,
            "popup_width": 600,
            "popup_height": 400,
            "video_path": "",
            "auto_start_countdown": True,
            "windows_autostart": False,
            "enable_tray": True,
            "enable_toast": True,
            "fullscreen_rest": False,
            "enable_lock_screen": False, # 新增
        }


def write_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def set_windows_autostart(enable: bool):
    # Add or remove from HKCU\Software\Microsoft\Windows\CurrentVersion\Run
    try:
        import winreg

        run_key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key_path, 0, winreg.KEY_ALL_ACCESS) as key:
            app_name = "CountdownApp"
            if enable:
                python_exe = sys.executable
                script_path = os.path.abspath(__file__)
                # Use pythonw if available to avoid console window
                if python_exe.endswith("python.exe") and os.path.exists(python_exe.replace("python.exe", "pythonw.exe")):
                    python_exe = python_exe.replace("python.exe", "pythonw.exe")
                cmd = f'"{python_exe}" "{script_path}"'
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass
        return True, None
    except Exception as e:
        return False, str(e)


class CountdownState:
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    LOCKED_PAUSED = "locked_paused" # New state for when screen is locked during rest phase


class Phase:
    USE = "使用时间"
    REST = "休息时间"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("护眼倒计时")
        self.geometry("720x520")
        self.resizable(False, False)

        self.config_data = read_config()

        # Internal state
        self.current_phase = Phase.USE
        self.remaining_seconds = int(self.config_data.get("use_seconds", 1500))
        self.state = CountdownState.IDLE
        self._ticker = None
        self._tray_icon = None
        self._toaster = ToastNotifier() if (os.name == "nt" and TOAST_AVAILABLE) else None
        self._screen_locked_check_id = None # For Windows screen lock status check

        self._build_ui()
        self._apply_config_to_ui()

        # Tray
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
        if self.config_data.get("enable_tray", True):
            self.after(200, self._start_tray)

        # Autostart countdown on launch
        if self.config_data.get("auto_start_countdown", True):
            self.start_countdown()

        # Apply Windows autostart if set
        if os.name == "nt":
            ok, err = set_windows_autostart(bool(self.config_data.get("windows_autostart", False)))
            if not ok and self.config_data.get("windows_autostart", False):
                messagebox.showwarning("开机自启失败", f"设置开机自启时出现问题: {err}")

        # Register for Windows Session Notifications (for screen lock/unlock)
        if WINDOWS_SCREEN_STATUS_AVAILABLE:
            self.hwnd = None
            try:
                self.hwnd = win32gui.CreateWindow(
                    win32gui.RegisterClass(win32gui.WNDCLASS()),
                    "CountimeHiddenWindow",
                    0,
                    0, 0, 0, 0,
                    0, 0,
                    win32gui.GetModuleHandle(None),
                    None
                )
                win32gui.SetWindowLong(self.hwnd, win32con.GWL_WNDPROC, self._wnd_proc)
                win32gui.RegisterSessionNotification(self.hwnd, win32con.NOTIFY_FOR_ALL_SESSIONS)
                print("Windows session notifications registered.")
            except Exception as e:
                print(f"Failed to register Windows session notifications: {e}")
                self.hwnd = None # Ensure it's None if registration fails

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_WTSSESSION_CHANGE:
            if wparam == win32con.WTS_SESSION_LOCK:
                self.after(0, self._on_screen_lock)
            elif wparam == win32con.WTS_SESSION_UNLOCK:
                self.after(0, self._on_screen_unlock)
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _on_screen_lock(self):
        print("Screen locked event detected.")
        if self.current_phase == Phase.REST and self.state == CountdownState.RUNNING:
            self.state = CountdownState.LOCKED_PAUSED
            print("Countdown paused due to screen lock during rest phase.")
            self._update_labels() # Update UI to reflect paused state
            self.pause_btn.config(text="继续 (已锁屏)", state=tk.DISABLED) # Update button text
            self.start_btn.config(state=tk.DISABLED)

    def _on_screen_unlock(self):
        print("Screen unlocked event detected.")
        if self.current_phase == Phase.USE and self.state == CountdownState.LOCKED_PAUSED:
            # This condition is for when the rest phase *just* ended, but use phase couldn't start because of lock.
            # Now unlocked, start the use phase countdown.
            print("Screen unlocked, resuming use phase countdown.")
            self.state = CountdownState.RUNNING
            self.start_countdown()
        elif self.current_phase == Phase.REST and self.state == CountdownState.LOCKED_PAUSED:
            # If screen was locked during rest, and is now unlocked, resume rest countdown
            print("Screen unlocked, resuming rest phase countdown.")
            self.state = CountdownState.RUNNING
            self.start_countdown()
        
        # In any case of unlock, if we were showing a special "paused due to lock" status, clear it
        if self.pause_btn["text"] == "继续 (已锁屏)":
            self.pause_btn.config(text="暂停", state=tk.NORMAL)
        
        self._update_labels()


    def _build_ui(self):
        container = ttk.Frame(self, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        # Title Row
        title = ttk.Label(container, text="应用设置", font=("Microsoft YaHei", 16, "bold"))
        title.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        # Time settings with digital clock style
        ttk.Label(container, text="使用时间").grid(row=1, column=0, sticky="w")
        self.use_time_frame = ttk.Frame(container)
        self.use_time_frame.grid(row=1, column=1, sticky="w")
        self._create_time_selector(self.use_time_frame, "use", 25)

        ttk.Label(container, text="休息时间").grid(row=1, column=2, sticky="w", padx=(24, 0))
        self.rest_time_frame = ttk.Frame(container)
        self.rest_time_frame.grid(row=1, column=3, sticky="w")
        self._create_time_selector(self.rest_time_frame, "rest", 5)

        # Popup size
        ttk.Label(container, text="弹窗尺寸 宽×高").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.popup_w_var = tk.IntVar(value=600)
        self.popup_h_var = tk.IntVar(value=400)
        self.popup_w_entry = ttk.Spinbox(container, from_=200, to=3840, textvariable=self.popup_w_var, width=8)
        self.popup_h_entry = ttk.Spinbox(container, from_=200, to=2160, textvariable=self.popup_h_var, width=8)
        self.popup_w_entry.grid(row=2, column=1, sticky="w", pady=(8, 0))
        self.popup_h_entry.grid(row=2, column=3, sticky="w", pady=(8, 0))

        # Video path（仅视频，不再支持图片）
        ttk.Label(container, text="提示视频").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.video_path_var = tk.StringVar()
        self.video_entry = ttk.Entry(container, textvariable=self.video_path_var, width=48)
        self.video_entry.grid(row=3, column=1, columnspan=2, sticky="we", pady=(8, 0))
        self.video_btn = ttk.Button(container, text="选择视频…", command=self._choose_video)
        self.video_btn.grid(row=3, column=3, sticky="w", pady=(8, 0))

        # Switches
        self.auto_start_var = tk.BooleanVar(value=True)
        self.auto_start_chk = ttk.Checkbutton(container, text="启动即开始倒计时", variable=self.auto_start_var)
        self.auto_start_chk.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.win_autostart_var = tk.BooleanVar(value=False)
        self.win_autostart_chk = ttk.Checkbutton(container, text="Windows 开机自启应用", variable=self.win_autostart_var)
        self.win_autostart_chk.grid(row=4, column=2, columnspan=2, sticky="w", pady=(8, 0))

        self.tray_var = tk.BooleanVar(value=True)
        self.tray_chk = ttk.Checkbutton(container, text="系统托盘常驻", variable=self.tray_var)
        self.tray_chk.grid(row=5, column=0, sticky="w")

        # self.toast_var = tk.BooleanVar(value=True)
        # self.toast_chk = ttk.Checkbutton(container, text="阶段完成气泡提醒", variable=self.toast_var)
        # self.toast_chk.grid(row=5, column=1, sticky="w")

        # self.fullscreen_rest_var = tk.BooleanVar(value=False)
        # self.fullscreen_chk = ttk.Checkbutton(container, text="休息阶段全屏锁定", variable=self.fullscreen_rest_var)
        # self.fullscreen_chk.grid(row=5, column=2, sticky="w") # Changed columnspan from 2 to 1

        # --- NEW LOCK SCREEN CHECKBOX ---
        self.lock_screen_var = tk.BooleanVar(value=False) # 新增锁屏变量
        self.lock_screen_chk = ttk.Checkbutton(container, text="休息阶段锁屏", variable=self.lock_screen_var)
        self.lock_screen_chk.grid(row=5, column=2, sticky="w") # 放在第4列
        # --- END NEW LOCK SCREEN CHECKBOX ---

        # Divider
        ttk.Separator(container, orient=tk.HORIZONTAL).grid(row=6, column=0, columnspan=4, sticky="we", pady=12)

        # Countdown display
        self.phase_var = tk.StringVar(value=f"当前阶段：{self.current_phase}")
        self.time_var = tk.StringVar(value=self._format_seconds(self.remaining_seconds))
        self.phase_label = ttk.Label(container, textvariable=self.phase_var, font=("Microsoft YaHei", 12))
        
        # 数字时钟样式：粗体、深灰色、现代字体
        self.time_label = ttk.Label(
            container, 
            textvariable=self.time_var, 
            font=("Segoe UI", 48, "bold"),
            foreground="#343a40"  # 深灰色，类似图片中的颜色
        )
        self.phase_label.grid(row=7, column=0, columnspan=2, sticky="w")
        self.time_label.grid(row=7, column=2, columnspan=2, sticky="e")

        # Controls
        self.start_btn = ttk.Button(container, text="开始", command=self.start_countdown)
        self.pause_btn = ttk.Button(container, text="暂停", command=self.toggle_pause, state=tk.DISABLED)
        self.reset_btn = ttk.Button(container, text="重置", command=self.reset_countdown)
        self.save_btn = ttk.Button(container, text="保存设置", command=self.save_settings)
        self.start_btn.grid(row=8, column=0, sticky="we", pady=(12, 0))
        self.pause_btn.grid(row=8, column=1, sticky="we", pady=(12, 0))
        self.reset_btn.grid(row=8, column=2, sticky="we", pady=(12, 0))
        self.save_btn.grid(row=8, column=3, sticky="we", pady=(12, 0))

        for i in range(4):
            container.columnconfigure(i, weight=1)

        # Footer
        footer = ttk.Label(container, text="倒计时分为两段：使用时间 → 弹窗/锁屏 + 休息时间 → 循环。", foreground="#666")
        footer.grid(row=9, column=0, columnspan=4, sticky="w", pady=(12, 0))

    def _apply_config_to_ui(self):
        # 从配置读取秒数
        use_sec = max(1, int(self.config_data.get("use_seconds", 1500)))
        rest_sec = max(1, int(self.config_data.get("rest_seconds", 300)))
        
        # 初始化秒数变量（用于保存设置）
        self.use_seconds_var = tk.IntVar(value=use_sec)
        self.rest_seconds_var = tk.IntVar(value=rest_sec)
        
        # 兼容性：保留分钟变量（默认值基于秒数）
        self.use_minutes_var = tk.IntVar(value=use_sec // 60)
        self.rest_minutes_var = tk.IntVar(value=rest_sec // 60)
        
        self.popup_w_var.set(int(self.config_data.get("popup_width", 600)))
        self.popup_h_var.set(int(self.config_data.get("popup_height", 400)))
        self.video_path_var.set(self.config_data.get("video_path", ""))
        self.auto_start_var.set(bool(self.config_data.get("auto_start_countdown", True)))
        self.win_autostart_var.set(bool(self.config_data.get("windows_autostart", False)))
        self.tray_var.set(bool(self.config_data.get("enable_tray", True)))
        # self.toast_var.set(bool(self.config_data.get("enable_toast", True)))
        # self.fullscreen_rest_var.set(bool(self.config_data.get("fullscreen_rest", False)))
        self.lock_screen_var.set(bool(self.config_data.get("enable_lock_screen", False))) # 新增

        self.current_phase = Phase.USE
        self.remaining_seconds = int(self.config_data.get("use_seconds", 1500))
        self._update_labels()

    def _choose_video(self):
        path = filedialog.askopenfilename(title="选择提示视频", filetypes=[
            ("视频文件", ".mp4 .mov .avi .mkv .webm"),
            ("所有文件", "*.*"),
        ])
        if path:
            self.video_path_var.set(path)

    def _create_time_selector(self, parent, prefix, default_minutes):
        """创建可编辑 HH:MM:SS 时间选择器（精确到秒）"""
        # 将分钟转换为总秒数
        default_seconds = default_minutes * 60
        
        # 从秒数计算小时、分钟、秒
        total_seconds = default_seconds
        hours = total_seconds // 3600
        minutes = (total_seconds // 60) % 60
        seconds = total_seconds % 60
        
        # 存储变量
        setattr(self, f"{prefix}_hours_var", tk.IntVar(value=hours))
        setattr(self, f"{prefix}_minutes_var", tk.IntVar(value=minutes))
        setattr(self, f"{prefix}_seconds_var", tk.IntVar(value=seconds))
        
        hours_var = getattr(self, f"{prefix}_hours_var")
        minutes_var = getattr(self, f"{prefix}_minutes_var")
        seconds_var = getattr(self, f"{prefix}_seconds_var")
        
        # 可编辑的 HH:MM:SS 显示与输入
        time_entry_var = tk.StringVar(value="00:00:00")
        time_entry = ttk.Entry(parent, textvariable=time_entry_var, width=10, justify="center", font=("Segoe UI", 12, "bold"))
        time_entry.grid(row=0, column=0, columnspan=5, pady=(0, 8))
        
        # 更新显示的函数
        def update_display():
            h = hours_var.get()
            m = minutes_var.get()
            s = seconds_var.get()
            time_entry_var.set(f"{h:02d}:{m:02d}:{s:02d}")
            # 更新对应的总秒数变量
            total_seconds = h * 3600 + m * 60 + s
            if prefix == "use":
                # Ensure the attribute is set directly, not just a local variable
                self.use_seconds_var.set(total_seconds)
            else:
                self.rest_seconds_var.set(total_seconds)

        def parse_and_apply_entry():
            """从输入框解析 HH:MM:SS 并同步到各变量。"""
            text = time_entry_var.get().strip()
            try:
                parts = text.split(":")
                if len(parts) == 3:
                    ph, pm, ps = parts
                elif len(parts) == 2:
                    # 允许 MM:SS
                    ph, pm, ps = "0", parts[0], parts[1]
                else:
                    raise ValueError
                h = max(0, min(23, int(ph)))
                m = max(0, min(59, int(pm)))
                s = max(0, min(59, int(ps)))
                hours_var.set(h)
                minutes_var.set(m)
                seconds_var.set(s)
                update_display()
            except Exception:
                # 解析失败则回显为当前有效值
                update_display()
        
        # 输入框事件绑定
        time_entry.bind("<FocusOut>", lambda e: parse_and_apply_entry())
        time_entry.bind("<Return>", lambda e: parse_and_apply_entry())
        time_entry.bind("<KP_Enter>", lambda e: parse_and_apply_entry())

        # 初始显示
        update_display()
    
    def _play_video_with_opencv(self, canvas, video_path, popup):
        """使用 OpenCV 播放视频"""
        # import cv2  # Already imported
        from PIL import Image, ImageTk
        
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise Exception("无法打开视频文件")
        
        # 存储变量
        popup._playing = True
        popup._cap = cap
        
        def update_frame():
            if not popup._playing:
                return
            try:
                ret, frame = cap.read()
                if not ret:
                    # 视频结束，重置并循环
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, frame = cap.read()
                    if not ret:
                        return
                
                # 转换为 RGB
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                
                # 调整尺寸以适应画布
                canvas_width = canvas.winfo_width()
                canvas_height = canvas.winfo_height()
                if canvas_width > 1 and canvas_height > 1:
                    img = Image.fromarray(frame_rgb)
                    img.thumbnail((canvas_width, canvas_height), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    
                    # 居中显示
                    canvas.delete("all")
                    x = canvas_width // 2
                    y = canvas_height // 2
                    canvas.create_image(x, y, anchor=tk.CENTER, image=photo)
                    canvas._photo = photo  # 保持引用
            except Exception:
                pass
            
            if popup._playing:
                canvas.after(33, update_frame)  # ~30 FPS
        
        def cleanup():
            popup._playing = False
            try:
                cap.release()
            except Exception:
                pass
        
        popup._video_cleanup.append(cleanup)
        update_frame()

    def _format_seconds(self, sec: int) -> str:
        if sec < 0:
            sec = 0
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _update_labels(self):
        self.phase_var.set(f"当前阶段：{self.current_phase}")
        self.time_var.set(self._format_seconds(self.remaining_seconds))
        if self.state == CountdownState.LOCKED_PAUSED:
            self.phase_var.set(f"当前阶段：{self.current_phase} (已锁屏)")
            self.time_var.set(self._format_seconds(self.remaining_seconds)) # Still show time, but indicate paused

    def save_settings(self):
        # 从秒数变量获取值
        use_seconds = int(getattr(self, "use_seconds_var", tk.IntVar(value=1500)).get())
        rest_seconds = int(getattr(self, "rest_seconds_var", tk.IntVar(value=300)).get())
        
        cfg = {
            "use_seconds": use_seconds,
            "rest_seconds": rest_seconds,
            "popup_width": int(self.popup_w_var.get()),
            "popup_height": int(self.popup_h_var.get()),
            "video_path": self.video_path_var.get(),
            "auto_start_countdown": bool(self.auto_start_var.get()),
            "windows_autostart": bool(self.win_autostart_var.get()),
            "enable_tray": bool(self.tray_var.get()),
            # "enable_toast": bool(self.toast_var.get()),
            # "fullscreen_rest": bool(self.fullscreen_rest_var.get()),
            "enable_lock_screen": bool(self.lock_screen_var.get()), # 新增
        }
        write_config(cfg)
        self.config_data = cfg
        ok, err = (True, None)
        if os.name == "nt":
            ok, err = set_windows_autostart(cfg.get("windows_autostart", False))
        if not ok:
            messagebox.showwarning("开机自启失败", f"无法设置开机自启：{err}")
        messagebox.showinfo("已保存", "设置已保存。")
        # Reset current phase with new durations
        self.reset_countdown()
        # Restart tray according to setting
        self._restart_tray_if_needed()

    def start_countdown(self):
        if self.state == CountdownState.RUNNING:
            return
        # Ensure remaining time is set for current phase
        if self.current_phase == Phase.USE:
            self.remaining_seconds = int(self.config_data.get("use_seconds", 1500)) if self.remaining_seconds <= 0 else self.remaining_seconds
        else: # Phase.REST
            self.remaining_seconds = int(self.config_data.get("rest_seconds", 300)) if self.remaining_seconds <= 0 else self.remaining_seconds
            
        self.state = CountdownState.RUNNING
        self.start_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.NORMAL, text="暂停")
        self._tick()

    def toggle_pause(self):
        if self.state == CountdownState.RUNNING:
            self.state = CountdownState.PAUSED
            self.pause_btn.config(text="继续")
        elif self.state == CountdownState.PAUSED:
            self.state = CountdownState.RUNNING
            self.pause_btn.config(text="暂停")
            self._tick()

    def reset_countdown(self):
        self.state = CountdownState.IDLE
        self.current_phase = Phase.USE
        self.remaining_seconds = int(self.config_data.get("use_seconds", 1500))
        self._update_labels()
        self.start_btn.config(state=tk.NORMAL)
        self.pause_btn.config(state=tk.DISABLED, text="暂停")

    def _tick(self):
        if self.state != CountdownState.RUNNING:
            return
        self._update_labels()
        if self.remaining_seconds <= 0:
            # Phase complete → popup
            self.state = CountdownState.IDLE # Temporarily set to IDLE
            self.start_btn.config(state=tk.NORMAL)
            self.pause_btn.config(state=tk.DISABLED, text="暂停")
            self._notify_phase_complete()
            self._show_media_popup_and_continue()
            return
        self.remaining_seconds -= 1
        self.after(1000, self._tick)

    # --- NEW LOCK COMPUTER METHOD ---
    def _lock_computer(self):
        """
        Attempts to lock the computer based on the operating system.
        """
        if sys.platform.startswith("win"):
            if WINDOWS_LOCK_AVAILABLE:
                try:
                    ctypes.windll.user32.LockWorkStation()
                    print("Windows: Computer locked.")
                except Exception as e:
                    print(f"Error locking Windows: {e}")
                    messagebox.showerror("锁屏失败", f"无法锁定Windows电脑: {e}")
            else:
                messagebox.showwarning("锁屏功能不可用", "ctypes库在Windows上加载失败，无法使用锁屏功能。")
        elif sys.platform == "darwin": # macOS
            try:
                # This command triggers the screensaver and requires a password to unlock.
                # It's the closest to "locking" the screen programmatically on macOS.
                # The 'keystroke' command simulates Ctrl+Cmd+Q, which locks the screen on modern macOS.
                subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "q" using {control down, command down}'])
                print("macOS: Screen locked (screensaver activated).")
            except Exception as e:
                print(f"Error locking macOS: {e}")
                messagebox.showerror("锁屏失败", f"无法锁定macOS电脑: {e}")
        else:
            # For Linux, you would typically use a command like:
            # 'gnome-screensaver-command --lock' (GNOME)
            # 'qdbus org.kde.KScreenLocker /MainApplication quit' (KDE)
            # You would need to detect the desktop environment.
            messagebox.showwarning("锁屏功能未实现", "当前操作系统不支持自动锁屏，或未实现该功能。")
    # --- END NEW LOCK COMPUTER METHOD ---


    def _show_media_popup_and_continue(self):
        width = int(self.config_data.get("popup_width", 600))
        height = int(self.config_data.get("popup_height", 400))
        video_path = self.config_data.get("video_path", "")

        # Determine the next phase *before* the popup is shown
        next_phase = Phase.REST if self.current_phase == Phase.USE else Phase.USE
        
        # Check if lock screen is enabled for the *current* phase transition (i.e., when USE ends and REST begins)
        enable_lock_for_rest = bool(self.config_data.get("enable_lock_screen", False)) and self.current_phase == Phase.USE

        # If next is REST and fullscreen enabled → full screen modal (not used with lock screen for simplicity)
        fullscreen = bool(self.config_data.get("fullscreen_rest", False)) and next_phase == Phase.REST and not enable_lock_for_rest
        
        # --- MODIFIED: Removed the early return for enable_lock ---
        # The popup will now always display if a phase ends, regardless of lock screen setting.
        # The actual locking will happen when the user clicks "我知道了，开始下一段".
        # --- END MODIFIED ---

        # 在创建窗口前计算与视频一致的纵横比
        calc_w, calc_h = width, height
        try:
            if video_path and os.path.exists(video_path):
                vw = vh = None
                if OPENCV_AVAILABLE:
                    try:
                        cap_probe = cv2.VideoCapture(video_path)
                        if cap_probe.isOpened():
                            vw = int(cap_probe.get(cv2.CAP_PROP_FRAME_WIDTH))
                            vh = int(cap_probe.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        cap_probe.release()
                    except Exception:
                        pass
                # 若拿到分辨率，则等比缩放到配置尺寸之内
                if vw and vh and vw > 0 and vh > 0:
                    scale = min(width / vw, height / vh)
                    scale = scale if scale > 0 else 1.0
                    calc_w = max(200, int(vw * scale))
                    calc_h = max(200, int(vh * scale))
        except Exception:
            pass

        popup = tk.Toplevel(self)
        popup.title("时间到！")
        if fullscreen:
            popup.attributes("-fullscreen", True)
            popup.overrideredirect(True)
            popup.resizable(True, True)
        else:
            popup.geometry(f"{calc_w}x{calc_h}")
            popup.resizable(True, True)
        try:
            if fullscreen:
                popup.grab_set_global()  # stronger modal
            else:
                popup.grab_set()
        except Exception:
            popup.grab_set()
        popup.attributes("-topmost", True)

        frame = ttk.Frame(popup, padding=8)
        frame.pack(fill=tk.BOTH, expand=True)

        # 锁定：屏蔽关闭与快捷键，仅按钮可退出
        def ignore_close():
            if fullscreen:
                return  # 忽略关闭
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", ignore_close)
        popup.bind("<Escape>", lambda e: "break")
        popup.bind("<Alt-F4>", lambda e: "break")
        popup.bind("<F11>", lambda e: "break")
        # 禁用主窗口交互
        try:
            self.attributes("-disabled", True)
        except Exception:
            pass

        # 仅视频
        popup._video_cleanup = []  # store cleanup callbacks

        played = False
        video_path_check = video_path and os.path.exists(video_path)
        
        if video_path_check:
            # Try OpenCV backend first (most compatible)
            if OPENCV_AVAILABLE and not played:
                try:
                    canvas = tk.Canvas(frame, bg="#000")
                    canvas.pack(fill=tk.BOTH, expand=True)
                    self._play_video_with_opencv(canvas, video_path, popup)
                    played = True
                except Exception as e:
                    try:
                        canvas.destroy()
                    except Exception:
                        pass
            
            # Try tkintervideo as fallback
            if TKINTERVIDEO_AVAILABLE and not played:
                try:
                    tk_player = TkinterVideo(master=frame, scaled=True, keep_aspect=True, bg="#000")
                    tk_player.pack(fill=tk.BOTH, expand=True)
                    tk_player.load(video_path)
                    tk_player.play()
                    played = True
                    def _cleanup_tk():
                        try:
                            tk_player.stop()
                        except Exception:
                            pass
                    popup._video_cleanup.append(_cleanup_tk)
                except Exception:
                    try:
                        tk_player.destroy()
                    except Exception:
                        pass

            # Fallback to VLC backend
            if VLC_AVAILABLE and not played:
                try:
                    prepare_vlc_on_windows()
                except Exception:
                    pass
                container = tk.Frame(frame, bg="#000")
                container.pack(fill=tk.BOTH, expand=True)
                container.update_idletasks()
                try:
                    instance = vlc.Instance()
                    media = instance.media_new(video_path)
                    player = instance.media_player_new()
                    player.set_media(media)
                    hwnd = container.winfo_id()
                    if sys.platform.startswith("win"):
                        player.set_hwnd(hwnd)
                    elif sys.platform == "darwin":
                        player.set_nsobject(hwnd)
                    else:
                        player.set_xwindow(hwnd)
                    player.play()
                    played = True
                    def _cleanup_vlc():
                        try:
                            player.stop()
                            player.release()
                            instance.release()
                        except Exception:
                            pass
                    popup._video_cleanup.append(_cleanup_vlc)
                except Exception as e:
                    container.destroy()
                    err = ttk.Label(frame, text=f"VLC 播放失败: {e}")
                    err.pack(expand=True)

        if not played:
            # 分两个条件检查
            has_video = video_path_check
            has_deps = TKINTERVIDEO_AVAILABLE or VLC_AVAILABLE or OPENCV_AVAILABLE
            
            if not has_video:
                msg_text = "⚠️ 未设置视频\n请在应用设置中选择视频文件"
            elif not has_deps:
                msg_text = "⚠️ 缺少播放依赖\n请安装依赖：pip install opencv-python\n或安装 VLC 播放器"
            else:
                msg_text = "⚠️ 视频播放失败\n请检查视频文件格式"
            
            msg = ttk.Label(frame, text=msg_text, font=("Microsoft YaHei", 12))
            msg.pack(expand=True)

        btn = ttk.Button(frame, text="我知道了，开始下一段", command=lambda: self._close_popup_and_start_next(popup))
        btn.pack(pady=8)

    # --- UPDATED _close_popup_and_start_next METHOD ---
    def _close_popup_and_start_next(self, popup: tk.Toplevel):
        # Determine the next phase *after* the current phase (which just ended)
        next_phase = Phase.REST if self.current_phase == Phase.USE else Phase.USE
        enable_lock_for_rest = bool(self.config_data.get("enable_lock_screen", False)) and self.current_phase == Phase.USE
        
        # Normal popup closing actions
        try:
            popup.grab_release()
        except Exception:
            pass
        try:
            self.attributes("-disabled", False)
        except Exception:
            pass
        try:
            for cb in getattr(popup, "_video_cleanup", []):
                cb()
        except Exception:
            pass
        popup.destroy()

        # Update phase and remaining seconds for the *next* phase
        self.current_phase = next_phase
        if self.current_phase == Phase.REST:
            self.remaining_seconds = int(self.config_data.get("rest_seconds", 300))
        else: # Phase.USE
            self.remaining_seconds = int(self.config_data.get("use_seconds", 1500))
        self._update_labels()

        # If lock screen is enabled and it's time for the rest phase
        if enable_lock_for_rest:
            self._lock_computer()
            # After locking, *automatically start* the rest countdown in the background
            print(f"Computer locked for {self.current_phase}. Starting countdown in background.")
            self.state = CountdownState.RUNNING # Change state to running for background countdown
            self.start_btn.config(state=tk.DISABLED)
            self.pause_btn.config(state=tk.DISABLED, text="暂停 (已锁屏)") # Indicate visually that it's "paused" but actually counting down
            self._tick() # Start the tick for the rest phase while locked
        else:
            # If no lock screen, just proceed to start the next phase's countdown normally
            self.start_countdown()
    # --- END UPDATED _close_popup_and_start_next METHOD ---


    # Helpers

    def _notify_phase_complete(self):
        if self.config_data.get("enable_toast", True) and self._toaster is not None:
            try:
                title = "倒计时结束"
                next_phase_name = "休息时间" if self.current_phase == Phase.USE else "使用时间"
                self._toaster.show_toast(title, f"即将进入：{next_phase_name}", duration=3, threaded=True)
            except Exception:
                pass

    # System tray
    def _start_tray(self):
        if not (self.config_data.get("enable_tray", True) and TRAY_AVAILABLE and PIL_AVAILABLE):
            return
        if self._tray_icon is not None:
            return

        # Simple in-memory icon
        img = PILImage.new("RGBA", (64, 64), (30, 144, 255, 255))
        # draw a simple dot/letter
        try:
            from PIL import ImageDraw
            d = ImageDraw.Draw(img)
            d.ellipse((12, 12, 52, 52), fill=(255, 255, 255, 255))
        except Exception:
            pass

        def on_show(icon, item):
            self.after(0, self.deiconify)
            self.after(0, self.lift)

        def on_start(icon, item):
            self.after(0, self.start_countdown)

        def on_pause(icon, item):
            self.after(0, self.toggle_pause)

        def on_reset(icon, item):
            self.after(0, self.reset_countdown)

        def on_quit(icon, item):
            self.after(0, self._quit_app)

        menu = (
            pystray.MenuItem("显示窗口", on_show),
            pystray.MenuItem("开始", on_start),
            pystray.MenuItem("暂停/继续", on_pause),
            pystray.MenuItem("重置", on_reset),
            pystray.MenuItem("退出", on_quit),
        )

        icon = pystray.Icon("Countdown", img, "倒计时", pystray.Menu(*menu))
        self._tray_icon = icon

        threading.Thread(target=icon.run, daemon=True).start()

    def _stop_tray(self):
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None

    def _restart_tray_if_needed(self):
        if self.config_data.get("enable_tray", True):
            if self._tray_icon is None:
                self._start_tray()
        else:
            self._stop_tray()

    def _on_window_close(self):
        if self.config_data.get("enable_tray", True) and TRAY_AVAILABLE:
            self.withdraw()
            if self.config_data.get("enable_toast", True) and self._toaster is not None:
                try:
                    self._toaster.show_toast("最小化到托盘", "应用在系统托盘运行", duration=3, threaded=True)
                except Exception:
                    pass
        else:
            self._quit_app()

    def _quit_app(self):
        self._stop_tray()
        if self.hwnd and WINDOWS_SCREEN_STATUS_AVAILABLE:
            try:
                win32gui.UnregisterSessionNotification(self.hwnd)
                win32gui.DestroyWindow(self.hwnd)
                print("Windows session notifications unregistered.")
            except Exception as e:
                print(f"Error unregistering Windows session notifications: {e}")
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()