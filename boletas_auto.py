"""
BOLETAS AUTOMATICAS - Kedro (app.codetus.com/POS)

Uso:
  1) Escribe cuantas boletas son y el monto de cada una (de los vouchers impresos).
  2) El programa muestra la lista y compara el total con el de la maquina.
  3) Se abre el navegador, entra solo al sistema y emite todas las boletas.

A prueba de cortes: si se va la luz, el internet o cierras el programa, el avance
queda guardado. Al volver a ejecutar, elige C para continuar donde se quedo.

Ejecutar:  python boletas_auto.py
"""

import csv
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

URL_POS = "https://app.codetus.com/POS"
URL_LOGIN = "https://app.codetus.com/"

# ---- DATOS DE ACCESO (editalos con el Bloc de notas) ----
RUC = "10201043331"
USUARIO = "LUZ"
CLAVE = "123"
# ----------------------------------------------------------

REVISAR_PRIMERA = False   # True = pausa antes de facturar la primera boleta (pide ENTER)

ARCHIVO_PROGRESO = Path("progreso_boletas.json")
CARPETA_HISTORIAL = Path("historial")          # tandas terminadas o archivadas
ARCHIVO_CSV = Path("boletas_emitidas.csv")     # registro de cada boleta emitida (abre con Excel)
ESPERAS_REINTENTO = [5, 15, 30, 60]            # segundos entre reintentos si falla antes de facturar
CARPETA_PERFIL = "perfil_kedro"   # el navegador recuerda sesion y la guia aqui
PAUSA_ENTRE_BOLETAS = 1.0         # segundos; sube el valor si la pagina va lenta
CONDICION_PAGO = "6"              # valor que elegiste en la grabacion
REINTENTOS = 3                    # veces que intenta facturar si sale el error del sistema

# Los botones llevan un icono antes del texto, por eso se buscan con expresion regular
BTN_AGREGAR = re.compile(r"^\W*Agregar\W*$")
LINK_FACTURAR_F9 = re.compile(r"^\W*FACTURAR\s*F9\W*$")
BTN_FACTURAR = re.compile(r"^\W*FACTURAR\W*$")

DIAGNOSTICO = True                 # True = guarda fotos y un registro de cada paso (carpeta "diagnostico")
CARPETA_DIAG = Path("diagnostico")

_estado = {"guia_revisada": False, "boleta": 0}


class Tee:
    """Escribe en pantalla y tambien en un archivo de registro."""

    def __init__(self, *destinos):
        self.destinos = destinos

    def write(self, texto):
        for d in self.destinos:
            d.write(texto)
            d.flush()

    def flush(self):
        for d in self.destinos:
            d.flush()


def foto(page, paso):
    """Guarda una foto de la pantalla (solo en modo diagnostico)."""
    if not DIAGNOSTICO:
        return
    try:
        CARPETA_DIAG.mkdir(exist_ok=True)
        page.screenshot(path=str(CARPETA_DIAG / f"boleta{_estado['boleta']:03d}_{paso}.png"))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# PASO 1: pedir los montos
# ---------------------------------------------------------------------------
def leer_monto(texto):
    """Acepta 12.50 o 12,50. Devuelve None si no es valido."""
    try:
        valor = round(float(texto.strip().replace(",", ".")), 2)
        return valor if valor > 0 else None
    except ValueError:
        return None


def pedir_montos():
    while True:
        try:
            cantidad = int(input("Cuantas boletas son? "))
            if cantidad > 0:
                break
        except ValueError:
            pass
        print("Escribe un numero entero mayor que 0.")

    montos = []
    print("(Si te equivocas, escribe B para borrar el monto anterior)")
    while len(montos) < cantidad:
        n = len(montos) + 1
        texto = input(f"  Monto de la boleta {n} de {cantidad}: S/ ").strip()
        if texto.upper() == "B":
            if montos:
                quitado = montos.pop()
                print(f"  Se borro S/ {quitado:.2f}. Vuelve a escribir la boleta {len(montos) + 1}.")
            continue
        monto = leer_monto(texto)
        if monto is None:
            print("  Monto no valido, intenta de nuevo.")
            continue
        montos.append(monto)
    return montos


def pedir_total_maquina():
    """Total que muestra la maquina de cobro: sirve para detectar errores al digitar."""
    while True:
        texto = input("\nTotal que indica la maquina de cobro (ENTER si no lo sabes): S/ ").strip()
        if texto == "":
            return None
        total = leer_monto(texto)
        if total is not None:
            return total
        print("Monto no valido.")


# ---------------------------------------------------------------------------
# COLA DE BOLETAS: cada boleta tiene un estado que se guarda en disco al instante
#   pendiente  -> todavia no se toco (es seguro repetirla)
#   en_proceso -> se esta facturando (si se corta, hay que verificar en Kedro)
#   emitida    -> ya salio
#   omitida    -> la quitaste de la lista
# ---------------------------------------------------------------------------
ETIQUETAS = {"pendiente": "pendiente", "en_proceso": "EN DUDA", "emitida": "emitida", "omitida": "quitada"}


def ahora():
    return datetime.now().strftime("%H:%M:%S")


class Cola:
    def __init__(self, items, total_maquina=None, creada=None):
        self.items = items
        self.total_maquina = total_maquina
        self.creada = creada or datetime.now().isoformat(timespec="seconds")

    @classmethod
    def nueva(cls, montos, total_maquina=None):
        return cls([{"monto": m, "estado": "pendiente", "hora": None} for m in montos], total_maquina)

    @classmethod
    def cargar(cls):
        datos = json.loads(ARCHIVO_PROGRESO.read_text(encoding="utf-8"))
        return cls(datos["items"], datos.get("total_maquina"), datos.get("creada"))

    def guardar(self):
        """Guardado seguro: si se va la luz a mitad de guardar, el archivo anterior queda intacto."""
        datos = {
            "creada": self.creada,
            "actualizada": datetime.now().isoformat(timespec="seconds"),
            "total_maquina": self.total_maquina,
            "items": self.items,
        }
        temporal = ARCHIVO_PROGRESO.with_suffix(".tmp")
        with open(temporal, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporal, ARCHIVO_PROGRESO)

    def marcar(self, n, estado):
        self.items[n]["estado"] = estado
        self.items[n]["hora"] = ahora()
        self.guardar()

    def cuantas(self, estado):
        return sum(1 for it in self.items if it["estado"] == estado)

    def hay_trabajo(self):
        return any(it["estado"] in ("pendiente", "en_proceso") for it in self.items)

    def total_listado(self):
        return round(sum(it["monto"] for it in self.items if it["estado"] != "omitida"), 2)

    def total_emitido(self):
        return round(sum(it["monto"] for it in self.items if it["estado"] == "emitida"), 2)


def archivar_tanda():
    """Mueve el archivo de progreso a la carpeta 'historial'."""
    if ARCHIVO_PROGRESO.exists():
        CARPETA_HISTORIAL.mkdir(exist_ok=True)
        destino = CARPETA_HISTORIAL / f"progreso_{datetime.now():%Y%m%d_%H%M%S}.json"
        shutil.move(str(ARCHIVO_PROGRESO), str(destino))


def registrar_emision(cola, n, nota=""):
    """Anota la boleta en boletas_emitidas.csv (sirve para cuadrar con la maquina)."""
    nuevo = not ARCHIVO_CSV.exists()
    with open(ARCHIVO_CSV, "a", newline="", encoding="utf-8-sig") as f:
        escritor = csv.writer(f)
        if nuevo:
            escritor.writerow(["fecha", "hora", "numero", "monto", "nota"])
        escritor.writerow([datetime.now().strftime("%Y-%m-%d"), ahora(), n + 1, f"{cola.items[n]['monto']:.2f}", nota])


def mostrar_cola(cola):
    print("\n----- LISTA DE BOLETAS -----")
    for n, it in enumerate(cola.items, 1):
        print(f"{n:>4}.  S/ {it['monto']:>9.2f}   {ETIQUETAS[it['estado']]}")
    print("----------------------------")
    print(
        f"Emitidas: {cola.cuantas('emitida')}   Pendientes: {cola.cuantas('pendiente')}   "
        f"TOTAL de la lista: S/ {cola.total_listado():.2f}"
    )
    if cola.total_maquina is not None:
        dif = round(cola.total_listado() - cola.total_maquina, 2)
        if abs(dif) < 0.005:
            print(f"Total de la maquina: S/ {cola.total_maquina:.2f}  -> COINCIDE")
        else:
            print(f"Total de la maquina: S/ {cola.total_maquina:.2f}  -> DIFERENCIA de S/ {dif:+.2f} (revisa los montos)")


def menu_edicion(cola):
    """Permite corregir, quitar o agregar boletas PENDIENTES antes de empezar o continuar."""
    while True:
        mostrar_cola(cola)
        resp = input(
            "\nENTER = empezar | numero = corregir ese monto | Q numero = quitar esa boleta | A = agregar una: "
        ).strip().upper()

        if resp == "":
            if cola.total_maquina is not None and abs(cola.total_listado() - cola.total_maquina) >= 0.005:
                if input("El total NO coincide con la maquina. Escribe IGNORAR para empezar igual: ").strip().upper() != "IGNORAR":
                    continue
            cola.guardar()
            return

        if resp == "A":
            monto = leer_monto(input("Monto de la nueva boleta: S/ "))
            if monto is not None:
                cola.items.append({"monto": monto, "estado": "pendiente", "hora": None})
                cola.guardar()
            else:
                print("Monto no valido.")
            continue

        quitar = resp.startswith("Q")
        numero = resp[1:].strip() if quitar else resp
        if not numero.isdigit() or not (1 <= int(numero) <= len(cola.items)):
            print("Opcion no valida.")
            continue

        n = int(numero) - 1
        if cola.items[n]["estado"] != "pendiente":
            print("Esa boleta ya se emitio (o esta en duda): no se puede cambiar desde aqui.")
            print("Si salio con un monto equivocado, hay que anularla en Kedro (nota de credito).")
            continue

        if quitar:
            cola.items[n]["estado"] = "omitida"
        else:
            nuevo = leer_monto(input(f"Nuevo monto para la boleta {n + 1}: S/ "))
            if nuevo is None:
                print("Monto no valido.")
                continue
            cola.items[n]["monto"] = nuevo
        cola.guardar()


def resolver_dudas(cola):
    """Boletas que se cortaron justo al facturar: el usuario confirma si salieron."""
    for n, it in enumerate(cola.items):
        if it["estado"] != "en_proceso":
            continue
        print(f"\nLa boleta {n + 1} (S/ {it['monto']:.2f}) quedo EN DUDA: se estaba facturando a las {it['hora']}.")
        print("Revisa en Kedro (lista de ventas) si esa boleta llego a emitirse.")
        respuesta = ""
        while respuesta not in ("S", "N"):
            respuesta = input("Salio en Kedro?   S = si salio   N = no salio: ").strip().upper()
        if respuesta == "S":
            cola.marcar(n, "emitida")
            registrar_emision(cola, n, "confirmada a mano")
        else:
            cola.marcar(n, "pendiente")


def preparar_cola():
    """Retoma una tanda sin terminar o crea una nueva."""
    previa = None
    if ARCHIVO_PROGRESO.exists():
        try:
            previa = Cola.cargar()
        except Exception:
            archivar_tanda()      # archivo de una version anterior o danado
            previa = None

    if previa is not None and previa.hay_trabajo():
        mostrar_cola(previa)
        print("\nQuedo una tanda SIN TERMINAR (se corto el internet, la luz o se detuvo el programa).")
        eleccion = ""
        while eleccion not in ("C", "N"):
            eleccion = input(
                "C = continuar esa tanda   N = empezar una nueva (la anterior se guarda en 'historial'): "
            ).strip().upper()
        if eleccion == "C":
            resolver_dudas(previa)
            menu_edicion(previa)
            return previa
        archivar_tanda()
    elif previa is not None:
        archivar_tanda()          # tanda terminada: se guarda en el historial

    montos = pedir_montos()
    cola = Cola.nueva(montos, pedir_total_maquina())
    menu_edicion(cola)
    return cola


# ---------------------------------------------------------------------------
# PASO 2: emitir UNA boleta en Kedro (pasos tomados de tu grabacion)
# ---------------------------------------------------------------------------
def iniciar_sesion(page):
    """Entra solo al sistema. Si ya hay sesion, sigue. Si falla, pide hacerlo a mano."""
    campo_ruc = page.get_by_role("textbox", name="Tu Ruc")
    try:
        campo_ruc.wait_for(state="visible", timeout=6000)
    except PWTimeout:
        print("Ya habia una sesion iniciada.")
        return

    try:
        campo_ruc.fill(RUC)
        page.get_by_role("textbox", name="Tu Usuario").fill(USUARIO)
        page.get_by_role("textbox", name="••••••••").fill(CLAVE)
        page.get_by_role("button", name="ACCEDER").click()
        campo_ruc.wait_for(state="hidden", timeout=20000)
        print("Sesion iniciada.")
    except PWTimeout:
        input(
            "\nNo se pudo entrar solo (revisa RUC, usuario y clave en el archivo).\n"
            "Inicia sesion tu en la ventana y presiona ENTER aqui para seguir... "
        )


def cerrar_tutorial(page):
    """
    Cierra la guia de bienvenida del POS con paciencia: espera a que aparezca
    (solo la primera vez) y la va cerrando paso por paso hasta que desaparece.
    """
    capa = page.locator(".pos-tour-overlay").first

    if not _estado["guia_revisada"]:
        try:
            capa.wait_for(state="visible", timeout=4000)
        except PWTimeout:
            _estado["guia_revisada"] = True
            return
        _estado["guia_revisada"] = True

    omitir = page.get_by_role("button", name="Omitir").first

    sin_capa = 0
    for _ in range(20):
        try:
            # Si la guia tiene boton Omitir, se usa ese
            if omitir.is_visible():
                omitir.click(timeout=2000)
                page.wait_for_timeout(700)
                continue
            if capa.is_visible():
                sin_capa = 0
                capa.click(timeout=2000)
            else:
                sin_capa += 1
                if sin_capa >= 3:
                    return
        except PWTimeout:
            pass
        page.wait_for_timeout(700)


def descartar_pendientes(page, esperar=False):
    """
    Si Kedro ofrece recuperar ventas pendientes (de una boleta que quedo a medias),
    las descarta, para empezar siempre con el carrito vacio.
    Con esperar=True aguarda unos segundos a que el aviso aparezca (despues de entrar).
    """
    if esperar:
        try:
            page.get_by_role("button", name="Descartar").first.wait_for(state="visible", timeout=4000)
        except PWTimeout:
            return

    descartadas = 0
    sin_aviso = 0
    for _ in range(30):
        boton = page.get_by_role("button", name="Descartar").first
        try:
            if boton.is_visible():
                sin_aviso = 0
                boton.click(timeout=3000)
                descartadas += 1
                print(f"    (se descarto la venta pendiente {descartadas})")
                # Pausa larga: despues de cerrar uno puede aparecer otro aviso
                page.wait_for_timeout(2000)
            else:
                if descartadas == 0:
                    return          # no habia nada pendiente, seguir rapido
                sin_aviso += 1
                if sin_aviso >= 2:  # confirmar dos veces que ya no sale otro
                    return
                page.wait_for_timeout(1500)
        except PWTimeout:
            return


def boton_agregar_unico(page):
    """
    Espera a que la busqueda 'vario' deje UN SOLO producto en la lista y devuelve
    su boton Agregar. Si quedan varios, se detiene en vez de agregar uno al azar.
    """
    candidatos = [
        page.get_by_role("button", name=BTN_AGREGAR),
        page.get_by_role("button", name="Agregar"),
    ]
    for _ in range(50):  # unos 10 segundos
        for boton in candidatos:
            if boton.count() == 1:
                return boton
        page.wait_for_timeout(200)

    cantidad = candidatos[0].count()
    raise RuntimeError(
        f"La busqueda 'vario' no dejo un solo producto (hay {cantidad} botones Agregar)."
    )


def preparar_venta(page, monto, recargar=True):
    """Abre el POS limpio, agrega el producto 'vario' y pone el monto."""
    if recargar:
        page.goto(URL_POS)
    campo = page.get_by_role("textbox", name="Ingrese descripción del")
    campo.wait_for(state="visible", timeout=20000)
    page.wait_for_timeout(500)

    cerrar_tutorial(page)
    descartar_pendientes(page)
    foto(page, "1_inicio")

    campo.click()
    campo.fill("")
    campo.press_sequentially("vario", delay=40)
    boton_agregar_unico(page).click()
    foto(page, "2_producto")

    casilla = page.get_by_role("cell", name="1.00 ..").get_by_placeholder("1")
    casilla.wait_for(state="visible", timeout=10000)

    # El nombre de la celda cambia al escribir, asi que se usa una referencia fija
    campo_precio = casilla.element_handle()
    campo_precio.click()
    campo_precio.dblclick()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")      # borra el 1.00 que viene por defecto
    page.wait_for_timeout(200)
    page.keyboard.type(f"{monto:.2f}", delay=30)

    # Quitar el foco del precio SIN usar Tab (Tab saltaba al enlace FACTURAR)
    campo_precio.evaluate("el => el.blur()")

    # Comprobar que el subtotal en pantalla es el monto antes de facturar
    esperado = f"{monto:,.2f}"
    patron = re.compile(r"Subtotal\s*(?:S/\.?\s*)?" + re.escape(esperado))
    visto = "no se ve"
    for _ in range(30):
        texto = page.inner_text("body")
        if patron.search(texto):
            break
        m = re.search(r"Subtotal\s*(?:S/\.?\s*)?([\d.,]+)", texto)
        visto = m.group(1) if m else "no se ve"
        page.wait_for_timeout(300)
    else:
        raise RuntimeError(f"El subtotal en pantalla es {visto} y deberia ser {esperado}.")
    print(f"    precio puesto: S/ {esperado}")
    foto(page, "3_precio")


def facturar(page, antes_de_facturar=None):
    """
    Abre FACTURAR, elige la condicion de pago y factura.
    Si el sistema muestra su error (a veces pasa), cierra el aviso y reintenta
    sobre la misma venta, tal como lo hiciste a mano.
    """
    print("    abriendo FACTURAR (tecla F9)...")
    cuadro_pago = page.locator('select[name="ListaVentaCondicionPago[0][idCondicionPago]"]')
    page.keyboard.press("F9")
    try:
        cuadro_pago.wait_for(state="visible", timeout=5000)
    except PWTimeout:
        # Si F9 no abrio el cuadro, activar el enlace sin depender de que este libre
        print("    (F9 no abrio el cuadro, usando el enlace)")
        page.get_by_role("link", name=LINK_FACTURAR_F9).dispatch_event("click")
    foto(page, "4_facturar_abierto")

    for intento in range(1, REINTENTOS + 1):
        condicion = page.locator('select[name="ListaVentaCondicionPago[0][idCondicionPago]"]')
        condicion.wait_for(state="visible", timeout=10000)
        condicion.select_option(CONDICION_PAGO)
        foto(page, "5_condicion")
        print("    condicion de pago elegida, presionando FACTURAR...")
        if antes_de_facturar and intento == 1:
            antes_de_facturar()   # desde aqui la boleta puede salir: queda "EN DUDA" si algo se corta
        page.get_by_role("button", name=BTN_FACTURAR).click()

        try:
            # Si todo sale bien, el OK abre la ventana con el comprobante
            with page.expect_popup(timeout=12000) as info:
                page.get_by_role("button", name="OK", exact=True).click(timeout=12000)
            info.value.close()
            foto(page, "6_emitida")
            return
        except PWTimeout:
            # Salio el error del sistema: cerrar el aviso y reintentar
            print(f"    (aviso del sistema, reintento {intento}/{REINTENTOS})")
            foto(page, f"6_aviso_error_{intento}")
            try:
                page.get_by_role("button", name="OK", exact=True).click(timeout=2000)
            except PWTimeout:
                pass
            page.wait_for_timeout(1000)

    raise RuntimeError("El sistema no dejo facturar despues de varios intentos.")


def emitir_boleta(page, monto, revisar=False, recargar=True, antes_de_facturar=None):
    # Antes de facturar es seguro repetir: aun no se emitio nada
    for intento in range(1, 3):
        try:
            preparar_venta(page, monto, recargar=(recargar or intento > 1))
            break
        except PWTimeout:
            if intento == 2:
                raise
            print("    (la pagina no respondio, recargando...)")

    if revisar:
        input(
            f"\nREVISION: mira la pantalla del POS. El total debe ser S/ {monto:.2f}.\n"
            "Si esta bien, presiona ENTER para facturar. Si no, cierra con Ctrl+C... "
        )

    facturar(page, antes_de_facturar)


# ---------------------------------------------------------------------------
# PASO 3: bucle principal a prueba de cortes
# ---------------------------------------------------------------------------
class ErrorEnDuda(Exception):
    """Fallo DESPUES de presionar FACTURAR: no se sabe si la boleta salio."""


def resumen_error(error):
    lineas = str(error).strip().splitlines()
    return lineas[0] if lineas else type(error).__name__


def conectar(page):
    """Abre Kedro e inicia sesion; si no hay internet, espera y reintenta."""
    for espera in [0] + ESPERAS_REINTENTO:
        if espera:
            print(f"    sin conexion, reintentando en {espera} s...")
            time.sleep(espera)
        try:
            page.goto(URL_LOGIN)
            iniciar_sesion(page)
            # Justo despues de entrar tambien aparecen la guia y el aviso de recuperar
            page.wait_for_load_state("domcontentloaded")
            cerrar_tutorial(page)
            descartar_pendientes(page, esperar=True)
            return
        except Exception as error:
            print(f"    no se pudo abrir Kedro: {resumen_error(error)}")
    raise RuntimeError("No hay conexion con Kedro. Revisa el internet y vuelve a ejecutar: el avance esta guardado.")


def procesar_boleta(page, cola, n, primera):
    """
    Emite la boleta n.
    - Si falla ANTES de facturar (internet, pagina lenta...): espera y reintenta solo.
    - Si falla DESPUES de facturar: NO repite (podria duplicarse) y pide verificar.
    """
    monto = cola.items[n]["monto"]
    intentos = len(ESPERAS_REINTENTO) + 1
    for intento in range(intentos):
        try:
            ya_en_pos = "/pos" in page.url.lower()
            emitir_boleta(
                page,
                monto,
                revisar=(REVISAR_PRIMERA and primera),
                recargar=not (primera and intento == 0 and ya_en_pos),
                antes_de_facturar=lambda: cola.marcar(n, "en_proceso"),
            )
            return
        except KeyboardInterrupt:
            raise
        except Exception as error:
            if cola.items[n]["estado"] == "en_proceso":
                raise ErrorEnDuda(resumen_error(error))
            if intento == intentos - 1:
                raise
            espera = ESPERAS_REINTENTO[intento]
            print(f"    (fallo: {resumen_error(error)})")
            print(f"    reintento {intento + 1} de {intentos - 1} en {espera} s...")
            foto(page, f"fallo_{intento + 1}")
            time.sleep(espera)


def imprimir_resumen(cola):
    activas = len(cola.items) - cola.cuantas("omitida")
    print("\n===== RESUMEN =====")
    print(
        f"Emitidas: {cola.cuantas('emitida')} de {activas}   "
        f"Pendientes: {cola.cuantas('pendiente')}   En duda: {cola.cuantas('en_proceso')}"
    )
    print(f"Total emitido: S/ {cola.total_emitido():.2f}", end="")
    if cola.total_maquina is not None:
        print(f"   (maquina: S/ {cola.total_maquina:.2f})")
    else:
        print()


def main():
    if DIAGNOSTICO:
        CARPETA_DIAG.mkdir(exist_ok=True)
        sys.stdout = Tee(sys.__stdout__, open(CARPETA_DIAG / "registro.txt", "a", encoding="utf-8"))
        print(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====")

    cola = preparar_cola()
    if cola.cuantas("pendiente") == 0:
        print("\nNo hay boletas pendientes. Nada que hacer.")
        imprimir_resumen(cola)
        return

    codigo = 0
    with sync_playwright() as p:
        contexto = p.chromium.launch_persistent_context(CARPETA_PERFIL, headless=False)
        page = contexto.pages[0] if contexto.pages else contexto.new_page()
        page.set_default_timeout(15000)   # si algo no responde, falla en 15 s en vez de 30

        try:
            conectar(page)
            primera = True
            for n, item in enumerate(cola.items):
                if item["estado"] != "pendiente":
                    continue
                _estado["boleta"] = n + 1
                procesar_boleta(page, cola, n, primera)
                primera = False
                cola.marcar(n, "emitida")
                registrar_emision(cola, n)
                print(f"[{n + 1}/{len(cola.items)}] Boleta emitida: S/ {item['monto']:.2f}")
                time.sleep(PAUSA_ENTRE_BOLETAS)
            print("\nListo: todas las boletas de la lista fueron emitidas.")

        except KeyboardInterrupt:
            print("\n\nPausado por ti. El avance quedo guardado.")
            print("Vuelve a ejecutar el programa y elige C para continuar.")
            codigo = 1
        except ErrorEnDuda as error:
            print(f"\n*** SE CORTO justo al facturar la boleta {_estado['boleta']}: {error}")
            print("No se sabe si esa boleta salio. Revisala en Kedro (lista de ventas).")
            print("Al volver a ejecutar y elegir C, el programa te preguntara si salio o no.")
            foto(page, "en_duda")
            codigo = 1
        except Exception as error:
            print(f"\n*** SE DETUVO en la boleta {_estado['boleta']}: {resumen_error(error)}")
            try:
                page.screenshot(path="error_pantalla.png")
                print("Se guardo una foto de la pantalla en: error_pantalla.png")
            except Exception:
                pass
            print("Ninguna boleta quedo a medias. Vuelve a ejecutar y elige C para continuar.")
            codigo = 1
        finally:
            imprimir_resumen(cola)
            try:
                contexto.close()
            except Exception:
                pass

    sys.exit(codigo)


if __name__ == "__main__":
    main()
