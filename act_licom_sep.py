# file: lickometer_ch1_gui.py
# Requerimientoss: pyserial, tkinter, numpy, matplotlib, Python 3.8+

import os
import glob
import csv
import time
import threading
import queue
from queue import Full
from datetime import datetime

# Salud del sistema: import opcional de psutil (dinámico para evitar que se te saque del programa)
try:
    import importlib
    psutil = importlib.import_module("psutil")  # opcional, para CPU/memoria
except Exception:
    psutil = None
import subprocess

import serial
from serial import SerialException
from serial.tools import list_ports

import numpy as np
import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt

import pandas as pd
from typing import List, Tuple

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except Exception as e:
    raise RuntimeError("Tkinter no disponible.")

HEADER = b"\xAA\xBB"
TAIL = b"\xCC\xDD"

# --- Umbrales de microestructura (ms)
BURST_MAX = 250.0    # ILI <= 250 => dentro de ráfaga
IBI_MIN   = 250.0    # ILI > 250 => IBI
ICI_MIN   = 1000.0   # ILI > 1000 => ICI (y también cuenta como IBI)

def list_serial_candidates() -> list:
    ports = list_ports.comports()
    cands = []
    for p in ports:
        dev = p.device
        desc = p.description or ""
        hwid = p.hwid or ""
        score = 0
        text = f"{desc} {hwid}".lower()
        for key in ("ch340", "cp210", "usb-serial", "arduino", "silabs", "ftdi", "wch"):
            if key in text:
                score += 10
        if dev.startswith("/dev/cu."):
            score += 3
        if "usb" in dev.lower():
            score += 2
        cands.append((score, dev, desc))
    cands.sort(key=lambda x: (-x[0], x[1]))
    if not cands:
        patterns = [
            "/dev/cu.usbserial*",
            "/dev/cu.usbmodem*",
            "/dev/tty.usbserial*",
            "/dev/tty.usbmodem*",
        ]
        seen = set()
        for pat in patterns:
            for dev in glob.glob(pat):
                if dev in seen:
                    continue
                seen.add(dev)
                cands.append((0, dev, "USB (glob)"))
    return [(dev, desc) for _, dev, desc in cands]

class PortSelectDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Seleccionar puerto serial")
        self.resizable(False, False)
        self.result = None
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Selecciona el puerto del lickómetro:").pack(anchor="w")

        self.ports_var = tk.StringVar(value=[])
        self.listbox = tk.Listbox(frm, listvariable=self.ports_var, width=52, height=8)
        self.listbox.pack(fill="both", expand=True, pady=(6, 6))

        btns = ttk.Frame(frm)
        btns.pack(fill="x")
        self.refresh_btn = ttk.Button(btns, text="Actualizar", command=self._refresh)
        self.ok_btn = ttk.Button(btns, text="Aceptar", command=self._on_ok)
        self.cancel_btn = ttk.Button(btns, text="Cancelar", command=self._on_cancel)
        self.refresh_btn.pack(side=tk.LEFT)
        self.ok_btn.pack(side=tk.RIGHT)
        self.cancel_btn.pack(side=tk.RIGHT, padx=(6, 6))

        self._refresh()
        self.after(100, self._autoselect_first)

        self.transient(master)
        self.grab_set()
        self.focus_force()

    def _autoselect_first(self):
        if self.listbox.size() > 0:
            self.listbox.selection_set(0)

    def _refresh(self):
        cands = list_serial_candidates()
        if not cands:
            self.ports_var.set(["(No se detectan puertos USB-serial)"])
            return
        items = [f"{dev} — {desc}" if desc else dev for dev, desc in cands]
        self.ports = [dev for dev, _ in cands]
        self.ports_var.set(items)

    def _on_ok(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        try:
            dev = self.ports[idx]
        except Exception:
            return
        self.result = dev
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()

class SingleLickometerGUI:
    """Lickómetro: solo CH1. Adquisición + CSV + diagnóstico (sin análisis)."""
    DEBUG = False

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Lickómetro (solo CH1)")

        # Config generales
        self.port = ""
        self.baudrate = 115200

        # No filtrar ILIs para un CSV fiel
        self.min_ili_ms = 0.0

        # Sesión
        self.duration_sec = 0.0
        self.start_time = None
        self.end_time = 0.0
        self.running = False
        self.save_dir = None

        # Estado CH1
        self.last_lick_time = None
        self.lick_times_ms = []
        self.diff_times_ms = []
        self.last_state = 0

        # Metadatos CH1
        self.rat_id_1 = tk.StringVar(value="")
        self.rat_w_1 = tk.StringVar(value="")
        self.bot_w_1 = tk.StringVar(value="")
        self.minutes_var = tk.StringVar(value="1")

        self.status_var = tk.StringVar(value="Listo")
        self.timer_var = tk.StringVar(value="00:00.0")
        self._timer_job = None

        # Serial
        self._need_reconnect = False
        self._ser = None
        self._reader_thread = None

        # Diagnóstico
        self.diag = {
            "start_iso": None,
            "last_error": "",
            "serial_reconnects": 0,
            "serial_exceptions": 0,
            "parse_errors": 0,
            "frames": 0,
            "queue_capacity": 100000,
            "queue_dropped": 0,
            "stalls": 0,
            "stall_threshold_s": 2.0,
            "last_frame_ts": 0.0,
            "last_lick_ts": 0.0,
            "licks_ch1": 0,
        }

        # Salud del sistema (health meter)
        self.health = {
            "score": 100.0,
            "cpu_pct": 0.0,
            "gpu_active_pct": None,  # sólo macOS con powermetrics (si disponible)
            "queue_ratio": 0.0,
            "lag_s": 0.0,
            "dropped_total": 0,
            "dropped_rate": 0.0,   # por minuto (estimado)
            "last_drop_count": 0,
            "last_health_t": time.time(),
        }

        # Cola eventos
        self.ev_q = queue.Queue(maxsize=100000)
        self.ev_dropped = 0

        # UI buffers
        self.ui_last_idx = 0
        self.ui_update_scheduled = False
        self.ui_max_rows = 500

        # Datos clasificados en vivo
        self.events = []            # dicts: {idx, t_rel_ms, ili_ms, ili_class}
        self.ili_ms_list = []       # lista de ILIs en ms (>=0)
        self.ili_class_list = []    # "Burst" | "IBI" | "ICI" | "Reset/Invalid"

        # Contadores en vivo (Bursts cuenta ráfagas COMPLETADAS)
        self.live_counts = {
            "Bursts": 0,   # se incrementa al cerrar una ráfaga (no por cada ILI)
            "IBI": 0,
            "ICI": 0,
        }
        # Estado interno para detectar límites de ráfagas
        self._in_burst = False
        self._burst_ili_count = 0  # # de ILIs consecutivos <= BURST_MAX en la ráfaga actual
        # Señal para contar IBI/ICI solo si siguen inmediatamente a una ráfaga completada
        self._just_closed_burst = False

        # Countdown state for delayed start
        self._countdown_job = None
        self._countdown_remaining = 0
        self._pending_start = False

        self._build_widgets()
        self.root.after(50, self._drain_queue)
        self.root.after(500, self._watchdog_tick)
        # Tick de salud del sistema cada 2 s
        self.root.after(2000, self._health_tick)
        # Fin de sesión
        self._session_finished = False

    def _build_widgets(self):
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)

        top = ttk.Frame(frm)
        top.pack(fill="x", pady=(0, 8))

        ttk.Label(top, text="Duración (min):").pack(side=tk.LEFT)
        ttk.Entry(top, width=6, textvariable=self.minutes_var).pack(side=tk.LEFT, padx=(4, 12))

        # Día experimental (D1, D2, D3, S1, S2, etc.)
        self.day_var = tk.StringVar(value="D1")
        ttk.Label(top, text="Día:").pack(side=tk.LEFT)
        ttk.Entry(top, width=4, textvariable=self.day_var).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top, text="CH1  ID:").pack(side=tk.LEFT)
        ttk.Entry(top, width=10, textvariable=self.rat_id_1).pack(side=tk.LEFT, padx=4)

        ttk.Label(top, text="Peso rata (g):").pack(side=tk.LEFT)
        ttk.Entry(top, width=7, textvariable=self.rat_w_1).pack(side=tk.LEFT, padx=4)

        ttk.Label(top, text="Peso bebedero (g):").pack(side=tk.LEFT)
        ttk.Entry(top, width=7, textvariable=self.bot_w_1).pack(side=tk.LEFT, padx=(4, 12))

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(0, 6))
        self.start_btn = ttk.Button(btns, text="Iniciar", command=self.start_session)
        self.stop_btn = ttk.Button(btns, text="Detener", state=tk.DISABLED, command=self.stop_session)
        self.choose_btn = ttk.Button(btns, text="Elegir puerto…", command=self._choose_port)
        self.refresh_btn = ttk.Button(btns, text="Actualizar puertos", command=self._refresh_port_hint)
        self.reopen_btn = ttk.Button(btns, text="Reiniciar puerto", command=self._force_port_restart)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn.pack(side=tk.LEFT, padx=6)
        self.choose_btn.pack(side=tk.LEFT, padx=6)
        self.refresh_btn.pack(side=tk.LEFT, padx=6)
        self.reopen_btn.pack(side=tk.LEFT, padx=6)

        timer_row = ttk.Frame(frm)
        timer_row.pack(fill="x", pady=(2, 8))
        ttk.Label(timer_row, text="Tiempo transcurrido:").pack(side=tk.LEFT)
        ttk.Label(timer_row, textvariable=self.timer_var, font=("TkDefaultFont", 12, "bold")).pack(side=tk.LEFT, padx=(6, 0))

        # Tabla principal de eventos (en lugar de raster)
        table = ttk.Frame(frm)
        table.pack(fill="both", expand=True)
        ttk.Label(table, text="Eventos CH1 (clasificados)").pack(anchor="w")

        cols = ("idx", "t_rel", "delta", "class")
        self.tree1 = ttk.Treeview(table, columns=cols, show="headings", height=20)
        self.tree1.heading("idx", text="#")
        self.tree1.heading("t_rel", text="t_rel (ms)")
        self.tree1.heading("delta", text="ILI (ms)")
        self.tree1.heading("class", text="Tipo")
        self.tree1.column("idx", width=60, anchor=tk.CENTER)
        self.tree1.column("t_rel", width=120, anchor=tk.E)
        self.tree1.column("delta", width=120, anchor=tk.E)
        self.tree1.column("class", width=120, anchor=tk.CENTER)
        self.tree1.pack(side=tk.LEFT, fill="both", expand=True)
        sb1 = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree1.yview)
        self.tree1.configure(yscroll=sb1.set)
        sb1.pack(side=tk.RIGHT, fill=tk.Y)

        # Resumen en vivo
        summary_frame = ttk.Frame(frm)
        summary_frame.pack(fill="x", pady=(6, 0))
        self.live_summary_var = tk.StringVar(value="Bursts: 0 | IBIs: 0 | ICIs: 0")
        ttk.Label(summary_frame, textvariable=self.live_summary_var).pack(anchor="w")

        ttk.Label(self.root, textvariable=self.status_var).pack(anchor="w", padx=10, pady=(4, 8))
        self._refresh_port_hint()

    def _choose_port(self):
        dlg = PortSelectDialog(self.root)
        self.root.wait_window(dlg)
        if dlg.result:
            self.port = dlg.result
            self.status_var.set(f"Puerto seleccionado: {self.port}")

    def _refresh_port_hint(self):
        cands = list_serial_candidates()
        if cands:
            hint = ", ".join(dev for dev, _ in cands[:3])
            self.status_var.set(f"Sugeridos: {hint}  —  Puerto actual: {self.port or '(no seleccionado)'}")
        else:
            self.status_var.set("No se detectan puertos USB-serial. Conecta el dispositivo y pulsa 'Actualizar puertos'.")

    def start_session(self):
        if self.running or self._pending_start:
            return
        try:
            minutes = float(self.minutes_var.get())
            if minutes <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Duración inválida", "Ingresa minutos > 0")
            return

        for var in (self.rat_w_1, self.bot_w_1):
            s = var.get().strip()
            if s:
                try:
                    float(s)
                except ValueError:
                    messagebox.showerror("Valor incorrecto", "Los pesos deben ser numéricos (g).")
                    return

        folder = filedialog.askdirectory(title="Selecciona carpeta para guardar CSV")
        if not folder:
            return
        self.save_dir = folder

        if not self._ensure_port_interactive():
            return

        self.duration_sec = minutes * 60.0
        self.timer_var.set("00:05")

        self._pending_start = True
        self._countdown_remaining = 5
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.DISABLED)
        self.status_var.set("Coloca la rata en el rig… iniciando en 5 s")
        self._run_start_countdown()

    def _run_start_countdown(self):
        if not self._pending_start:
            return
        if self._countdown_remaining <= 0:
            self.timer_var.set("00:00.0")
            self._begin_acquisition()
            return

        self.timer_var.set(f"00:0{self._countdown_remaining}")
        self.status_var.set(f"Coloca la rata en el rig… iniciando en {self._countdown_remaining} s")
        self._countdown_remaining -= 1
        self._countdown_job = self.root.after(1000, self._run_start_countdown)

    def _begin_acquisition(self):
        self._pending_start = False
        if self._countdown_job is not None:
            try:
                self.root.after_cancel(self._countdown_job)
            except Exception:
                pass
            self._countdown_job = None

        self.start_time = time.time()
        self.end_time = self.start_time + self.duration_sec

        self.timer_var.set("00:00.0")
        if self._timer_job is not None:
            try:
                self.root.after_cancel(self._timer_job)
            except Exception:
                pass
            self._timer_job = None
        self._timer_job = self.root.after(0, self._update_timer)

        self.diag.update(
            {
                "start_iso": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "last_error": "",
                "serial_reconnects": 0,
                "serial_exceptions": 0,
                "parse_errors": 0,
                "frames": 0,
                "queue_dropped": 0,
                "stalls": 0,
                "last_frame_ts": time.time(),
                "last_lick_ts": time.time(),
                "licks_ch1": 0,
            }
        )

        self.last_lick_time = None
        self.lick_times_ms.clear()
        self.diff_times_ms.clear()
        self.events.clear()
        self.ili_ms_list.clear()
        self.ili_class_list.clear()
        self.live_counts = {"Bursts": 0, "IBI": 0, "ICI": 0}
        self._in_burst = False
        self._burst_ili_count = 0
        self._just_closed_burst = False
        if hasattr(self, 'live_summary_var'):
            self.live_summary_var.set("Bursts: 0 | IBIs: 0 | ICIs: 0")
        for item in self.tree1.get_children(""):
            self.tree1.delete(item)
        self.ui_last_idx = 0
        self.ui_update_scheduled = False
        self.last_state = 0

        try:
            while True:
                self.ev_q.get_nowait()
        except Exception:
            pass

        self._session_finished = False

        self.running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.status_var.set(f"Conectando a {self.port}…")

        self._reader_thread = threading.Thread(target=self._read_serial_loop, daemon=True)
        self._reader_thread.start()

    def stop_session(self):
        self.running = False
        self._pending_start = False
        if self._countdown_job is not None:
            try:
                self.root.after_cancel(self._countdown_job)
            except Exception:
                pass
            self._countdown_job = None
        if self._timer_job is not None:
            try:
                self.root.after_cancel(self._timer_job)
            except Exception:
                pass
            self._timer_job = None
        try:
            if self._reader_thread and self._reader_thread.is_alive():
                self._reader_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass
        self._ser = None
        # Asegurar finalización (exportar CSV/XLSX y mostrar promedios) si no se ha hecho aún
        try:
            if not self._session_finished:
                self._finish_session()
        except Exception:
            pass

    def _update_timer(self):
        try:
            if self.start_time is None:
                self.timer_var.set("00:00.0")
            else:
                elapsed = max(0.0, time.time() - self.start_time)
                minutes = int(elapsed // 60)
                seconds = elapsed % 60
                self.timer_var.set(f"{minutes:02d}:{seconds:04.1f}")
        except Exception:
            pass
        finally:
            if self.running:
                self._timer_job = self.root.after(100, self._update_timer)
            else:
                self._timer_job = None

    def _ensure_port_interactive(self) -> bool:
        def exists(dev: str) -> bool:
            try:
                return os.path.exists(dev)
            except Exception:
                return False

        if self.port and exists(self.port):
            return True

        cands = list_serial_candidates()
        if cands:
            self.port = cands[0][0]
            if exists(self.port):
                self.status_var.set(f"Usando puerto detectado: {self.port}")
                return True

        dlg = PortSelectDialog(self.root)
        self.root.wait_window(dlg)
        if dlg.result and exists(dlg.result):
            self.port = dlg.result
            self.status_var.set(f"Usando puerto: {self.port}")
            return True

        messagebox.showerror("Puerto no encontrado", "No se pudo seleccionar un puerto válido.")
        return False

    def _force_port_restart(self):
        if self._ser:
            try:
                self._ser.dtr = True
                time.sleep(0.05)
                self._ser.dtr = False
                self.status_var.set("Pulso DTR enviado (reset suave)")
            except Exception:
                pass
        self._need_reconnect = True

    def _open_serial(self):
        ser = serial.Serial(self.port, self.baudrate, timeout=0.05, dsrdtr=False, rtscts=False)
        try:
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        self._ser = ser
        return ser

    def _reopen_serial(self, old_ser, backoff=0.5):
        try:
            if old_ser:
                try:
                    old_ser.close()
                except Exception:
                    pass
        finally:
            pass
        delay = backoff
        while self.running and time.time() < self.end_time:
            if not os.path.exists(self.port or ""):
                self.root.after(0, self._choose_port)
                for _ in range(20):
                    if self.port and os.path.exists(self.port):
                        break
                    time.sleep(0.1)
            try:
                s = self._open_serial()
                self.diag["serial_reconnects"] += 1
                self.root.after(0, lambda: self.status_var.set(f"Reconectado a {self.port}"))
                return s
            except Exception as e:
                self.diag["last_error"] = f"Reintento conexión: {e}"
                self.root.after(0, lambda e=e: self.status_var.set(f"Reintentando: {e}"))
                time.sleep(delay)
                delay = min(delay * 1.5, 5.0)
        return None

    def _read_serial_loop(self):
        try:
            ser = self._open_serial()
            self.diag["last_error"] = ""
        except Exception as e:
            self.root.after(0, lambda e=e: messagebox.showerror(
                "Error de conexión",
                f"No se pudo abrir {self.port or '(sin puerto)'}:\n{e}\n\n"
                "Sugerencias:\n• Verifica el cable/energía\n• Prueba otro puerto USB\n• Pulsa 'Elegir puerto…'"
            ))
            self.root.after(0, lambda: self.start_btn.config(state=tk.NORMAL))
            self.running = False
            return

        self.root.after(0, lambda: self.status_var.set("conectado"))
        buf = b""

        while self.running and time.time() < self.end_time:
            try:
                n = ser.in_waiting
                if n:
                    chunk = ser.read(min(n, 2048))
                    if not chunk:
                        raise SerialException("device returned no data")
                    buf += chunk
                    self.diag["last_frame_ts"] = time.time()
                    while True:
                        frame, buf = self._find_frame(buf)
                        if frame is None:
                            break
                        canal_estado = self._parse_frame(frame)
                        if canal_estado:
                            self.diag["frames"] += 1
                            ch, st = canal_estado
                            if ch == 1:
                                prev = self.last_state
                                if st != prev:
                                    self.last_state = st
                                    if st == 1:
                                        self._register_lick()
                else:
                    time.sleep(0.003)
            except (SerialException, OSError) as e:
                self.diag["serial_exceptions"] += 1
                self.diag["last_error"] = f"SerialException/OSError: {e}"
                self.root.after(0, lambda e=e: self.status_var.set(f"Serial caído: {e} (sin reconexión automática)"))
                break
            except Exception as e:
                self.diag["parse_errors"] += 1
                self.diag["last_error"] = f"Excepción lectura/parsing: {e}"
                self.root.after(0, lambda e=e: self.status_var.set(f"Error lectura: {e} (sin reconexión automática)"))
                break

        try:
            ser.close()
        except Exception:
            pass
        self._ser = None
        self.running = False
        self.root.after(0, self._finish_session)

    def _find_frame(self, bbuf: bytes):
        h = bbuf.find(HEADER)
        if h < 0:
            return None, bbuf[-1:]
        t = bbuf.find(TAIL, h + 2)
        if t < 0:
            return None, bbuf[h:]
        return bbuf[h : t + 2], bbuf[t + 2 :]

    def _parse_frame(self, frame: bytes):
        if not (frame.startswith(HEADER) and frame.endswith(TAIL)):
            return None
        body = frame[2:-2]
        if len(body) < 4:
            return None
        canal = body[1]
        estado = body[2]
        return int(canal), int(estado)

    def _register_lick(self):
        try:
            self.ev_q.put_nowait(time.time())
        except Full:
            self.ev_dropped += 1
            self.diag["queue_dropped"] += 1
        except Exception as e:
            self.diag["last_error"] = f"Queue put error: {e}"

    def _drain_queue(self):
        try:
            processed = 0
            max_per_tick = 2000
            updated = False
            while processed < max_per_tick:
                try:
                    ts = self.ev_q.get_nowait()
                except Exception:
                    break
                processed += 1
                last = self.last_lick_time
                if last is not None and (ts - last) * 1000.0 < self.min_ili_ms:
                    continue
                if self.start_time is None:
                    continue
                t_rel_ms = (ts - self.start_time) * 1000.0
                delta_ms = 0.0 if last is None else (ts - last) * 1000.0

                self.last_lick_time = ts
                self.lick_times_ms.append(t_rel_ms)
                self.diff_times_ms.append(delta_ms)

                # Clasificación del ILI (delta_ms se asocia al lick actual)
                ili = float(delta_ms)
                if ili <= 0:
                    if self._in_burst and self._burst_ili_count >= 2:
                        self.live_counts["Bursts"] += 1
                    self._in_burst = False
                    self._burst_ili_count = 0
                    self._just_closed_burst = False
                    ili_class = "Reset/Invalid"
                elif ili <= BURST_MAX:
                    self._in_burst = True
                    self._burst_ili_count += 1
                    self._just_closed_burst = False
                    ili_class = "Burst"
                else:
                    closed = False
                    if self._in_burst and self._burst_ili_count >= 2:
                        self.live_counts["Bursts"] += 1
                        closed = True

                    self._in_burst = False
                    self._burst_ili_count = 0

                    if ili > ICI_MIN:
                        ili_class = "ICI"
                    else:
                        ili_class = "IBI"

                    if closed:
                        self.live_counts["IBI"] += 1
                        if ili > ICI_MIN:
                            self.live_counts["ICI"] += 1

                    self._just_closed_burst = False

                self.ili_ms_list.append(max(0.0, ili))
                self.ili_class_list.append(ili_class)

                ev = {
                    "idx": len(self.lick_times_ms),
                    "t_rel_ms": t_rel_ms,
                    "ili_ms": ili,
                    "class": ili_class,
                }
                self.events.append(ev)

                # Actualiza resumen en vivo (sin bloquear)
                try:
                    self.live_summary_var.set(
                        f"Bursts: {self.live_counts['Bursts']} | IBIs: {self.live_counts['IBI']} | "
                        f"ICIs: {self.live_counts['ICI']}"
                    )
                except Exception:
                    pass

                self.diag["licks_ch1"] += 1
                self.diag["last_lick_ts"] = ts
                updated = True
            if updated:
                self._schedule_ui_update()
        except Exception as e:
            self.status_var.set(f"Error cola: {e}")
        finally:
            self.root.after(30, self._drain_queue)

    def _watchdog_tick(self):
        now = time.time()
        if self.running:
            lastf = self.diag.get("last_frame_ts", 0.0)
            if now - lastf > self.diag.get("stall_threshold_s", 2.0):
                self.diag["stalls"] += 1
                self.diag["last_error"] = f"Watchdog: {int(now-lastf)}s sin datos del dispositivo"
                # Sin reconexión automática; el usuario puede actualizar el puerto manualmente
                self.status_var.set("Sin datos del dispositivo (no hay reconexión automática)")
        self.root.after(500, self._watchdog_tick)

    def _health_tick(self):
        try:
            now = time.time()
            # Métricas base
            qcap = self.diag.get("queue_capacity", 100000) or 1
            try:
                qsize = self.ev_q.qsize()
            except Exception:
                qsize = 0
            queue_ratio = min(1.0, max(0.0, qsize / float(qcap)))

            lastf = self.diag.get("last_frame_ts", 0.0)
            lag_s = max(0.0, now - lastf) if lastf else 0.0

            dropped_total = self.ev_dropped
            dt = max(0.1, now - self.health.get("last_health_t", now))
            dropped_rate = max(0.0, (dropped_total - self.health.get("last_drop_count", 0)) / (dt / 60.0))

            # CPU opcional
            cpu_pct = psutil.cpu_percent(interval=None) if psutil else 0.0

            # GPU macOS (opcional, best-effort): intenta powermetrics (requeriría permisos); no bloquea si falla
            gpu_active_pct = self.health.get("gpu_active_pct")
            try:
                # Ejecutar rápido, 1 muestra; si no hay permisos o no existe, ignora
                proc = subprocess.run([
                    "/usr/bin/powermetrics", "--samplers", "gpu_power", "-n", "1"
                ], capture_output=True, text=True, timeout=0.8)
                if proc.returncode == 0 and "GPU Active" in proc.stdout:
                    # Busca líneas tipo: "GPU Active residency: 12%"
                    for line in proc.stdout.splitlines():
                        if "GPU Active" in line and "%" in line:
                            try:
                                gpu_active_pct = float(line.split(":")[-1].strip().rstrip("% "))
                                break
                            except Exception:
                                pass
            except Exception:
                pass

            # Score (100 ideal). Penalizaciones suaves y explicables
            score = 100.0
            # Cola ocupada: hasta -30 pts cuando está al 100%
            score -= 30.0 * queue_ratio
            # Lag: -20 pts si lag >= 2 s; lineal hasta 0
            score -= min(20.0, 10.0 * (lag_s / 1.0))  # ~10 pts por segundo de lag (cap 20)
            # Drops: -20 pts si caen >= 1000 eventos/min
            score -= min(20.0, (dropped_rate / 1000.0) * 20.0)
            # Excepciones serial penalizan 10 (si hubo alguna desde el último tick)
            if self.diag.get("serial_exceptions", 0) > 0:
                score -= 10.0

            score = max(0.0, min(100.0, score))

            # Guardar
            self.health.update({
                "score": score,
                "cpu_pct": cpu_pct,
                "gpu_active_pct": gpu_active_pct,
                "queue_ratio": queue_ratio,
                "lag_s": lag_s,
                "dropped_total": dropped_total,
                "dropped_rate": dropped_rate,
                "last_drop_count": dropped_total,
                "last_health_t": now,
            })

            # Mostrar en la barra de estado sin bloquear
            try:
                gpu_txt = f" | GPU act: {gpu_active_pct:.0f}%" if isinstance(gpu_active_pct, (int, float)) else ""
                self.status_var.set(
                    f"Salud: {score:.0f}/100 | CPU: {cpu_pct:.0f}% | Cola: {int(queue_ratio*100)}% | Lag: {lag_s:.1f}s | Drops/min: {dropped_rate:.0f}{gpu_txt}"
                )
            except Exception:
                pass

        except Exception as e:
            try:
                self.diag["last_error"] = f"health_tick: {e}"
            except Exception:
                pass
        finally:
            # Programar próxima medición
            try:
                self.root.after(2000, self._health_tick)
            except Exception:
                pass

    def _schedule_ui_update(self):
        if self.ui_update_scheduled:
            return
        self.ui_update_scheduled = True
        self.root.after(90, self._flush_ui)

    def _update_raster_points(self):
        return

    def _flush_ui(self):
        start = self.ui_last_idx
        end = len(self.lick_times_ms)
        if end <= start:
            self.ui_update_scheduled = False
            return
        batch_limit = 50
        end_limited = min(end, start + batch_limit)
        for i in range(start, end_limited):
            try:
                idx = i + 1
                tms = self.lick_times_ms[i]
                dms = self.diff_times_ms[i]
                ili_class = self.ili_class_list[i]
                self.tree1.insert("", tk.END, values=(idx, f"{tms:.1f}", f"{dms:.1f}", ili_class))
            except Exception:
                continue
        rows = self.tree1.get_children("")
        excess = len(rows) - self.ui_max_rows
        if excess > 0:
            for iid in rows[:excess]:
                self.tree1.delete(iid)
        rows = self.tree1.get_children("")
        if rows:
            self.tree1.see(rows[-1])

        self.ui_last_idx = end_limited
        if end > end_limited:
            self.root.after(40, self._flush_ui)
            return
        self.ui_update_scheduled = False

    def _compute_clusters(self):
        """Segmenta clusters: secuencias de ILIs positivos donde cada ILI <= ICI_MIN (1000 ms).
        Un ILI > ICI_MIN rompe el cluster. Cada cluster incluye bursts (<=250) e IBIs (251–1000).
        Devuelve un DataFrame con columnas: cluster_id, n_licks, duration_ms.
        """
        ilis = [x for x in self.ili_ms_list if x > 0]
        clusters = []
        run = []
        for x in ilis:
            if x > ICI_MIN:
                # romper cluster actual
                if len(run) >= 1:
                    clusters.append({
                        "cluster_id": len(clusters) + 1,
                        "n_licks": len(run) + 1,           # ILIs + 1
                        "duration_ms": float(np.sum(run)),  # suma de ILIs dentro del cluster
                    })
                run = []
            else:
                run.append(x)
        # cerrar último
        if len(run) >= 1:
            clusters.append({
                "cluster_id": len(clusters) + 1,
                "n_licks": len(run) + 1,
                "duration_ms": float(np.sum(run)),
            })
        return pd.DataFrame(clusters)

    def _compute_posthoc_metrics(self):
        ilis = [float(x) for x in self.ili_ms_list if float(x) > 0]

        bursts_durations = []
        bursts_licks = []
        ibis = []
        icis = []

        run = []  # ILIs dentro de una ráfaga (<= BURST_MAX)

        for x in ilis:
            if x <= BURST_MAX:
                run.append(x)
            else:
                # Si rompemos una ráfaga válida (>=2 ILIs => >=3 licks)
                if len(run) >= 2:
                    burst_duration = float(sum(run))
                    bursts_durations.append(burst_duration)
                    bursts_licks.append(len(run) + 1)

                    # Todo ILI > 250 ms que rompe una ráfaga válida cuenta como IBI
                    ibis.append(float(x))

                    # Si además supera 1000 ms, también cuenta como ICI
                    if x > ICI_MIN:
                        icis.append(float(x))

                run = []

        # Si termina dentro de ráfaga válida
        if len(run) >= 2:
            burst_duration = float(sum(run))
            bursts_durations.append(burst_duration)
            bursts_licks.append(len(run) + 1)

        # Clusters = corrida de ILIs <= 1000 ms
        clusters = []
        c_run = []
        for x in ilis:
            if x <= ICI_MIN:
                c_run.append(x)
            else:
                if c_run:
                    clusters.append(float(sum(c_run)))
                    c_run = []
        if c_run:
            clusters.append(float(sum(c_run)))

        summary = {
            "avg_burst_duration_ms": float(np.mean(bursts_durations)) if bursts_durations else 0.0,
            "n_bursts": int(len(bursts_durations)),
            "avg_ibi_ms": float(np.mean(ibis)) if ibis else 0.0,
            "n_ibis": int(len(ibis)),
            "avg_ici_ms": float(np.mean(icis)) if icis else 0.0,
            "n_icis": int(len(icis)),
            "avg_cluster_size_ms": float(np.mean(clusters)) if clusters else 0.0,
            "n_clusters": int(len(clusters)),
            "avg_cluster_duration_ms": float(np.mean(clusters)) if clusters else 0.0,
        }

        return summary

    def _correct_long_burst(self, run, cap_ms=200.0):
        """
        Dada la lista de ILIs (ms) que forman una ráfaga, si su duración total supera 3000 ms,
        reducir cada ILI a un máximo de `cap_ms` (por defecto 200 ms) y devolver la duración corregida.
        No fracciona la ráfaga en segmentos.
        """
        try:
            # Asegurar valores positivos y numéricos
            cleaned = [float(max(0.0, v)) for v in run]
        except Exception:
            cleaned = [float(v) for v in run]
        capped = [min(v, cap_ms) for v in cleaned]
        return float(sum(capped))

    def _export_excel_analysis(self, base_folder: str):
        """Exporta un XLSX adicional con los eventos clasificados y el resumen de promedios."""
        try:
            os.makedirs(base_folder, exist_ok=True)
            rid = (self.rat_id_1.get().strip() or "rat")
            rid_safe = self._safe_name(rid)
            stamp = self.rat_id_1.get().strip()
            xlsx_path = os.path.join(base_folder, f"{rid_safe}_analysis_{stamp}.xlsx")

            events_df = pd.DataFrame(self.events, columns=["idx", "t_rel_ms", "ili_ms", "class"]) if self.events else pd.DataFrame(columns=["idx","t_rel_ms","ili_ms","class"]) 
            summary = self._compute_posthoc_metrics()
            summary_df = pd.DataFrame([summary])
            clusters_df = self._compute_clusters()

            with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
                events_df.to_excel(writer, index=False, sheet_name="events")
                summary_df.to_excel(writer, index=False, sheet_name="summary")
                clusters_df.to_excel(writer, index=False, sheet_name="clusters")
            return xlsx_path, summary
        except Exception as e:
            self.diag["last_error"] = f"Export Excel error: {e}"
            return None, None

    def _finish_session(self):
        # Evitar ejecución doble si ya se finalizó
        if getattr(self, "_session_finished", False):
            return
        self._session_finished = True
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        if self._timer_job is not None:
            try:
                self.root.after_cancel(self._timer_job)
            except Exception:
                pass
            self._timer_job = None
        if self.start_time is not None:
            elapsed = max(0.0, time.time() - self.start_time)
            minutes = int(elapsed // 60)
            seconds = elapsed % 60
            self.timer_var.set(f"{minutes:02d}:{seconds:04.1f}")

        # Si quedó una ráfaga abierta al detener, contabilizarla si tenía >=2 ILIs
        try:
            if self._in_burst and self._burst_ili_count >= 2:
                self.live_counts["Bursts"] += 1
        except Exception:
            pass

        self.status_var.set("Exportando CSV…")
        try:
            csv_path = self._export_csv()
            self._write_diag()

            # Análisis externo ligero (no bloquear GUI)
            try:
                from lick_analysis_utils import analyze_and_append
                threading.Thread(
                    target=analyze_and_append,
                    args=(csv_path, self.day_var.get(), self.save_dir),
                    daemon=True
                ).start()
            except Exception as e:
                print(f"Error lanzando análisis externo: {e}")

            xlsx_path = None
            summary = None

            msg = (
                f"Sesión finalizada.\n"
                f"CSV guardado en:\n{csv_path}\n\n"
                f"El análisis del día {self.day_var.get()} se está actualizando en segundo plano."
            )

            self.status_var.set(msg)
            try:
                messagebox.showinfo("Resultados", msg)
            except Exception:
                pass
        except Exception as e:
            messagebox.showerror("Exportación", f"Error al exportar: {e}")
            self.status_var.set("Error en exportación.")

    def _write_diag(self):
        try:
            os.makedirs(self.save_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self.save_dir, f"diagnostico_{stamp}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("Lickómetro CH1 – Reporte de Diagnóstico\n")
                f.write("=" * 50 + "\n\n")
                f.write(f"Inicio: {self.diag.get('start_iso','')}\n")
                f.write(f"Puerto: {self.port}\n")
                f.write(f"Baudios: {self.baudrate}\n")
                f.write(f"Duración_s: {self.duration_sec:.3f}\n")
                f.write(f"Min_ILI_ms: {self.min_ili_ms:.1f}\n\n")

                f.write("Contadores\n")
                f.write("-" * 20 + "\n")
                f.write(f"Frames decodificados: {self.diag.get('frames',0)}\n")
                f.write(f"Licks CH1: {self.diag.get('licks_ch1',0)}\n")
                f.write(f"Excepciones serial: {self.diag.get('serial_exceptions',0)}\n")
                f.write(f"Reconexiones serial: {self.diag.get('serial_reconnects',0)}\n")
                f.write(f"Eventos descartados (cola UI): {self.diag.get('queue_dropped',0)} / cap={self.diag.get('queue_capacity',0)}\n")
                f.write(f"Watchdog (stalls): {self.diag.get('stalls',0)} (umbral={self.diag.get('stall_threshold_s',2.0)}s)\n\n")

                f.write("Último estado\n")
                f.write("-" * 20 + "\n")
                lf = self.diag.get("last_frame_ts", 0.0)
                ll = self.diag.get("last_lick_ts", 0.0)
                now = time.time()
                f.write(f"Seg. desde último frame: {now-lf:.3f}\n")
                f.write(f"Seg. desde último lick: {now-ll:.3f}\n")
                f.write(f"Último error: {self.diag.get('last_error','')}\n")
            try:
                messagebox.showinfo("Diagnóstico", f"Frames: {self.diag.get('frames',0)} | "
                                                    f"L1: {self.diag.get('licks_ch1',0)} | "
                                                    f"SerialEx: {self.diag.get('serial_exceptions',0)} | "
                                                    f"Reconn: {self.diag.get('serial_reconnects',0)} | "
                                                    f"Dropped: {self.diag.get('queue_dropped',0)} | "
                                                    f"Stalls: {self.diag.get('stalls',0)}\n"
                                                    f"Se guardó el diagnóstico.")
            except Exception:
                pass
        except Exception as e:
            try:
                messagebox.showwarning("Diagnóstico", f"No se pudo escribir diagnóstico: {e}")
            except Exception:
                pass

    def _export_csv(self):
        os.makedirs(self.save_dir, exist_ok=True)
        rid1 = self.rat_id_1.get().strip() or "rat"
        base_name = self._safe_name(f"{rid1}_CH1")
        out_path = os.path.join(self.save_dir, f"{base_name}.csv")

        if os.path.exists(out_path):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            new_name = f"{base_name}_{stamp}.csv"
            try:
                messagebox.showwarning(
                    "Archivo duplicado",
                    f"Ya existe un archivo con este nombre:\n{out_path}\n\n"
                    f"Se guardará como:\n{new_name}"
                )
            except Exception:
                pass
            out_path = os.path.join(self.save_dir, new_name)

        self._write_csv(out_path)
        return out_path

    def _safe_name(self, s: str) -> str:
        return s.replace("/", "-").replace("\\", "-").replace(":", "-").replace(" ", "_")

    def _write_csv(self, path: str):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["rat_id", self.rat_id_1.get().strip()])
            w.writerow(["rat_weight_g", self.rat_w_1.get().strip()])
            w.writerow(["bottle_weight_g", self.bot_w_1.get().strip()])
            w.writerow(["channel", 1])
            w.writerow(["port", self.port])
            w.writerow(["baudrate", self.baudrate])
            w.writerow(["start_iso", time.strftime("%Y-%m-%dT%H:%M:%S")])
            w.writerow(["duration_s", f"{self.duration_sec:.3f}"])
            w.writerow(["min_ili_ms", f"{self.min_ili_ms:.1f}"])
            w.writerow([])
            w.writerow(["indice_lick", "t_rel_ms", "delta_ms"])
            for idx, (tms, dms) in enumerate(zip(self.lick_times_ms, self.diff_times_ms), start=1):
                w.writerow([idx, f"{tms:.1f}", f"{dms:.1f}"])

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    SingleLickometerGUI().run()