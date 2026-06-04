# Lickómetro CH1 – Guía de uso

Este programa permite registrar eventos de lamido en un lickómetro de un solo canal (**CH1**) mediante comunicación serial. La interfaz gráfica permite seleccionar el puerto USB-serial, configurar la duración de la sesión, registrar los datos del animal y guardar automáticamente los archivos generados al finalizar la adquisición.

El archivo principal del programa es:

```text
lickometer_ch1_gui.py
```

Opcionalmente, el programa puede ejecutar un análisis posterior si en la misma carpeta existe el archivo:

```text
lick_analysis_utils.py
```

Este archivo no es necesario para registrar y guardar los datos crudos, pero sí para actualizar automáticamente el análisis del día después de cada sesión.

---

## 1. Requisitos

### Sistema operativo

El programa está diseñado principalmente para macOS, aunque puede adaptarse a Windows o Linux si se modifica la selección del puerto serial.

En macOS, los puertos suelen aparecer como:

```text
/dev/cu.usbserial*
/dev/cu.usbmodem*
/dev/tty.usbserial*
/dev/tty.usbmodem*
```

### Python

Se recomienda usar Python 3.8 o superior.

Para verificar la versión instalada:

```bash
python3 --version
```

### Librerías necesarias

Instalar las dependencias con:

```bash
pip install pyserial numpy matplotlib pandas openpyxl psutil
```

También puede usarse:

```bash
python3 -m pip install pyserial numpy matplotlib pandas openpyxl psutil
```

`psutil` es opcional. Si no está instalado, el programa seguirá funcionando, pero no mostrará métricas detalladas de CPU/memoria.

---

## 2. Archivos del proyecto

La carpeta mínima del programa debe contener:

```text
lickometer_ch1_gui.py
```

Si se desea que el programa ejecute análisis automático posterior, agregar también:

```text
lick_analysis_utils.py
```

Ejemplo de estructura recomendada:

```text
Lickometro_CH1/
├── lickometer_ch1_gui.py
├── lick_analysis_utils.py
├── README.md
└── datos/
```

La carpeta `datos/` no es obligatoria, pero se recomienda usarla para guardar los CSV, diagnósticos y archivos de análisis.

---

## 3. Conexión del lickómetro

1. Conectar el lickómetro a la computadora mediante USB.
2. Verificar que el dispositivo esté encendido.
3. Abrir el programa.
4. Presionar **Actualizar puertos** para detectar los puertos disponibles.
5. Si el puerto no se selecciona automáticamente, presionar **Elegir puerto…** y seleccionar el puerto correspondiente al lickómetro.

El programa prioriza dispositivos USB-serial compatibles con controladores como:

```text
CH340
CP210
FTDI
Silabs
Arduino
USB-serial
```

---

## 4. Ejecutar el programa

Desde la terminal, entrar a la carpeta donde está el archivo:

```bash
cd ruta/a/Lickometro_CH1
```

Ejecutar:

```bash
python3 lickometer_ch1_gui.py
```

Si se usa un ambiente virtual o Conda, activar primero el ambiente correspondiente.

Ejemplo con Conda:

```bash
conda activate nombre_del_ambiente
python lickometer_ch1_gui.py
```

---

## 5. Configuración de una sesión

Antes de iniciar la adquisición, llenar los campos de la interfaz:

### Duración

Duración de la sesión en minutos.

Ejemplo:

```text
10
```

para una sesión de 10 minutos.

### Día

Identificador experimental de la sesión. Puede ser:

```text
D1
D2
D3
S1
S2
```

o cualquier código definido por el experimento.

### CH1 ID

Identificador del animal.

Ejemplos:

```text
R1
R2
SCOP_HB_R4
Veh_R3
```

### Peso rata (g)

Peso corporal del animal en gramos.

Ejemplo:

```text
285
```

### Peso bebedero (g)

Peso inicial del bebedero en gramos.

Ejemplo:

```text
652.4
```

Este dato queda guardado como metadato en el CSV.

---

## 6. Iniciar una sesión

Para iniciar:

1. Revisar que el puerto serial sea correcto.
2. Llenar los datos del animal.
3. Presionar **Iniciar**.
4. Seleccionar la carpeta donde se guardarán los archivos.
5. Colocar al animal en el rig durante la cuenta regresiva de 5 segundos.
6. La adquisición inicia automáticamente al terminar la cuenta regresiva.

Durante la sesión, la interfaz muestra:

- Tiempo transcurrido.
- Eventos detectados.
- Tiempo relativo de cada lick.
- Intervalo entre licks.
- Clasificación del intervalo.
- Estado general del sistema.

---

## 7. Detener una sesión

La sesión termina automáticamente cuando se cumple la duración programada.

También puede detenerse manualmente con el botón:

```text
Detener
```

Al finalizar, el programa guarda automáticamente los datos.

---

## 8. Archivos generados

Al terminar la sesión, el programa genera un archivo CSV con los datos crudos del animal.

El nombre del archivo sigue el formato:

```text
ID_CH1.csv
```

Ejemplo:

```text
R4_CH1.csv
```

Si ya existe un archivo con ese nombre, el programa genera una copia con fecha y hora para evitar sobrescribir datos:

```text
R4_CH1_20260604_183012.csv
```

También genera un archivo de diagnóstico:

```text
diagnostico_YYYYMMDD_HHMMSS.txt
```

---

## 9. Formato del archivo CSV

El CSV contiene primero metadatos de la sesión:

```text
rat_id
rat_weight_g
bottle_weight_g
channel
port
baudrate
start_iso
duration_s
min_ili_ms
```

Después contiene la tabla de eventos:

```text
indice_lick,t_rel_ms,delta_ms
```

Donde:

- `indice_lick`: número consecutivo del lick.
- `t_rel_ms`: tiempo relativo desde el inicio de la sesión, en milisegundos.
- `delta_ms`: intervalo entre el lick actual y el lick anterior, en milisegundos.

Ejemplo:

```text
indice_lick,t_rel_ms,delta_ms
1,1523.4,0.0
2,1680.1,156.7
3,1837.0,156.9
4,2010.5,173.5
```

El primer lick tiene `delta_ms = 0.0` porque no existe un lick previo.

---

## 10. Clasificación en vivo de los ILIs

El programa clasifica los intervalos entre licks usando los siguientes umbrales:

```text
BURST_MAX = 250 ms
IBI_MIN   = 250 ms
ICI_MIN   = 1000 ms
```

La clasificación es:

| Intervalo | Clasificación |
|---|---|
| ILI ≤ 250 ms | Burst |
| 250 ms < ILI ≤ 1000 ms | IBI |
| ILI > 1000 ms | ICI |
| Primer lick o valor inválido | Reset/Invalid |

En esta versión, una ráfaga se considera válida cuando contiene al menos 3 licks consecutivos, equivalentes a por lo menos 2 ILIs consecutivos dentro del umbral de ráfaga.

---

## 11. Diferencia entre CSV crudo y análisis posterior

El CSV principal guarda los datos crudos de la sesión. Este archivo es el respaldo principal y no debe modificarse manualmente.

El análisis posterior, si está disponible mediante `lick_analysis_utils.py`, se ejecuta en segundo plano después de finalizar la sesión.

El programa intenta ejecutar:

```python
from lick_analysis_utils import analyze_and_append
```

y después llama:

```python
analyze_and_append(csv_path, day, save_dir)
```

Si `lick_analysis_utils.py` no existe o tiene errores, el registro crudo no se pierde. El programa solo mostrará un mensaje en la terminal indicando que no pudo lanzar el análisis externo.

---

## 12. Diagnóstico del sistema

El archivo de diagnóstico incluye información útil para detectar problemas de adquisición:

- Puerto usado.
- Baudios.
- Duración de la sesión.
- Número de frames decodificados.
- Número de licks detectados.
- Excepciones seriales.
- Eventos descartados por saturación de la cola.
- Periodos sin datos del dispositivo.
- Último error registrado.

Ejemplo de variables reportadas:

```text
Frames decodificados
Licks CH1
Excepciones serial
Reconexiones serial
Eventos descartados
Watchdog stalls
```

Este archivo debe revisarse si una sesión parece incompleta o si el lickómetro dejó de registrar eventos.

---

## 13. Problemas comunes

### No aparece ningún puerto

Verificar:

1. Que el lickómetro esté conectado.
2. Que el cable USB funcione.
3. Que el dispositivo esté encendido.
4. Que el driver USB-serial esté instalado.
5. Presionar **Actualizar puertos**.

En macOS, puede revisarse desde terminal con:

```bash
ls /dev/cu.*
```

### El programa abre, pero no registra licks

Verificar:

1. Que el puerto seleccionado sea el correcto.
2. Que el lickómetro esté enviando datos.
3. Que el sensor esté alineado.
4. Que el haz infrarrojo se interrumpa correctamente.
5. Que el baudrate coincida con el firmware del dispositivo.

Este programa usa:

```text
115200 baudios
```

Si el firmware usa otro baudrate, debe modificarse esta línea:

```python
self.baudrate = 115200
```

### Aparece error de conexión serial

Puede ocurrir si otro programa está usando el puerto.

Cerrar otros programas como:

- Arduino IDE.
- Serial Monitor.
- TeraTerm.
- PuTTY.
- Otro proceso de Python.

Después presionar **Reiniciar puerto** o cerrar y abrir de nuevo el programa.

### La sesión guarda pocos licks

Revisar:

1. El archivo de diagnóstico.
2. El número de frames decodificados.
3. Si hubo excepciones seriales.
4. Si hubo periodos largos sin datos.
5. La alineación física del sensor.

### El archivo CSV no aparece

Verificar que se haya seleccionado una carpeta de guardado al iniciar la sesión.

Si la sesión se canceló antes de iniciar, no se genera CSV.

---

## 14. Recomendaciones de uso experimental

Antes de una sesión real:

1. Hacer una prueba corta de 1 minuto.
2. Interrumpir manualmente el sensor para confirmar que registra eventos.
3. Confirmar que el CSV se guarda correctamente.
4. Revisar que los tiempos `t_rel_ms` y `delta_ms` tengan valores razonables.
5. Verificar que el archivo de diagnóstico no reporte errores seriales.

Durante experimentos formales:

1. Usar siempre el mismo formato de ID para los animales.
2. No sobrescribir archivos crudos.
3. Guardar cada sesión en una carpeta organizada por experimento y día.
4. Respaldar los CSV originales antes de analizarlos.
5. Registrar manualmente cualquier problema ocurrido durante la sesión.

Ejemplo de organización:

```text
Proyecto_Escopolamina/
└── datos_conductuales/
    └── lickometro/
        └── crudos/
            ├── SCOP_HB/
            │   ├── S1/
            │   └── S2/
            └── VEH/
                ├── S1/
                └── S2/
```

---

## 15. Variables importantes del código

Los umbrales de microestructura están definidos al inicio del archivo:

```python
BURST_MAX = 250.0
IBI_MIN   = 250.0
ICI_MIN   = 1000.0
```

Si el análisis del proyecto usa otro umbral de ráfaga, por ejemplo 330 ms, modificar:

```python
BURST_MAX = 330.0
IBI_MIN   = 330.0
```

El umbral `ICI_MIN` puede mantenerse en 1000 ms si se desea conservar la definición de pausa larga.

El baudrate se define dentro de la clase `SingleLickometerGUI`:

```python
self.baudrate = 115200
```

---

## 16. Notas importantes

El archivo CSV generado por este programa debe considerarse el dato crudo principal.

No se recomienda modificar manualmente el CSV original. Si se requiere limpieza, conversión o análisis, generar archivos derivados en una carpeta separada.

La clasificación en vivo de Burst, IBI e ICI sirve como referencia durante la sesión. El análisis estadístico final debe realizarse posteriormente con el pipeline de análisis definido para el experimento.

---

## 17. Contacto y mantenimiento

Para modificar el programa, revisar principalmente las siguientes secciones:

- Selección de puerto: `list_serial_candidates()`
- Inicio de sesión: `start_session()`
- Lectura serial: `_read_serial_loop()`
- Registro de licks: `_register_lick()`
- Clasificación de ILIs: `_drain_queue()`
- Exportación CSV: `_export_csv()` y `_write_csv()`
- Diagnóstico: `_write_diag()`
- Análisis externo: bloque dentro de `_finish_session()`

Antes de modificar el código para experimentos formales, probar siempre con sesiones cortas y conservar una copia funcional del programa original.
