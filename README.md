# Enable Banking para Home Assistant (v0.1, sandbox)

## Instalar
1. **Clave privada**: copia tu `private.key` a `/config/enablebanking/private.key` del servidor de Home Assistant.
   - Accesible desde Windows con el complemento *Samba* (`\\homeassistant.local\config`) o con *File editor* / *SSH*.
   - **NUNCA** dentro de `/config/www` (esa carpeta es pública en `/local/`). El asistente rechaza esa ruta.
2. **Componente**: copia la carpeta `custom_components/enablebanking` dentro de `/config/custom_components/`
   (debe quedar `/config/custom_components/enablebanking/manifest.json`).
3. **URL de redirección**: en el panel de Enable Banking, la aplicación debe tener registrada EXACTAMENTE
   `http://homeassistant.local:8123/enablebanking/callback` (con `:8123`).
4. Reinicia Home Assistant (Ajustes → Sistema → Reiniciar).
5. Ajustes → Dispositivos y servicios → Añadir integración → **Enable Banking**.
   - ID de aplicación: el tuyo. Ruta de la clave: la de arriba. Banco: `Mock ASPSP`, país: `FI`.
   - Se abre el login del banco de pruebas; al terminar, Home Assistant continúa solo.

## Qué crea
Por cada cuenta: un dispositivo con los sensores **Saldo** y **Último movimiento**, y el servicio
`enablebanking.get_transactions` (Herramientas para desarrolladores → Acciones) que devuelve
saldos y movimientos completos con todos sus campos.

## Opciones (botón "Configurar" de la integración)
Horas entre consultas (por defecto 6) y días de movimientos (0 = lo que decida el banco).
