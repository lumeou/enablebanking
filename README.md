# Enable Banking para Home Assistant (v0.2)

## Qué cambia respecto a v0.1
Ahora la integración **solo consulta al banco lo justo** y guarda todo en una base de
datos local (SQLite en `/config/enablebanking/<id>.db`). Sensores y servicios leen
siempre de esa copia local, nunca del banco directamente.

- **Sincronización automática:** cada 6 horas (configurable, mínimo 6h), y como máximo
  4 veces al día — el límite que suelen aplicar los bancos a las consultas sin ti
  delante. Si el banco devuelve "demasiadas peticiones", se pausa varias horas sola.
- **Primera descarga:** pide el histórico más largo posible (`strategy=longest`) justo
  al terminar de vincular la cuenta, aprovechando la ventana en que el banco te
  considera "presente".
- **Si caduca la autorización:** los sensores siguen mostrando los últimos datos
  guardados y Home Assistant te pide reautenticar (Ajustes → Dispositivos y servicios).
- **Al eliminar la integración:** se cierra el consentimiento en el banco **y se borra
  la base de datos local**. Para recuperar el histórico, vuelve a añadir la integración
  y vincula la cuenta de nuevo.

## Instalación
Igual que en v0.1: copia `custom_components/enablebanking` a `/config/custom_components/`,
la clave privada a `/config/enablebanking/private.key`, y registra en Enable Banking
la URL `https://<tu-dominio-local>:8123/enablebanking/callback` (HTTPS obligatorio en
producción). Reinicia Home Assistant y añade la integración.

## Categorías
Un archivo `/config/enablebanking/categories.yaml` (se crea automáticamente, con
ejemplos) define reglas por texto o expresión regular. Edítalo y llama al servicio
`enablebanking.recategorize` para aplicarlo. Las categorías puestas a mano con
`enablebanking.set_category` nunca se sobrescriben por las reglas.

## Servicios (Herramientas para desarrolladores → Acciones)
- **`enablebanking.sync_now`** (`force`: bool) — fuerza una consulta al banco, saltándose
  el calendario si `force: true` (pero no el bloqueo si el banco te ha limitado).
- **`enablebanking.get_transactions`** — filtra por cuenta (IBAN), fechas, ingreso/gasto,
  importe, texto y categoría, con paginación.
- **`enablebanking.get_summary`** — totales de ingresos, gastos y neto, agrupados por
  categoría, mes, cuenta o comercio.
- **`enablebanking.export`** — CSV o JSON a `/config/enablebanking/exports/`.
- **`enablebanking.set_category`** / **`enablebanking.recategorize`** /
  **`enablebanking.list_uncategorized`** — categorización manual y por reglas.

## Sensores por cuenta
**Saldo**, **Último movimiento** (con categoría y datos completos como atributo) y
**Número de movimientos** (desactivado por defecto, diagnóstico).
