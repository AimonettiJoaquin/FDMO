# Sesión del 30/10/2026 — 07:46 a 19:44

Revisión de las 123 trazas y 59.700 muestras de la última ejecución.
El corte final fue voluntario mediante Ctrl+C.

## Resultado

| Indicador | Valor |
| --- | ---: |
| Duración | 11:57:59 |
| Victorias | 120 |
| Derrotas | 1 |
| Abandonos del bot | 1 |
| Partidas interrumpidas | 1 |
| Victorias sobre 122 duelos resueltos, incluido el abandono | 98,36 % |
| Dinero neto observado | +25.588 |
| Dinero por minuto | 35,64 |
| Mediana de duración de las primeras 122 trazas, incluida la apertura | 5:37,47 |

El rendimiento monetario subió aproximadamente un 31,7 % frente a los
27,06/min reconstruidos en la sesión anterior. La comparación no aísla el
efecto de cada cambio: también dependen las cartas y situaciones de cada duelo.

## Recuperaciones observadas

Hubo 36 entradas de recuperación en 33 partidas:

| Motivo | Entradas |
| --- | ---: |
| Resultado de ataque no confirmado | 23 |
| Prueba de ataque directo sin resultado confirmado | 4 |
| Gaia Power sin efecto de activación verificado | 4 |
| Summon sin elección de zona observada | 2 |
| Paso a END no confirmado | 2 |
| Recuperación de fase agotada | 1 |

No se registraron recuperaciones por localizar objetivos mediante hover,
Raigeki consumido sin limpiar el campo ni paneles grandes de búsqueda.
Los 27 casos de verificación de ataques siguen siendo la principal fuente
de recuperación. No corresponde declararlos éxitos solo porque se cierre
un selector: pueden existir cancelaciones o efectos sin daño.

## Derrota: partida 18

Terminó con 0 LP propios y 10.200 rivales. Antes de perder se registró una
invocación cuyo clic no abrió una elección de zona. Más tarde el campo propio
seguía vacío frente a St. Joan de 2.800 ATK y White Magician Pikeru; al terminar
el turno hubo un descarte obligatorio de la mano de siete cartas.

La invocación fallida es un punto concreto que revisar, pero estas muestras
no permiten atribuir toda la derrota a un único clic. Conviene mejorar la
confirmación del menú de invocación y revisar el uso de remoción cuando el
campo queda vacío frente a un monstruo de mayor ATK.

## Abandono: partida 42

La traza que terminó fuera del duelo sin LP a cero es la 42; corresponde al
único abandono del resumen. La clasificación usa ese resumen junto a las
trazas: los estados de memoria aislados no contienen el evento de clic que
permite distinguir Forfeit de una salida no clasificada.

Después de `Summon no abrió la elección de zona`, la recuperación intentó
END dos veces. Apareció después el aviso de colocación de **King Tiger Wanghu**,
con la carta todavía en la misma mano. El flujo lo trató como un bloqueo de
fase hasta agotar la recuperación. La partida salió con 5.900 LP propios y
11.800 rivales.

Se añadió una recuperación específica para esa colocación tardía. Exige:

- Haber registrado el clic de invocación de esa copia de carta.
- Mantener el mismo duelo y la mano completa del intento original.
- Reconocer el texto y los parámetros exactos del aviso de Normal Summon.
- Observar una zona libre válida y confirmar la colocación por el flujo habitual.

El contexto se descarta al cambiar de turno o confirmar la invocación. La
reproducción de tres muestras reales del bloqueo vuelve a producir
`recovery_summon_zone_ready`, `checking_zone_choice` y `zone_clicked`, con un
clic simulado de colocación y sin teclas de END o Forfeit. Es una validación
de la ruta de recuperación; no demuestra cuál habría sido el resultado del
duelo con esa entrada nueva.

## Demoras y siguientes mejoras

Se observaron 113 separaciones entre muestras mayores a ocho segundos, que
suman 1.668,819 segundos. Estos intervalos incluyen escaneo, lectura del
monitor y planificación; no son una medida exclusiva del tiempo de escaneo.

Las prioridades siguientes son:

1. Investigar los 27 casos de ataque sin confirmación con eventos de entrada
   y estado del controlador registrados junto a cada fallo.
2. Separar un clic ignorado de una invocación cuyo selector aparece tarde;
   ya se corrigió la recuperación de la segunda situación.
3. Registrar bytes y duración de cada descubrimiento de escena en la propia
   traza para medir el efecto de las prioridades de búsqueda.
4. Revisar los cuatro menús de Gaia Power que se cerraron sin una activación
   verificada y sin gastar la carta.

La última partida, 123, terminó por Ctrl+C a los 117,5 segundos de traza,
con 8.000 LP de cada lado y sin recuperaciones. Su clasificación como
«Sin resultado» es correcta.

## Preparación del proyecto

Se conservaron los módulos de ejecución, lectura, estrategia y calibración,
las ocho plantillas necesarias y las pruebas de esas funciones. Se retiraron
capturas completas, video, cachés, registros históricos, un layout estático
antiguo y utilidades de casino, edición de moneda e investigaciones ajenas al
bot. La lectura de saldo para métricas sigue incluida.

La configuración y los registros locales están excluidos de Git. El README,
el ejemplo de configuración, las dependencias y el workflow de pruebas permiten
preparar una instalación nueva. La ruta del juego se configura con `DMO_GAME_DIR`
y el monitor usa el mismo intérprete del lanzador.

Validación local: **515 pruebas pasan** sobre el árbol limpio. El recuento es
menor que el de la revisión anterior porque se retiraron las pruebas de las
utilidades eliminadas y se agregaron casos de recuperación y configuración.
No se enviaron entradas a una partida real durante esta revisión.
