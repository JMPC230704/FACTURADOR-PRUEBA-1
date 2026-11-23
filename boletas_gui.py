"""
BOLETAS KEDRO - programa con ventanas (no hace falta usar el cmd).

Debe estar en la MISMA carpeta que boletas_auto.py (ese es el motor).
Se abre con doble clic en  Boletas.bat  o, ya convertido, con  Boletas Kedro.exe
"""

import glob
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

# --- Carpeta fija donde se guarda todo (avance, registro CSV, historial, config) ---
CARPETA_DATOS = Path.home() / "BoletasKedro"
CARPETA_DATOS.mkdir(exist_ok=True)
os.chdir(CARPETA_DATOS)

if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boletas_auto as ba  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

ARCHIVO_CONFIG = CARPETA_DATOS / "config.json"

# --- Colores y letra ---
FUENTE = "Segoe UI"
FONDO = "#f4f6f8"
OSCURO = "#0b1220"
VERDE = "#16a34a"
VERDE_OSC = "#15803d"
ROJO = "#dc2626"
NARANJA = "#d97706"
GRIS = "#6b7280"

# El motor guarda el avance desde dos hilos (ventana y emision): se protege con un candado
_candado = threading.RLock()
_guardar_original = ba.Cola.guardar


def _guardar_seguro(self):
    with _candado:
        _guardar_original(self)


ba.Cola.guardar = _guardar_seguro


class LoginFallido(BaseException):
    """No se pudo iniciar sesion (clave mala o navegador oculto)."""


def cargar_config():
    try:
        return json.loads(ARCHIVO_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def guardar_config(cfg):
    ARCHIVO_CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def fmt(x):
    return f"{x:,.2f}"


class LogWriter:
    """Lleva lo que el motor 'imprime' hacia el panel de detalles."""

    def __init__(self, cola):
        self.cola = cola

    def write(self, texto):
        if texto and texto.strip():
            self.cola.put(("log", texto.rstrip()))
        return len(texto) if texto else 0

    def flush(self):
        pass

    def isatty(self):
        return False


def navegador_instalado():
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(Path.home() / "AppData" / "Local" / "ms-playwright")
    return bool(glob.glob(os.path.join(base, "chromium-*")))


def instalar_navegador():
    """Descarga el navegador que usa el programa (solo la primera vez en cada PC)."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    resultado = compute_driver_executable()
    comando = list(resultado) if isinstance(resultado, (tuple, list)) else [str(resultado)]
    banderas = 0x08000000 if sys.platform == "win32" else 0   # sin ventana negra
    subprocess.run(comando + ["install", "chromium"], env=get_driver_env(), check=True, creationflags=banderas)


def extraer_montos(texto):
    """Saca los montos de un texto pegado (uno por linea, separados por espacios, etc.)."""
    montos = []
    for trozo in re.findall(r"\d+(?:[.,]\d{1,2})?", texto):
        valor = ba.leer_monto(trozo)
        if valor is not None:
            montos.append(valor)
    return montos


class App:
    def __init__(self, root):
        self.root = root
        root.title("Boletas Kedro")
        root.geometry("1020x740")
        root.minsize(940, 660)
        root.configure(bg=FONDO)

        self.q = queue.Queue()
        sys.stdout = LogWriter(self.q)
        sys.stderr = LogWriter(self.q)
        ba.input = self.pregunta_desde_hilo
        ba.DIAGNOSTICO = False

        self.cfg = cargar_config()
        self.cola = ba.Cola.nueva([])
        self.corriendo = False
        self.pausar = threading.Event()
        self.respuesta = threading.Event()
        self.actual = None
        self._cache = {}

        self.var_monto = tk.StringVar()
        self.var_maquina = tk.StringVar()
        self.var_ver = tk.BooleanVar(value=self.cfg.get("ver_navegador", True))

        self.estilos()
        self.construir()
        root.protocol("WM_DELETE_WINDOW", self.cerrar)
        root.after(200, self.sondear)
        root.after(300, self.refrescar_periodico)
        root.after(400, self.al_iniciar)

    # ------------------------------------------------------------------ estilo
    def estilos(self):
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # letra nitida en Windows
        except Exception:
            pass
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Treeview", font=(FUENTE, 11), rowheight=30, background="white", fieldbackground="white", borderwidth=0)
        st.configure("Treeview.Heading", font=(FUENTE, 10, "bold"), background="#e5e7eb", relief="flat")
        st.map("Treeview", background=[("selected", "#cfe8ff")], foreground=[("selected", OSCURO)])
        st.configure("Acento.TButton", font=(FUENTE, 11, "bold"), background=VERDE, foreground="white", padding=(14, 8), borderwidth=0)
        st.map("Acento.TButton", background=[("active", VERDE_OSC)])
        st.configure("Grande.TButton", font=(FUENTE, 15, "bold"), background=VERDE, foreground="white", padding=(10, 16), borderwidth=0)
        st.map("Grande.TButton", background=[("active", VERDE_OSC), ("disabled", "#9ca3af")], foreground=[("disabled", "white")])
        st.configure("Pausa.TButton", font=(FUENTE, 11, "bold"), background=NARANJA, foreground="white", padding=(10, 8), borderwidth=0)
        st.map("Pausa.TButton", background=[("active", "#b45309"), ("disabled", "#d1d5db")], foreground=[("disabled", "white")])
        st.configure("Suave.TButton", font=(FUENTE, 10), padding=(10, 6))
        st.configure("Horizontal.TProgressbar", troughcolor="#e5e7eb", background=VERDE, thickness=18, borderwidth=0)

    # --------------------------------------------------------------- pantalla
    def construir(self):
        cab = tk.Frame(self.root, bg=OSCURO)
        cab.pack(fill="x")
        tk.Label(cab, text="🧾  Boletas Kedro", font=(FUENTE, 20, "bold"), fg="white", bg=OSCURO).pack(side="left", padx=20, pady=14)
        barra = tk.Frame(cab, bg=OSCURO)
        barra.pack(side="right", padx=14)
        ttk.Button(barra, text="📂 Registros", style="Suave.TButton", command=self.abrir_carpeta).pack(side="left", padx=4)
        ttk.Button(barra, text="⚙ Datos de acceso", style="Suave.TButton", command=self.abrir_config).pack(side="left", padx=4)

        cuerpo = tk.Frame(self.root, bg=FONDO)
        cuerpo.pack(fill="both", expand=True, padx=18, pady=(14, 8))
        cuerpo.columnconfigure(0, weight=3)
        cuerpo.columnconfigure(1, weight=2)
        cuerpo.rowconfigure(0, weight=1)

        # ---- izquierda: escribir montos y lista
        izq = tk.Frame(cuerpo, bg=FONDO)
        izq.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        izq.rowconfigure(1, weight=1)
        izq.columnconfigure(0, weight=1)

        entrada = tk.Frame(izq, bg=FONDO)
        entrada.grid(row=0, column=0, sticky="ew")
        tk.Label(entrada, text="Monto del voucher (escribe y presiona ENTER)", font=(FUENTE, 10), fg=GRIS, bg=FONDO).pack(anchor="w")
        fila = tk.Frame(entrada, bg=FONDO)
        fila.pack(fill="x", pady=(4, 10))
        self.entry = ttk.Entry(fila, textvariable=self.var_monto, font=(FUENTE, 22), width=9, justify="right")
        self.entry.pack(side="left", ipady=6)
        self.entry.bind("<Return>", self.agregar)
        ttk.Button(fila, text="＋ Agregar", style="Acento.TButton", command=self.agregar).pack(side="left", padx=10)
        ttk.Button(fila, text="📋 Pegar lista", style="Suave.TButton", command=self.pegar).pack(side="left")

        marco = tk.Frame(izq, bg="white", highlightbackground="#e5e7eb", highlightthickness=1)
        marco.grid(row=1, column=0, sticky="nsew")
        marco.rowconfigure(0, weight=1)
        marco.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(marco, columns=("n", "monto", "estado"), show="headings", selectmode="browse")
        self.tree.heading("n", text="N°")
        self.tree.heading("monto", text="Monto (S/)")
        self.tree.heading("estado", text="Estado")
        self.tree.column("n", width=60, anchor="center", stretch=False)
        self.tree.column("monto", width=130, anchor="e", stretch=False)
        self.tree.column("estado", width=200, anchor="w")
        barra_v = ttk.Scrollbar(marco, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=barra_v.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        barra_v.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", self.corregir)
        self.tree.bind("<Delete>", self.quitar)
        for etiqueta, fondo, letra in (
            ("pendiente", "white", OSCURO),
            ("emitida", "#e6f6ec", "#166534"),
            ("duda", "#fff3dc", "#92400e"),
            ("omitida", "#f3f4f6", "#9ca3af"),
            ("curso", "#e0efff", "#1d4ed8"),
        ):
            self.tree.tag_configure(etiqueta, background=fondo, foreground=letra)

        pie = tk.Frame(izq, bg=FONDO)
        pie.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(pie, text="✎ Corregir monto", style="Suave.TButton", command=self.corregir).pack(side="left", padx=(0, 6))
        ttk.Button(pie, text="✖ Quitar", style="Suave.TButton", command=self.quitar).pack(side="left", padx=6)
        self.btn_nueva = ttk.Button(pie, text="🗑 Nueva tanda", style="Suave.TButton", command=self.nueva_tanda)
        self.btn_nueva.pack(side="right")
        tk.Label(pie, text="Doble clic = corregir · Supr = quitar", font=(FUENTE, 9), fg=GRIS, bg=FONDO).pack(side="right", padx=12)

        # ---- derecha: totales y boton grande
        der = tk.Frame(cuerpo, bg="white", highlightbackground="#e5e7eb", highlightthickness=1)
        der.grid(row=0, column=1, sticky="nsew")
        interior = tk.Frame(der, bg="white")
        interior.pack(fill="both", expand=True, padx=20, pady=18)

        tk.Label(interior, text="TOTAL DE LAS BOLETAS", font=(FUENTE, 9, "bold"), fg=GRIS, bg="white").pack(anchor="w")
        self.lbl_total = tk.Label(interior, text="S/ 0.00", font=(FUENTE, 32, "bold"), fg=OSCURO, bg="white")
        self.lbl_total.pack(anchor="w")
        self.lbl_cuenta = tk.Label(interior, text="0 boletas", font=(FUENTE, 11), fg=GRIS, bg="white")
        self.lbl_cuenta.pack(anchor="w", pady=(0, 12))

        tk.Label(interior, text="Total que indica la máquina de cobro", font=(FUENTE, 9, "bold"), fg=GRIS, bg="white").pack(anchor="w")
        self.entry_maquina = ttk.Entry(interior, textvariable=self.var_maquina, font=(FUENTE, 15), justify="right")
        self.entry_maquina.pack(fill="x", pady=(4, 4), ipady=3)
        self.entry_maquina.bind("<FocusOut>", lambda e: self.cola.guardar())
        self.lbl_comp = tk.Label(interior, text="", font=(FUENTE, 11, "bold"), bg="white", fg=GRIS, anchor="w", justify="left")
        self.lbl_comp.pack(anchor="w", pady=(0, 14))

        self.progreso = ttk.Progressbar(interior, mode="determinate", maximum=100)
        self.progreso.pack(fill="x")
        self.lbl_prog = tk.Label(interior, text="", font=(FUENTE, 10), fg=GRIS, bg="white")
        self.lbl_prog.pack(anchor="w", pady=(4, 14))

        self.btn_emitir = ttk.Button(interior, text="▶  EMITIR", style="Grande.TButton", command=self.emitir)
        self.btn_emitir.pack(fill="x")
        self.btn_pausar = ttk.Button(interior, text="⏸  Pausar", style="Pausa.TButton", command=self.pedir_pausa)
        self.btn_pausar.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(interior, text="Ver el navegador mientras trabaja", variable=self.var_ver, command=self.guardar_opcion).pack(anchor="w", pady=(12, 0))

        self.lbl_estado = tk.Label(interior, text="", font=(FUENTE, 10), fg=GRIS, bg="white", wraplength=300, justify="left")
        self.lbl_estado.pack(anchor="w", pady=(10, 0))

        # ---- abajo: detalles
        detalle = tk.LabelFrame(self.root, text=" Detalles ", font=(FUENTE, 9), fg=GRIS, bg=FONDO)
        detalle.pack(fill="x", padx=18, pady=(0, 14))
        self.log = tk.Text(detalle, height=6, font=("Consolas", 9), bg="#0f172a", fg="#cbd5e1", relief="flat", state="disabled")
        barra_log = ttk.Scrollbar(detalle, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=barra_log.set)
        self.log.pack(side="left", fill="x", expand=True)
        barra_log.pack(side="right", fill="y")

        self.var_maquina.trace_add("write", lambda *a: self.refrescar())
        self.entry.focus()

    # ------------------------------------------------------------- utilidades
    def guardar_opcion(self):
        self.cfg["ver_navegador"] = bool(self.var_ver.get())
        guardar_config(self.cfg)

    def escribir_log(self, texto):
        self.log.configure(state="normal")
        self.log.insert("end", texto + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 600:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def abrir_carpeta(self):
        try:
            os.startfile(CARPETA_DATOS)
        except Exception:
            messagebox.showinfo("Registros", f"Los registros están en:\n{CARPETA_DATOS}")

    def sonido(self):
        try:
            import winsound

            winsound.MessageBeep()
        except Exception:
            self.root.bell()

    def maquina(self):
        return ba.leer_monto(self.var_maquina.get())

    # ------------------------------------------------- arranque / tanda previa
    def al_iniciar(self):
        if not (self.cfg.get("ruc") and self.cfg.get("usuario") and self.cfg.get("clave")):
            self.abrir_config(primera_vez=True)

        if not ba.ARCHIVO_PROGRESO.exists():
            return
        try:
            previa = ba.Cola.cargar()
        except Exception:
            ba.archivar_tanda()
            return

        if previa.hay_trabajo():
            texto = (
                "Quedó una tanda SIN TERMINAR (se cortó la luz, el internet o se cerró el programa):\n\n"
                f"   ✔ Emitidas: {previa.cuantas('emitida')}\n"
                f"   ⏳ Pendientes: {previa.cuantas('pendiente')}\n"
                f"   ⚠ En duda: {previa.cuantas('en_proceso')}\n\n"
                "¿Continuar con esa tanda?\n\n"
                "Sí = continuar donde se quedó\nNo = descartarla y empezar de cero"
            )
            if messagebox.askyesno("Tanda sin terminar", texto, icon="warning"):
                self.cola = previa
                if previa.total_maquina is not None:
                    self.var_maquina.set(f"{previa.total_maquina:.2f}")
                self.resolver_dudas()
            else:
                ba.archivar_tanda()
        elif previa.cuantas("emitida"):
            ba.archivar_tanda()
        else:
            ba.ARCHIVO_PROGRESO.unlink(missing_ok=True)
        self.refrescar()

    def resolver_dudas(self):
        """Boletas que se cortaron justo al facturar: se confirma si salieron."""
        for n, it in enumerate(self.cola.items):
            if it["estado"] != "en_proceso":
                continue
            r = messagebox.askyesnocancel(
                "Boleta en duda",
                f"La boleta {n + 1} (S/ {fmt(it['monto'])}) se estaba facturando a las {it['hora']} "
                "cuando se cortó.\n\nRevisa en Kedro (lista de ventas) y responde:\n\n"
                "Sí = SÍ salió\nNo = NO salió (se volverá a emitir)\nCancelar = decidir después",
                icon="warning",
            )
            if r is True:
                self.cola.marcar(n, "emitida")
                ba.registrar_emision(self.cola, n, "confirmada a mano")
            elif r is False:
                self.cola.marcar(n, "pendiente")

    # ---------------------------------------------------------- editar la lista
    def agregar(self, evento=None):
        monto = ba.leer_monto(self.var_monto.get())
        if monto is None:
            self.root.bell()
            self.entry.focus()
            self.entry.selection_range(0, "end")
            return
        self.cola.items.append({"monto": monto, "estado": "pendiente", "hora": None})
        self.cola.guardar()
        self.var_monto.set("")
        self.entry.focus()
        self.refrescar()
        self.tree.see(str(len(self.cola.items) - 1))

    def pegar(self):
        try:
            texto = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("Pegar lista", "No hay nada copiado.")
            return
        montos = extraer_montos(texto)
        if not montos:
            messagebox.showinfo("Pegar lista", "No encontré montos en lo que copiaste.")
            return
        if messagebox.askyesno("Pegar lista", f"Encontré {len(montos)} montos por un total de S/ {fmt(sum(montos))}.\n\n¿Agregarlos a la lista?"):
            for m in montos:
                self.cola.items.append({"monto": m, "estado": "pendiente", "hora": None})
            self.cola.guardar()
            self.refrescar()

    def seleccionada(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def corregir(self, evento=None):
        n = self.seleccionada()
        if n is None:
            messagebox.showinfo("Corregir", "Primero elige una boleta de la lista.")
            return
        it = self.cola.items[n]
        if it["estado"] != "pendiente" or (self.corriendo and self.actual == n):
            messagebox.showinfo(
                "Corregir",
                "Esa boleta ya se emitió (o se está emitiendo), no se puede cambiar desde aquí.\n"
                "Si salió con un monto equivocado, hay que anularla en Kedro con una nota de crédito.",
            )
            return
        texto = simpledialog.askstring("Corregir monto", f"Nuevo monto para la boleta {n + 1}:", initialvalue=f"{it['monto']:.2f}", parent=self.root)
        if texto is None:
            return
        nuevo = ba.leer_monto(texto)
        if nuevo is None:
            messagebox.showwarning("Corregir", "Ese monto no es válido.")
            return
        it["monto"] = nuevo
        self.cola.guardar()
        self.refrescar()

    def quitar(self, evento=None):
        n = self.seleccionada()
        if n is None:
            return
        it = self.cola.items[n]
        if it["estado"] != "pendiente" or (self.corriendo and self.actual == n):
            messagebox.showinfo("Quitar", "Esa boleta ya se emitió (o se está emitiendo), no se puede quitar.")
            return
        if self.corriendo:
            it["estado"] = "omitida"          # no se borra: la emision esta recorriendo la lista
        else:
            self.cola.items.pop(n)
        self.cola.guardar()
        self.refrescar()

    def nueva_tanda(self):
        if self.corriendo:
            return
        pend = self.cola.cuantas("pendiente") + self.cola.cuantas("en_proceso")
        if pend and not messagebox.askyesno("Nueva tanda", f"Hay {pend} boletas sin emitir. ¿Descartarlas y empezar una tanda nueva?"):
            return
        self.reiniciar_tanda()

    def reiniciar_tanda(self):
        if self.cola.cuantas("emitida") or self.cola.cuantas("en_proceso"):
            ba.archivar_tanda()
        else:
            ba.ARCHIVO_PROGRESO.unlink(missing_ok=True)
        self.cola = ba.Cola.nueva([])
        self.var_maquina.set("")
        self.refrescar()
        self.entry.focus()

    # ---------------------------------------------------------------- pantalla
    def refrescar_periodico(self):
        self.refrescar()
        self.root.after(400, self.refrescar_periodico)

    def refrescar(self):
        items = self.cola.items
        hijos = self.tree.get_children()
        if len(hijos) != len(items):
            self.tree.delete(*hijos)
            self._cache = {}
            for i in range(len(items)):
                self.tree.insert("", "end", iid=str(i), values=("", "", ""))

        for i, it in enumerate(items):
            est = it["estado"]
            en_curso = self.corriendo and self.actual == i and est in ("pendiente", "en_proceso")
            if en_curso:
                texto, tag = "▶ Emitiendo…", "curso"
            elif est == "emitida":
                texto, tag = "✔ Emitida", "emitida"
            elif est == "en_proceso":
                texto, tag = "⚠ EN DUDA", "duda"
            elif est == "omitida":
                texto, tag = "— Quitada", "omitida"
            else:
                texto, tag = "⏳ Pendiente", "pendiente"
            clave = (it["monto"], texto)
            if self._cache.get(i) != clave:
                self._cache[i] = clave
                self.tree.item(str(i), values=(i + 1, fmt(it["monto"]), texto), tags=(tag,))
                if en_curso:
                    self.tree.see(str(i))

        total = self.cola.total_listado()
        activas = len(items) - self.cola.cuantas("omitida")
        emitidas = self.cola.cuantas("emitida")
        pend = self.cola.cuantas("pendiente")
        self.cola.total_maquina = self.maquina()

        self.lbl_total.config(text=f"S/ {fmt(total)}")
        self.lbl_cuenta.config(text=f"{activas} boleta{'s' if activas != 1 else ''}")
        self.progreso["value"] = (100 * emitidas / activas) if activas else 0
        self.lbl_prog.config(text=f"{emitidas} de {activas} emitidas" if activas else "")

        m = self.cola.total_maquina
        if m is None:
            self.lbl_comp.config(text="", fg=GRIS)
        else:
            dif = round(total - m, 2)
            if abs(dif) < 0.005:
                self.lbl_comp.config(text="✔ Coincide con la máquina", fg=VERDE)
            else:
                self.lbl_comp.config(text=f"⚠ Diferencia de S/ {dif:+.2f}\n   Revisa los montos", fg=ROJO)

        if self.corriendo:
            self.btn_emitir.config(state="disabled", text="Emitiendo…")
            self.btn_pausar.config(state="disabled" if self.pausar.is_set() else "normal")
            self.btn_nueva.config(state="disabled")
        else:
            verbo = "CONTINUAR" if emitidas else "EMITIR"
            if pend:
                self.btn_emitir.config(state="normal", text=f"▶  {verbo}  {pend} BOLETA{'S' if pend != 1 else ''}")
            else:
                self.btn_emitir.config(state="disabled", text="▶  EMITIR")
            self.btn_pausar.config(state="disabled")
            self.btn_nueva.config(state="normal")

        if self.corriendo:
            if self.pausar.is_set():
                self.lbl_estado.config(text="Pausando al terminar la boleta actual…", fg=NARANJA)
            else:
                self.lbl_estado.config(text="Trabajando… no cierres la ventana del navegador.", fg=GRIS)
        elif not items:
            self.lbl_estado.config(text="Escribe el monto de cada voucher y presiona ENTER.", fg=GRIS)
        else:
            self.lbl_estado.config(text="", fg=GRIS)

    # ------------------------------------------------------------ configuracion
    def abrir_config(self, primera_vez=False):
        ven = tk.Toplevel(self.root)
        ven.title("Datos de acceso a Kedro")
        ven.configure(bg="white")
        ven.resizable(False, False)
        ven.transient(self.root)
        ven.grab_set()

        valores = {
            "ruc": tk.StringVar(value=self.cfg.get("ruc", ba.RUC)),
            "usuario": tk.StringVar(value=self.cfg.get("usuario", ba.USUARIO)),
            "clave": tk.StringVar(value=self.cfg.get("clave", ba.CLAVE if primera_vez else "")),
        }
        tk.Label(ven, text="Estos datos se guardan solo en esta computadora.", font=(FUENTE, 10), fg=GRIS, bg="white").grid(
            row=0, column=0, columnspan=2, padx=20, pady=(16, 8), sticky="w"
        )
        for fila, (clave, titulo) in enumerate((("ruc", "RUC"), ("usuario", "Usuario"), ("clave", "Contraseña")), start=1):
            tk.Label(ven, text=titulo, font=(FUENTE, 11), bg="white").grid(row=fila, column=0, padx=(20, 10), pady=6, sticky="e")
            ttk.Entry(ven, textvariable=valores[clave], font=(FUENTE, 12), width=24, show="•" if clave == "clave" else "").grid(
                row=fila, column=1, padx=(0, 20), pady=6
            )

        def guardar():
            if not all(v.get().strip() for v in valores.values()):
                messagebox.showwarning("Datos de acceso", "Completa los tres datos.", parent=ven)
                return
            self.cfg.update({k: v.get().strip() for k, v in valores.items()})
            guardar_config(self.cfg)
            ven.destroy()

        ttk.Button(ven, text="Guardar", style="Acento.TButton", command=guardar).grid(row=4, column=0, columnspan=2, pady=(10, 18))
        self.root.wait_window(ven)

    # ----------------------------------------------------------------- emitir
    def emitir(self):
        if self.corriendo:
            return
        if self.cola.cuantas("en_proceso"):
            self.resolver_dudas()
            if self.cola.cuantas("en_proceso"):
                return
        pend = self.cola.cuantas("pendiente")
        if not pend:
            messagebox.showinfo("Emitir", "No hay boletas pendientes.")
            return
        if not (self.cfg.get("ruc") and self.cfg.get("usuario") and self.cfg.get("clave")):
            self.abrir_config(primera_vez=True)
            return

        m = self.maquina()
        if m is not None and abs(self.cola.total_listado() - m) >= 0.005:
            dif = round(self.cola.total_listado() - m, 2)
            if not messagebox.askyesno(
                "El total no coincide",
                f"La suma de la lista es S/ {fmt(self.cola.total_listado())} y la máquina indica S/ {fmt(m)} "
                f"(diferencia de S/ {dif:+.2f}).\n\n¿Emitir de todos modos?",
                icon="warning",
            ):
                return

        total_pend = sum(it["monto"] for it in self.cola.items if it["estado"] == "pendiente")
        if not messagebox.askyesno("Confirmar", f"Se emitirán {pend} boleta{'s' if pend != 1 else ''} por S/ {fmt(total_pend)}.\n\n¿Empezar?"):
            return

        self.corriendo = True
        self.pausar.clear()
        self.cola.guardar()
        threading.Thread(target=self.trabajo, daemon=True).start()
        self.refrescar()

    def pedir_pausa(self):
        self.pausar.set()
        self.refrescar()

    # ---------------------------------------------- hilo de trabajo (navegador)
    def pregunta_desde_hilo(self, aviso=""):
        """El motor pide 'presiona ENTER': se convierte en un cuadro de aviso."""
        if not self.var_ver.get():
            raise LoginFallido()
        self.respuesta.clear()
        self.q.put(("pregunta", "Kedro no dejó entrar solo.\n\nInicia sesión tú en la ventana del navegador y luego presiona Aceptar."))
        self.respuesta.wait()
        return ""

    def trabajo(self):
        codigo, detalle = "ok", ""
        ba.RUC, ba.USUARIO, ba.CLAVE = self.cfg["ruc"], self.cfg["usuario"], self.cfg["clave"]
        ba._estado["guia_revisada"] = False
        try:
            if not navegador_instalado():
                print("Instalando el navegador (solo la primera vez, puede tardar unos minutos)...")
                instalar_navegador()

            with sync_playwright() as p:
                contexto = p.chromium.launch_persistent_context(ba.CARPETA_PERFIL, headless=not self.var_ver.get())
                page = contexto.pages[0] if contexto.pages else contexto.new_page()
                page.set_default_timeout(15000)
                try:
                    ba.conectar(page)
                    primera = True
                    n = 0
                    while n < len(self.cola.items):          # la lista puede crecer mientras se emite
                        item = self.cola.items[n]
                        if item["estado"] != "pendiente":
                            n += 1
                            continue
                        if self.pausar.is_set():
                            codigo = "pausa"
                            break
                        self.actual = n
                        ba._estado["boleta"] = n + 1
                        ba.procesar_boleta(page, self.cola, n, primera)
                        primera = False
                        self.cola.marcar(n, "emitida")
                        ba.registrar_emision(self.cola, n)
                        print(f"[{n + 1}/{len(self.cola.items)}] Boleta emitida: S/ {fmt(item['monto'])}")
                        time.sleep(ba.PAUSA_ENTRE_BOLETAS)
                        n += 1
                except ba.ErrorEnDuda as error:
                    codigo, detalle = "duda", str(error)
                except LoginFallido:
                    codigo = "login"
                except Exception as error:
                    codigo, detalle = "error", ba.resumen_error(error)
                    try:
                        page.screenshot(path="error_pantalla.png")
                    except Exception:
                        pass
                finally:
                    try:
                        contexto.close()
                    except Exception:
                        pass
        except Exception as error:
            codigo, detalle = "error", ba.resumen_error(error)
        self.q.put(("fin", codigo, detalle))

    # ------------------------------------------------- mensajes desde el hilo
    def sondear(self):
        try:
            while True:
                evento = self.q.get_nowait()
                if evento[0] == "log":
                    self.escribir_log(evento[1])
                elif evento[0] == "pregunta":
                    messagebox.showinfo("Kedro", evento[1], parent=self.root)
                    self.respuesta.set()
                elif evento[0] == "fin":
                    self.al_terminar(evento[1], evento[2])
        except queue.Empty:
            pass
        self.root.after(200, self.sondear)

    def al_terminar(self, codigo, detalle):
        self.corriendo = False
        self.actual = None
        self.refrescar()
        self.sonido()
        emitidas = self.cola.cuantas("emitida")
        resumen = f"Emitidas: {emitidas}   ·   Total emitido: S/ {fmt(self.cola.total_emitido())}"

        if codigo == "ok":
            messagebox.showinfo("Listo ✔", f"Todas las boletas fueron emitidas.\n\n{resumen}")
            self.reiniciar_tanda()
        elif codigo == "pausa":
            messagebox.showinfo("En pausa", f"Quedó en pausa. El avance está guardado.\n\n{resumen}\n\nPresiona CONTINUAR cuando quieras seguir.")
        elif codigo == "duda":
            messagebox.showwarning(
                "Revisa una boleta",
                f"Se cortó justo al facturar: {detalle}\n\nNo se sabe si esa boleta salió. Revísala en Kedro (lista de ventas).\n"
                "Al presionar CONTINUAR, el programa te preguntará si salió o no.",
            )
        elif codigo == "login":
            messagebox.showerror("No se pudo entrar", "No se pudo iniciar sesión en Kedro.\n\nRevisa el RUC, usuario y contraseña en ⚙ Datos de acceso.")
        else:
            messagebox.showerror(
                "Se detuvo",
                f"Se detuvo por un problema:\n{detalle}\n\nNinguna boleta quedó a medias. El avance está guardado: "
                "presiona CONTINUAR para seguir.",
            )

    def cerrar(self):
        if self.corriendo and not messagebox.askyesno(
            "Cerrar",
            "Se están emitiendo boletas. Si cierras ahora, puede quedar una boleta en duda.\n\n"
            "Es mejor presionar Pausar y esperar.\n\n¿Cerrar de todos modos?",
            icon="warning",
        ):
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
