# Reproductores recomendados

OpenAce genera listas M3U compatibles con cualquier reproductor IPTV. Cada plugin proporciona dos formatos de playlist:

- **MPEG-TS**: `http://<host>:8888/<plugin>/mpegts.m3u` — URLs OpenAce `/play/mpegts/...`; por defecto proxya directo desde AceStream sin exponer el puerto del motor
- **HLS**: `http://<host>:8888/<plugin>/hls.m3u` — Segmentos HLS bajo demanda via FFmpeg; requiere `OPENACE_FFMPEG_ENABLED=true`

## Autenticacion en reproductores

La mayoria de reproductores no soportan login con cookie. Usa un **token API** (creado desde `/admin/users`) de una de estas formas:

```
# Parametro en URL (mas compatible)
http://<host>:8888/<plugin>/mpegts.m3u?token=<tu-token>

# HTTP Basic Auth (si el reproductor lo soporta)
http://usuario:password@<host>:8888/<plugin>/mpegts.m3u
```

## Reproductores moviles

### TiviMate (Android) — Recomendado

El mejor reproductor IPTV para Android. Interfaz moderna, EPG, favoritos y multiples listas.

**Configuracion:**
1. Ajustes > Playlists > Anadir playlist
2. Selecciona "M3U playlist"
3. URL: `http://<host>:8888/<plugin>/mpegts.m3u?token=<tu-token>`
4. Nombre: el que quieras
5. Intervalo de actualizacion: cada 4 horas

**Formato recomendado:** MPEG-TS (menor latencia)

### OTT Navigator (Android)

Alternativa gratuita con buen soporte de listas M3U.

**Configuracion:**
1. Ajustes > Proveedores > Nuevo proveedor
2. Tipo: M3U
3. URL: `http://<host>:8888/<plugin>/mpegts.m3u?token=<tu-token>`

### IPTV Smarters Pro (Android / iOS)

Reproductor multiplataforma.

**Configuracion:**
1. Anadir usuario > Cargar lista de reproduccion o archivo/URL
2. Selecciona "M3U URL"
3. URL: `http://<host>:8888/<plugin>/mpegts.m3u?token=<tu-token>`

### Televizo (Android)

Reproductor ligero con Material Design.

**Configuracion:**
1. Anadir lista > URL
2. URL: `http://<host>:8888/<plugin>/mpegts.m3u?token=<tu-token>`

## Reproductores de escritorio

### VLC Media Player (Windows / macOS / Linux)

Reproductor universal. Soporta MPEG-TS y HLS de forma nativa.

**Para un canal individual:**
1. Multimedia > Abrir ubicacion de red
2. URL: `http://<host>:8888/play/mpegts/<infohash>?token=<tu-token>`

**Para una lista completa:**
1. Multimedia > Abrir ubicacion de red
2. URL: `http://<host>:8888/<plugin>/mpegts.m3u?token=<tu-token>`

**Formato recomendado:** MPEG-TS

### Kodi (Windows / macOS / Linux / Android)

Centro multimedia completo con soporte IPTV via add-on.

**Configuracion:**
1. Instala el add-on "PVR IPTV Simple Client"
2. Configuracion del add-on > General
3. Ubicacion: URL remota
4. URL de la lista M3U: `http://<host>:8888/<plugin>/mpegts.m3u?token=<tu-token>`
5. Modo de cache: lista M3U cacheada en almacenamiento local
6. Reinicia Kodi

**Formato recomendado:** MPEG-TS

### Jellyfin (auto-hospedado)

Servidor multimedia que puede importar canales IPTV.

**Configuracion:**
1. Dashboard > TV en directo > Anade un proveedor de guia de TV
2. Tipo: M3U
3. URL: `http://<host>:8888/<plugin>/mpegts.m3u?token=<tu-token>`

## Reproductores para Smart TV

### Smart IPTV (Samsung / LG)

1. Accede a la web de Smart IPTV y busca tu TV por MAC
2. Sube la URL de la playlist M3U

### SS IPTV (Samsung / LG)

1. Ajustes > Contenido > Listas externas
2. Anade la URL M3U con token

### IPTV Smarters (Smart TV)

Misma configuracion que la version movil.

## Comparativa de formatos

| Caracteristica | MPEG-TS | HLS |
|---|---|---|
| Latencia | Baja (2-5s) | Media (10-15s) |
| Compatibilidad | Muy alta | Universal |
| Uso de CPU en servidor | Bajo (FFmpeg `-c copy`) | Bajo/medio (FFmpeg `-c copy` + segmentacion) |
| Cambio de canal | Rapido | Lento (arranque FFmpeg) |
| Navegadores web | No | Si |
| Robustez ante cortes | Menor | Mayor (segmentos con buffer) |

**Recomendacion general:** Usa **MPEG-TS** para reproductores dedicados (TiviMate, Kodi, VLC). Usa **HLS** solo si necesitas compatibilidad con navegadores web o si tienes problemas de estabilidad con MPEG-TS.

## Notas sobre rendimiento

- Con `OPENACE_FFMPEG_ENABLED=false` (default), MPEG-TS no arranca FFmpeg: OpenAce mantiene la autenticacion/token y retransmite el stream directo del motor.
- Con `OPENACE_FFMPEG_ENABLED=true`, cada stream MPEG-TS/HLS activo consume ancho de banda y un proceso FFmpeg con codec copy.
- HLS anade coste de segmentacion y mas latencia inicial.
- Los procesos FFmpeg inactivos se terminan automaticamente tras `OPENACE_IDLE_TIMEOUT_S` segundos sin peticiones (180 por defecto).
- El numero maximo de streams simultaneos se controla con `OPENACE_MAX_STREAMS` (32 por defecto).
- HLS identifica clientes con `hls_client` y propaga el `token` a los segmentos automaticamente.

## Solucion de problemas

### El reproductor pide autenticacion

Asegurate de incluir el token en la URL: `?token=<tu-token>`

### El stream se corta frecuentemente

- Verifica la conexion de red del servidor
- Si usas VPN, prueba diferentes servidores/paises
- Comprueba el numero de peers en `/peers` — pocos peers indican un canal con poca disponibilidad
- Usa MPEG-TS en lugar de HLS para menor latencia

### Error 401 / No autenticado

- El token ha expirado o ha sido revocado
- Genera un nuevo token desde `/admin/users`
- Verifica que el token pertenece a un usuario activo

### El canal aparece como "caido" en el checker

- Puede ser temporal — los canales AceStream dependen de la disponibilidad de seeders en la red P2P
- Recomprueba mas tarde o prueba reproducirlo directamente (a veces el checker tiene timeout pero el canal funciona)
