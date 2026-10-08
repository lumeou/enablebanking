# Enable Banking para Home Assistant

Integración personalizada, en desarrollo, para leer saldos y movimientos bancarios
(vía [Enable Banking](https://enablebanking.com)) desde Home Assistant, con una
base de datos local propia para auditar y categorizar los movimientos.

**Versión actual: 0.4.1.** Registro de cambios al final del documento.

---

## Instalación

1. **Clave privada** de tu aplicación de Enable Banking: cópiala a
   `/config/enablebanking/private.key` en el servidor de Home Assistant.
   **Nunca** en `/config/www` (esa carpeta es pública en `/local/`); el asistente
   de configuración rechaza esa ruta.
2. **Componente:** copia la carpeta `custom_components/enablebanking` a
   `/config/custom_components/`.
3. **URL de redirección** en el panel de Enable Banking: debe ser exactamente
   `https://<tu-dominio-local>:8123/enablebanking/callback`. **HTTPS es
   obligatorio** en aplicaciones de producción (el sandbox admite HTTP plano).
   Home Assistant necesita entonces tener HTTPS activado (certificado autofirmado
   vale, con resolución local por mDNS/`.local` o tu propio dominio).
4. Reinicia Home Assistant.
5. **Ajustes → Dispositivos y servicios → Añadir integración → Enable Banking.**
   Se abre el login del banco; al terminar, la integración continúa sola (no hay
   que copiar y pegar ninguna URL).
6. Puedes repetir el proceso para añadir **varios bancos**, cada uno como una
   integración independiente, con su propia aplicación de Enable Banking.

## Cómo funciona (arquitectura local-first)

```
Banco → [motor de sincronización] → base de datos local (SQLite) → sensores · servicios · exportación · categorías
```

**Solo el motor de sincronización habla con el banco.** Sensores y servicios leen
siempre de una copia local en `/config/enablebanking/<id_de_la_integración>.db`
(un archivo por banco conectado). Esto existe porque los bancos limitan cuántas
veces al día se les puede consultar sin que el usuario esté delante.

- **Primera descarga:** al vincular la cuenta, pide el histórico más largo posible
  (`strategy=longest`), aprovechando la ventana en que el banco te considera
  "presente".
- **Sincronizaciones siguientes:** incrementales, con 10 días de solapamiento
  sobre el último movimiento guardado, para capturar apuntes tardíos o corregidos.
- **Calendario:** cada 6 horas por defecto (configurable, mínimo 6h), con un tope
  de 4 sincronizaciones reales al día — el límite habitual que aplican los bancos
  a las consultas automáticas. El calendario y el tope se cuentan por separado
  para cada banco conectado.
- **Si el banco devuelve "demasiadas peticiones" (429):** se pausa varias horas
  sola y se sigue sirviendo lo último guardado.
- **Si caduca la autorización (401):** los sensores **no desaparecen** — siguen
  mostrando los últimos datos guardados — y Home Assistant pide reautenticar
  desde Ajustes → Dispositivos y servicios.
- **Deduplicación:** usa `entry_reference` cuando el banco lo da y es único; si
  no, una huella con fecha, importe, contraparte y saldo posterior. Los
  movimientos pendientes (`PDNG`) se sustituyen en cada consulta, nunca se
  acumulan.
- **Al eliminar una integración:** se cierra el consentimiento en el banco **y se
  borra su base de datos local**. Para recuperar el histórico, vuelve a añadir la
  integración y vincula la cuenta de nuevo (consume una autorización, pero no
  cuenta para el límite diario de consultas).

## Explotar los datos en Lovelace

No hace falta ningún sensor adicional de Home Assistant para tablas, tartas o
tendencias: los servicios ya devuelven justo lo necesario, y una plantilla
disparada por evento (`template:` con `trigger:`) guarda ese resultado en un
sensor propio que Lovelace puede leer. El patrón, aportado y verificado en la
práctica: llamar al servicio dentro de la plantilla con `response_variable`, y
usarlo como atributo de un sensor cuyo estado es solo la marca de tiempo de la
última actualización.

Esto **no se puede crear desde la interfaz** — los sensores de plantilla con
`trigger:` solo existen en YAML a día de hoy; hay una petición abierta en la
comunidad de Home Assistant pidiendo justo esa capacidad para el editor
visual, sin resolver todavía. Para no mezclarlo con el resto de tu
`configuration.yaml`, va como **paquete**: un archivo propio,
`packages/enablebanking.yaml`, que puedes borrar entero el día que quieras
quitarlo, sin dejar nada suelto. Si nunca has usado paquetes en tu instancia:

1. Copia [`packages/enablebanking.yaml`](packages/enablebanking.yaml) (incluido junto a este README) a `/config/packages/enablebanking.yaml`.
2. En tu `configuration.yaml`, asegúrate de tener esto (si ya existe una clave `homeassistant:`, añade solo la línea `packages: ...` dentro):
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
3. Reinicia Home Assistant.

El archivo ya trae los seis sensores completos (los cinco periodos que
pediste más la tendencia mensual). Aquí va su contenido, por si quieres
entender o ajustar algo antes de copiarlo:

```yaml
template:
  - trigger:
      - platform: time_pattern
        minutes: "/15"
      - platform: homeassistant
        event: start
      - platform: event
        event_type: enablebanking_sync_finished
    variables:
      primer_dia_mes_actual: >-
        {{ now().replace(day=1).strftime('%Y-%m-%d') }}
      primer_dia_mes_anterior: >-
        {{ (now().replace(day=1) - timedelta(days=1)).replace(day=1).strftime('%Y-%m-%d') }}
      ultimo_dia_mes_anterior: >-
        {{ (now().replace(day=1) - timedelta(days=1)).strftime('%Y-%m-%d') }}
    action:
      - service: enablebanking.get_summary
        data:
          group_by: category
          date_from: "{{ primer_dia_mes_actual }}"
        response_variable: mes_actual
      - service: enablebanking.get_summary
        data:
          group_by: category
          date_from: "{{ primer_dia_mes_anterior }}"
          date_to: "{{ ultimo_dia_mes_anterior }}"
        response_variable: mes_anterior
      - service: enablebanking.get_summary
        data:
          group_by: category
          date_from: "{{ now().strftime('%Y-01-01') }}"
        response_variable: anio_actual
      - service: enablebanking.get_summary
        data:
          group_by: category
          date_from: "{{ (now().year - 1) }}-01-01"
          date_to: "{{ (now().year - 1) }}-12-31"
        response_variable: anio_anterior
      - service: enablebanking.get_summary
        data:
          group_by: category
        response_variable: historico
      - service: enablebanking.get_summary
        data:
          group_by: month
          date_from: "{{ now().strftime('%Y-01-01') }}"
        response_variable: tendencia_mensual
    sensor:
      - name: "Enable Banking - Mes actual"
        unique_id: enablebanking_resumen_mes_actual
        state: "{{ now().isoformat() }}"
        attributes:
          income: "{{ mes_actual.income }}"
          expense: "{{ mes_actual.expense }}"
          net: "{{ mes_actual.net }}"
          transaction_count: "{{ mes_actual.transaction_count }}"
          groups: "{{ mes_actual.groups | to_json }}"
      - name: "Enable Banking - Mes anterior"
        unique_id: enablebanking_resumen_mes_anterior
        state: "{{ now().isoformat() }}"
        attributes:
          income: "{{ mes_anterior.income }}"
          expense: "{{ mes_anterior.expense }}"
          net: "{{ mes_anterior.net }}"
          transaction_count: "{{ mes_anterior.transaction_count }}"
          groups: "{{ mes_anterior.groups | to_json }}"
      - name: "Enable Banking - Año actual"
        unique_id: enablebanking_resumen_anio_actual
        state: "{{ now().isoformat() }}"
        attributes:
          income: "{{ anio_actual.income }}"
          expense: "{{ anio_actual.expense }}"
          net: "{{ anio_actual.net }}"
          transaction_count: "{{ anio_actual.transaction_count }}"
          groups: "{{ anio_actual.groups | to_json }}"
      - name: "Enable Banking - Año anterior"
        unique_id: enablebanking_resumen_anio_anterior
        state: "{{ now().isoformat() }}"
        attributes:
          income: "{{ anio_anterior.income }}"
          expense: "{{ anio_anterior.expense }}"
          net: "{{ anio_anterior.net }}"
          transaction_count: "{{ anio_anterior.transaction_count }}"
          groups: "{{ anio_anterior.groups | to_json }}"
      - name: "Enable Banking - Histórico"
        unique_id: enablebanking_resumen_historico
        state: "{{ now().isoformat() }}"
        attributes:
          income: "{{ historico.income }}"
          expense: "{{ historico.expense }}"
          net: "{{ historico.net }}"
          transaction_count: "{{ historico.transaction_count }}"
          groups: "{{ historico.groups | to_json }}"
      - name: "Enable Banking - Tendencia mensual (año actual)"
        unique_id: enablebanking_tendencia_mensual
        state: "{{ now().isoformat() }}"
        attributes:
          groups: "{{ tendencia_mensual.groups | to_json }}"
```

Para limitarte a un solo banco en cualquiera de estas llamadas, añade
`account_iban: "TU_IBAN"` en el `data:` del servicio correspondiente.

Desde `enablebanking.get_summary`, el diccionario `groups` que devuelve sale ya
ordenado de forma útil: **cronológicamente** cuando agrupas por `month` o
`year` (de más antiguo a más reciente), y **de mayor a menor gasto** cuando
agrupas por `category`, `account` o `counterparty`. Esto importa porque las
llamadas se hacen por separado a cada banco conectado y se combinan después:
sin este orden, un gráfico podría salir con los meses o las categorías
mezclados según qué banco respondiera primero.

### Gráfico de tarta (gastos por categoría del mes actual)

Con [`plotly-graph-card`](https://github.com/dbuezas/lovelace-plotly-graph-card)
(HACS), que sabe generar tantas porciones como haga falta a partir de un
atributo, sin declarar cada categoría a mano:

```yaml
type: custom:plotly-graph
title: Gastos por categoría - mes actual
entities:
  - entity: sensor.enable_banking_mes_actual
    type: pie
    labels: |
      $ex Object.entries(hass.states["sensor.enable_banking_mes_actual"]
        ?.attributes?.groups || {})
        .filter(([, v]) => v.expense < 0)
        .map(([k]) => k)
    values: |
      $ex Object.entries(hass.states["sensor.enable_banking_mes_actual"]
        ?.attributes?.groups || {})
        .filter(([, v]) => v.expense < 0)
        .map(([, v]) => Math.abs(v.expense))
    textinfo: label+percent
    textposition: inside
    hovertemplate: |
      <b>%{label}</b><br>
      %{value:,.2f} €<br>
      %{percent}<extra></extra>
    sort: true
hours_to_show: 1
refresh_interval: auto
layout:
  height: 450
  margin:
    l: 10
    r: 10
    t: 20
    b: 20
  legend:
    orientation: h
    "y": -10
```

### Gráfico de barras (tendencia mensual)

Mismo mecanismo, cambiando `pie` por `bar` y leyendo el sensor de tendencia
mensual en vez del de un solo periodo:

```yaml
type: custom:plotly-graph
title: Neto por mes (año actual)
entities:
  - entity: sensor.enable_banking_tendencia_mensual_ano_actual
    type: bar
    x: >
      $ex
      Object.keys(hass.states["sensor.enable_banking_tendencia_mensual_ano_actual"]
        ?.attributes?.groups || {})
    "y": >
      $ex
      Object.values(hass.states["sensor.enable_banking_tendencia_mensual_ano_actual"]
        ?.attributes?.groups || {}).map(v => v.net)
hours_to_show: 8760
refresh_interval: auto
```

Para comparar años en vez de meses, duplica el bloque de `get_summary` en la
plantilla con `group_by: year` y sin `date_from` (para tener todo el
histórico repartido por año) y apunta el gráfico de barras a ese sensor.

### Tabla

Con [`flex-table-card`](https://github.com/custom-cards/flex-table-card)
(HACS), o simplemente una tarjeta Markdown:

```yaml
type: markdown
title: Gastos del mes actual por categoría
content: >
  | Categoría | Gasto |

  |:---|--:|

  {% for cat, datos in state_attr('sensor.enable_banking_mes_actual',
  'groups').items() -%}

  | {{ cat }} | {{ '%.2f'|format(datos.expense) }} € |

  {% endfor %}
```

## Sensores por cuenta (nativos, siempre disponibles sin configuración extra)

| Sensor | Qué muestra |
|---|---|
| **Saldo** | El saldo más significativo disponible (prioriza saldo disponible sobre contable), con todos los saldos como atributo. |
| **Último movimiento** | Importe con signo, contraparte, concepto, categoría y el movimiento completo como atributo. |
| **Número de movimientos** | Diagnóstico, desactivado por defecto. Total guardado y pendientes. |

## Categorización

- `/config/enablebanking/categories.yaml` se crea automáticamente con reglas de
  ejemplo (Alimentación, Restauración, Transporte, Vivienda, Suministros, Ocio,
  Salud, Nómina/Ingresos). Es un único archivo compartido por todos los bancos
  conectados, porque las reglas no dependen del banco. Edítalo y ejecuta
  `enablebanking.recategorize` para aplicarlo.
- Las categorías puestas a mano con `enablebanking.set_category` **nunca** se
  sobrescriben por las reglas, aunque vuelvas a ejecutar `recategorize`.

## Servicios

Todos aparecen en Herramientas para desarrolladores → Acciones, con nombre y
descripción de cada campo en español o inglés según el idioma de tu Home
Assistant. Los que consultan movimientos (`get_transactions`, `get_summary`,
`export`) combinan automáticamente **todos los bancos conectados**, salvo que
indiques `account_iban` para limitarte a uno.

| Servicio | Qué hace |
|---|---|
| **`enablebanking.sync_now`** (`force`) | Fuerza una consulta a todos los bancos, saltándose el calendario si `force: true` (pero no un bloqueo activo por límite del banco). |
| **`enablebanking.get_transactions`** | Movimientos filtrados por cuenta, fechas, ingreso/gasto, importe, texto, categoría, con paginación (`limit`/`offset`) y orden. |
| **`enablebanking.get_summary`** | Totales de ingresos, gastos y neto, agrupados por categoría, mes, año, cuenta o contraparte. El orden de `groups` es cronológico para `month`/`year` y de mayor a menor gasto para el resto. |
| **`enablebanking.export`** | Escribe los movimientos filtrados en `/config/enablebanking/exports/`, en CSV o JSON. |
| **`enablebanking.set_category`** | Asigna a mano la categoría de un movimiento (por su `id`, el que devuelve `get_transactions`). Si el mismo `id` numérico existe en dos bancos distintos, hay que indicar `account_iban` para desambiguar. |
| **`enablebanking.recategorize`** | Aplica `categories.yaml` a los movimientos guardados, sin tocar los categorizados a mano. |
| **`enablebanking.list_uncategorized`** | Contrapartes más frecuentes sin categoría, para ayudarte a ampliar `categories.yaml`. |

## Eventos

| Evento | Cuándo | Datos |
|---|---|---|
| **`enablebanking_new_transactions`** | Al terminar una sincronización que trajo movimientos nuevos. | `entry_id`, `bank`, `new_transactions`, `accounts` |
| **`enablebanking_sync_finished`** | Al terminar cualquier sincronización (haya traído novedades o no). | `entry_id`, `bank`, `status`, `reason` |

Ambos identifican de qué banco vienen (`entry_id`/`bank`), imprescindible si tienes
más de una integración conectada.

## Opciones (botón "Configurar" de cada integración)

Horas entre consultas automáticas (6–48h, por defecto 6). El tope diario de 4
consultas reales no es configurable desde la interfaz.

## Parámetros no visibles en la interfaz

Por diseño, algunos ajustes no se exponen como opción de usuario y solo se
cambian editando `custom_components/enablebanking/const.py`: tope de consultas
diarias, duración de los bloqueos tras un 429, días de solapamiento en la
sincronización incremental, si se borra la base de datos al eliminar la
integración (por defecto sí), y si se usan las cabeceras del navegador como
señal de "usuario presente" en la primera descarga.

## Limitaciones conocidas

- No hay panel visual para navegar y categorizar movimientos; de momento la vía
  es `get_transactions`/`get_summary` desde Herramientas para desarrolladores, o
  Excel con `export`. Planeado para una versión futura.
- Deliberadamente **no hay sensores nativos de Home Assistant para totales por
  periodo o desgloses por categoría**: se evaluaron (sensores en Python, uno por
  banco y por periodo) y se descartaron a favor del patrón de plantilla +
  servicio descrito en "Explotar los datos en Lovelace", que es más flexible
  (cualquier rango de fechas, no solo unos pocos fijos) y no obliga a mantener
  decenas de entidades.
- El código de error exacto que cada banco usa para "sesión caducada" no está
  confirmado más que para el formato general de Enable Banking
  (`{"error": "..."}`); un 401 sin código reconocible se trata, por prudencia,
  como caducidad.

## Registro de cambios

**v0.4.1**
- Corregido: movimientos duplicados en la base de datos local. La causa era
  que la deduplicación usaba como identificador principal `entry_reference` (o
  campos similares) tal cual los reporta el banco; algunos bancos (confirmado
  con un caso real) lo devuelven a `null` en la sincronización en la que el
  movimiento aparece por primera vez y lo rellenan días después para ese
  **mismo** movimiento. Al cambiar de `null` a un valor, el esquema de
  deduplicación "saltaba" a una clave distinta y el movimiento se insertaba
  por segunda vez en lugar de actualizarse.
  - Nueva huella de contenido (`fingerprint`) para reconocer que dos filas son
    el mismo movimiento real, calculada sobre fecha efectiva, importe
    normalizado (`"6"`, `"6.00"` y `6` producen la misma huella), moneda,
    signo (cargo/abono), contraparte, IBAN de la contraparte y concepto
    (`remittance`) normalizados (espacios colapsados, sin distinguir
    mayúsculas/minúsculas). Deliberadamente **no** incluye `balance_after`
    (puede faltar o variar entre consultas) ni las tres fechas en bruto
    (`booking_date`/`value_date`/`transaction_date`), sustituidas por la
    fecha efectiva ya resuelta.
  - Mecanismo de "promoción": si una fila ya guardada con clave de huella
    (`fp:...`) coincide en huella con un movimiento que llega después con
    `entry_reference` (o `reference_number`) ya informado, se reutiliza la
    clave de esa fila existente en lugar de crear una nueva; el `UPSERT`
    actualiza esa misma fila y rellena el identificador que llegó tarde, sin
    duplicar nada ni tocar la categoría que ya tuviera.
  - Migración de esquema (v1 → v2): añade la columna `fingerprint` y calcula
    su valor para todos los movimientos ya almacenados a partir de sus
    columnas existentes, de forma automática al actualizar la integración;
    no requiere ninguna acción manual ni pierde histórico.
  - Añadido, por el mismo motivo, `reference_number` como segundo
    identificador fuerte (igual que `entry_reference`) para bancos que usen
    ese campo en su lugar.
  - Cubierto con pruebas automatizadas que reproducen exactamente el caso
    reportado (mismo movimiento, `entry_reference` nulo y luego informado en
    dos sincronizaciones sucesivas) y casos de no regresión (movimientos
    distintos el mismo día, movimientos idénticos genuinos el mismo día,
    bases de datos creadas antes de esta versión).

**v0.4.0**
- Corregido: los movimientos nuevos no se categorizaban solos al descargarse.
  `categories.yaml` solo se aplicaba cuando se llamaba a mano al servicio
  `enablebanking.recategorize`; todo lo que entraba por sincronización
  automática se quedaba sin categoría hasta ese momento. Ahora cada
  movimiento se categoriza por reglas en el mismo instante en que se guarda
  por primera vez (incluida la descarga inicial completa al vincular una
  cuenta), sin tocar para nada los movimientos que ya tuvieran categoría
  puesta a mano o por una sincronización anterior. `recategorize` sigue
  existiendo, para volver a aplicar las reglas tras editar
  `categories.yaml` o para el histórico que ya tenías de antes de esta
  versión.

**v0.3.3**
- El IBAN de cada cuenta ahora se ve de un vistazo: aparece como "Modelo"
  (la línea bajo el nombre del dispositivo, tanto en la lista de dispositivos
  de la integración como al abrir la ficha del dispositivo) y también en el
  campo "Número de serie" de esa ficha, con espacios cada 4 caracteres para
  que se lea como lo imprime el propio banco.

**v0.3.2**
- Corregido: la plantilla del paquete llamaba a `.strftime()` sobre variables
  que ya habían pasado por `variables:`, y Home Assistant las convierte a
  texto en ese paso — provocaba `UndefinedError: 'str object' has no
  attribute 'strftime'`. El formateo se hace ahora dentro de la propia
  variable, de una sola vez.
- Corregido: los ejemplos de Lovelace de la sección "Explotar los datos en
  Lovelace" apuntaban a `sensor.enablebanking_resumen_mes_actual` y
  `sensor.enablebanking_tendencia_mensual`, que no son los `entity_id` reales
  (Home Assistant los genera a partir de `name:`, no de `unique_id`). Los
  ejemplos habrían mostrado tarjetas en blanco. Corregidos a
  `sensor.enable_banking_mes_actual` y
  `sensor.enable_banking_tendencia_mensual_ano_actual`.
- Ampliadas las reglas de categorización por defecto (`categories.py`): de 8 a
  13 categorías, con más palabras clave por categoría, y cuatro categorías
  nuevas (Compras, Hogar, Educación, Seguros, Finanzas).

**v0.3.1**
- El patrón de la sección "Explotar los datos en Lovelace" pasa de pegarse a
  mano en `configuration.yaml` a un **paquete** propio
  (`packages/enablebanking.yaml`, incluido en el ZIP), autocontenido y fácil
  de quitar. Sin cambios en el componente Python.

**v0.3.0**
- Añadido `group_by: year` a `enablebanking.get_summary`, para comparativas
  año a año sin tener que sumar doce llamadas agrupadas por mes.
- Corregido: el diccionario `groups` que devuelve `get_summary` salía en un
  orden arbitrario cuando combinaba varios bancos (dependía de cuál respondiera
  antes). Ahora sale cronológico para `month`/`year` y de mayor a menor gasto
  para el resto — importante para que un gráfico de barras o de tarta no salga
  desordenado.
- Evaluados y descartados los sensores nativos de periodo (mes/año actual y
  anterior, histórico) en favor del patrón de plantilla disparada por evento +
  `get_summary`, documentado a fondo en la nueva sección "Explotar los datos en
  Lovelace", con ejemplos de tarta y barras con `plotly-graph-card` y de tabla.

**v0.2.2**
- Corregido: los servicios de auditoría (`get_transactions`, `get_summary`,
  `export`, `set_category`, `recategorize`, `list_uncategorized`) solo miraban
  la base de datos de la primera integración cargada; con más de un banco
  conectado, devolvían resultados vacíos o del banco equivocado. Ahora combinan
  todos los bancos y admiten `account_iban` para limitarse a uno.
- Corregido: `enablebanking.sync_now` podía perder el resultado de un banco si
  dos integraciones tenían el mismo nombre (mismo banco, dos aplicaciones). Ahora
  se indexa por `entry_id`.
- Corregido: los eventos `enablebanking_new_transactions` y
  `enablebanking_sync_finished` no indicaban de qué banco venían.
- Corregido/completado: traducciones de todos los servicios y de los mensajes de
  error de validación (antes solo estaba traducido `get_transactions`, con un
  campo obsoleto). Añadido `strings.json` como referencia.

**v0.2.0 – v0.2.1**
- Reescritura a arquitectura local-first: base de datos SQLite propia, motor de
  sincronización independiente con calendario y tope diario de consultas,
  gestión de errores 401/429, deduplicación de movimientos, categorización con
  reglas YAML y corrección manual, servicios de auditoría y exportación.
- URL de redirección con HTTPS para producción (documentado el porqué).

**v0.1.0**
- Primera versión: config flow con autenticación en el navegador (sin copiar y
  pegar URLs), sensores de Saldo y Último movimiento por cuenta, servicio básico
  `get_transactions` sin base de datos local (leía directo del banco en cada
  sincronización), reautenticación automática al caducar el consentimiento.
