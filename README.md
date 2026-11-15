# DMO Bot

Bot de farmeo desarrollado en Python para DMO, una aplicación de escritorio
ejecutada en Linux con Wine y Hyprland. Abre duelos mediante reconocimiento
visual, lee el estado del cliente y controla las cartas mediante teclado y ratón.

El proyecto incluye control por estados, recuperación de operaciones sin
confirmar, registros de diagnóstico y métricas de sesión.

El monitor abre la memoria del proceso en modo de lectura. Las acciones del
bot se realizan con `ydotool`; cada colocación, ataque o cambio de fase requiere
una transición observada antes de continuar.

## Requisitos

- Linux con `/proc`, Wine y una instalación propia de DMO.
- Hyprland, `hyprctl`, `grim`, `ydotool` con su servicio `ydotoold` operativo,
  y `sudo` para el monitor de lectura.
- Python 3.12 o posterior, NumPy y Pillow.
- Juego visible, sin menús iniciales abiertos y con la dificultad elegida
  manualmente. El rival configurado es Bonz.

La calibración incluida usa 1920×1080. El control del cursor depende del
dispatch Lua `hl.dsp.cursor.move(...)` empleado por este entorno de Hyprland.
El modo `--window` adapta las coordenadas a una ventana, pero sigue siendo
experimental. La lectura nativa de Unity comprueba el binario compatible;
una actualización del juego puede requerir revisar el lector.

## Instalación

Desde la carpeta del proyecto:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.json
```

La carpeta predeterminada del juego es
`~/Games/DMO/DMO/DMO/Release`. Para otra instalación y para guardar el nombre
del personaje:

```bash
cp .env.example .env
# Editar .env con la ruta que contiene GameAssembly.dll y DMO_Data/.
source .env
```

Repetí `source .env` en cada nueva terminal desde la que ejecutes el bot.

También se puede definir la ruta directamente:

```bash
export DMO_GAME_DIR="/ruta/al/juego/Release"
```

El lanzador transmite esa ruta al monitor bajo `sudo` y utiliza el mismo
intérprete de Python que ejecutó el usuario. No hay una ruta de usuario fija
en el código. `config.json`, `.env`, capturas y registros quedan fuera de Git.

## Ejecutar

Abrí DMO con tu personaje en el mundo, sin menús. Ejecutá el lanzador como
usuario normal; pedirá autenticación para iniciar el monitor:

```bash
# Una partida completa, con mágicas y estrategia de combate.
python dmo_run_bot.py --name TuPersonaje

# Comprobar un turno antes de ejecutar varios duelos.
python dmo_run_bot.py --name TuPersonaje --turns 1

# Repetir hasta detenerlo con Ctrl+C.
python dmo_run_bot.py --name TuPersonaje --matches 0
```

`--name` también puede configurarse con `DMO_CHARACTER` en `.env`.
Dejá el foco en el juego durante la ejecución. El bot respeta la dificultad
elegida manualmente y fija la cámara al comenzar cada duelo.

`Ctrl+C` detiene el bot, cierra su monitor y conserva el resumen de sesión.
La partida interrumpida se cuenta como «Sin resultado». El apagado de la PC
solo se solicita si se pasó `--shutdown-on-exit`; **Ctrl+C lo cancela**.

| Opción | Uso |
| --- | --- |
| `--matches N` | Cantidad de duelos; `0` repite indefinidamente. |
| `--turns N` | Limita los turnos del primer duelo. |
| `--delay N` | Segundos para volver al juego después de autenticar. |
| `--monsters-only` | Ejecuta el bucle sin mágicas. |
| `--window` | Modo experimental para ventana o borderless. |
| `--config RUTA` | Usa otra configuración local. |
| `--trace RUTA` | Elige dónde guardar la captura JSONL. |
| `--session-log-dir RUTA` | Elige la carpeta de resúmenes persistentes. |
| `--spell-probe` | Diagnóstico: se detiene al seleccionar una mágica. |
| `--shutdown-on-exit` | Solicita apagar el equipo al salir; Ctrl+C lo cancela. |

La repetición de partidas requiere jugar el duelo completo y no se combina
con `--turns` ni `--spell-probe`. `--monsters-only` tampoco se combina con
`--spell-probe`.

## Calibración

`config.example.json` incluye las coordenadas y tiempos de referencia.
Copialo a `config.json` y ajustá la geometría para tu escritorio y cámara.
Las plantillas recortadas de `templates/` son necesarias para abrir duelos;
las capturas completas y los videos de investigación no forman parte del bot.

La calibración completa reinicia la configuración a partir de los valores
base. Si ya tenés ajustes propios, guardá una copia de `config.json` antes
de ejecutarla.

```bash
# Grabar coordenadas y plantillas desde el juego.
python dmo_bot.py calibrate

# Actualizar solo las plantillas visuales.
python dmo_bot.py capture-templates

# Actualizar una referencia concreta.
python dmo_bot.py capture-template duel_lobby
```

Después de calibrar, revisá las opciones específicas del modo de memoria:

- `zones.own_monsters` y `zones.opponent_monsters`: posiciones de monstruos.
- `zones.memory_opponent_targets` y `zones.memory_opponent_confirms`:
  coordenadas por casilla para objetivo y confirmación rival.
- `attack_sword_offset` y `target_confirm_offset`: botones relativos.
- `memory_camera_scroll_up`: pasos para fijar la cámara; la referencia usa 12.

Los puntos rivales se guardan de izquierda a derecha; sus índices de memoria
se interpretan desde la perspectiva del rival. El bot verifica las cartas
seleccionadas y no interpreta un clic ignorado como campo vacío.

## Estrategia y límites

El mazo implementado combina monstruos de nivel 4 o menor con búsquedas,
robo, Gaia Power y remoción. Incluye reglas para Enraged Battle Ox,
Gyaku-Gire Panda y D.D. Assailant. Las decisiones usan las acciones ofrecidas
por el cliente y las estadísticas visibles; no leen la identidad ni las
estadísticas de monstruos rivales boca abajo.

Puede efectuar una invocación normal por turno, seleccionar búsquedas y
descartes, combatir, pasar de fase y recuperarse de transiciones sin confirmar.
Una colocación de invocación que aparece tarde puede retomarse si coinciden
la carta exacta, la mano y el aviso del intento original.

El cálculo numérico no modela todas las cartas, trampas o cadenas del juego.
Los cambios de versión, resolución, composición del mazo o posición de cámara
pueden requerir ajustes. Las pruebas automáticas utilizan memoria y escritorio
simulados; una ejecución real sigue siendo necesaria para validar cambios de
entrada o calibración.

## Registros y métricas

- Estados por partida: `/tmp/dmo-run-FECHA-HORA*.jsonl`, o la ruta de `--trace`.
- Recuperaciones y fallos: archivos `.recovery.jsonl` y `.error.json` junto a
  la traza.
- Capturas y caché de objetos: `debug/`.
- Resúmenes de sesión: `logs/sessions/session-*.json`.

Se crean automáticamente al ejecutar. El resumen distingue victorias,
derrotas, abandonos confirmados e interrupciones sin resultado. Las trazas de
estado por sí solas no contienen todos los eventos de clic necesarios para
reconstruir un abandono.

Las métricas de dinero corresponden a la moneda de DMO. El dinero neto es la
diferencia entre el último saldo observado y el primero. Dinero/min divide
esa diferencia por la duración total de la sesión en minutos. No se atribuye
cada cambio automáticamente a una recompensa. Una muestra de duelo inestable
puede conservar el saldo anterior hasta tres segundos, mostrando su antigüedad;
un fallo explícito de lectura del saldo deja la métrica como no disponible.

El análisis de la sesión de 123 partidas está en
[docs/session-review-2026-10-30.md](docs/session-review-2026-10-30.md).

## Desarrollo y pruebas

```bash
python -m unittest discover -v
python dmo_run_bot.py --help
python monitor_dmo.py --help
```

Las pruebas no necesitan instalar DMO, acceder a su proceso ni controlar el
escritorio. El workflow de GitHub Actions está configurado para ejecutarlas
con Python 3.12 y 3.14 usando las acciones oficiales
[checkout](https://github.com/actions/checkout) y
[setup-python](https://github.com/actions/setup-python).

Estructura principal:

| Archivo o carpeta | Función |
| --- | --- |
| `dmo_run_bot.py` | Inicio de duelos, repetición y cierre de sesión. |
| `monitor_dmo.py`, `inspect_dmo_memory.py` | Lectura y validación de memoria. |
| `dmo_loop_bot.py`, `dmo_turn_bot.py` | Flujo del duelo y ejecución de turnos. |
| `dmo_advisor.py`, `dmo_farm_strategy.py`, `dmo_battle.py` | Decisiones. |
| `dmo_spell_controller.py`, `dmo_card_panel.py` | Mágicas y selecciones. |
| `dmo_scene.py`, `dmo_transform.py` | Objetos de interfaz y vínculos nativos. |
| `dmo_settings.py` | Ruta configurable de instalación. |
| `dmo_bot.py`, `templates/` | Reconocimiento visual y calibración. |
| `tests/` | Pruebas y fixtures de memoria simulada. |

## Subir a GitHub

Creá un repositorio vacío en tu cuenta. Desde esta carpeta:

```bash
git init -b main
git add .
git diff --cached --stat
git diff --cached --check
git commit -m "Preparar bot DMO"
git remote add origin URL_DEL_REPOSITORIO
git push -u origin main
```

La configuración personal, entornos virtuales, trazas, cachés y capturas están
excluidos por `.gitignore`. El juego y sus binarios no se distribuyen con este
proyecto. Todavía no se ha definido una licencia para el código.
