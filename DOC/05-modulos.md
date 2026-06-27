# Modulos

Descripcion de cada modulo de la interfaz de OpenAce.

## Dashboard (`/panel`)

Pagina principal tras iniciar sesion. Muestra tarjetas de acceso rapido a cada modulo:

- **Peers & Estado** — Monitorizacion en tiempo real
- **Channel Checker** — Verificacion de canales
- **EULA** — Acuerdo de licencia
- **Plugins** — Gestion de fuentes M3U
- **Usuarios** — Gestion de usuarios y tokens (solo visible para admins)

El dashboard detecta automaticamente el rol del usuario y solo muestra las tarjetas accesibles.

---

## Peers & Estado (`/peers`)

Panel de monitorizacion en tiempo real del sistema. Se actualiza automaticamente cada 5 segundos.

### Reproduciendo ahora

Tabla de streams activos en ese momento:
- **Canal**: Nombre del canal (resuelto desde los plugins)
- **Content ID**: Infohash de AceStream
- **Formato**: MPEG-TS o HLS
- **Clientes**: Numero de clientes conectados a ese stream
- **Tiempo**: Duracion desde que se inicio el stream

### Motor AceStream

- **Estado**: Online/Offline con indicador visual
- **Version**: Version del motor AceStream
- **Endpoint**: Direccion interna del motor (127.0.0.1:6878)

### Resumen

Contadores de:
- Clientes conectados al proxy (puerto 8888)
- Conexiones al motor AceStream
- Peers P2P activos
- Conexiones externas salientes
- Streams activos MPEG-TS/HLS (procesos FFmpeg)
- Plugins cargados

### Peers P2P

Tabla detallada de todas las conexiones P2P del motor AceStream:
- **Estado**: ESTABLISHED, TIME_WAIT, CLOSE_WAIT, etc.
- **Local / Remote**: Direcciones IP y puertos
- **Org / ISP**: Organizacion del peer (via ipinfo.io)
- **Ciudad / Pais / Timezone / Coordenadas**: Geolocalizacion del peer
- **Bajada / Subida**: Velocidad estimada desde `ss -tin` (B/s, KB/s, MB/s)

La geolocalizacion usa cache y limita nuevas consultas por refresco para evitar abusar de ipinfo.io.

Las columnas son ordenables haciendo clic en las cabeceras.

En la barra superior se muestra la velocidad total agregada de bajada y subida.

### Red

Muestra la IP publica del servidor (consultada a ipinfo.io) con:
- IP, hostname, ciudad, pais, organizacion/ISP, zona horaria

### Plugins

Tabla de plugins cargados con:
- Nombre y slug
- Numero de canales
- Intervalo de refresco
- Tiempo desde el ultimo refresco

### Streams activos

Procesos FFmpeg activos para MPEG-TS y HLS:
- Content ID
- PID del proceso
- Estado (vivo/muerto)
- Clientes conectados
- IPs de clientes cuando estan disponibles
- Tiempo de inactividad (se terminan automaticamente tras `OPENACE_IDLE_TIMEOUT_S`, 180 segundos por defecto)

---

## Channel Checker (`/check`)

Herramienta para verificar la disponibilidad de los canales AceStream.

### Comprobacion manual

Pega un ID de AceStream en cualquiera de estos formatos:
- Infohash directo: `a1b2c3d4e5f6...`
- URL acestream: `acestream://a1b2c3d4e5f6...`
- URL con parametro: `http://...?id=a1b2c3d4e5f6...`

Se comprobara si el stream esta disponible y se mostrara:
- Estado (vivo, caido, timeout, error)
- Tiempo de respuesta en milisegundos
- Numero de peers

### Comprobacion masiva

Comprueba todos los canales de los plugins cargados de forma secuencial. Al abrir `/check` y al iniciar una comprobacion masiva, el catalogo se sincroniza con los plugins, purga resultados obsoletos y deduplica canales por infohash. Se puede filtrar por:
- **Plugin**: Solo canales de un plugin concreto
- **Grupo**: Solo canales de un grupo (categoria)
- **Estado**: Solo canales no comprobados, vivos, caidos, timeout o error

El monitor de progreso muestra en tiempo real:
- Barra de progreso
- Canal que se esta comprobando actualmente
- Contadores de vivos, caidos, timeout, error y saltados

La comprobacion se puede detener en cualquier momento.

### Resultados

Tabla con todos los canales y su estado:
- **Canal**: Nombre del canal
- **Grupo**: Categoria/grupo del canal
- **Plugin**: Plugin de origen
- **ID**: Infohash de AceStream
- **Estado**: Vivo, caido, timeout, error, o sin comprobar
- **Respuesta**: Tiempo de respuesta
- **Ultima comprobacion**: Hace cuanto se comprobo

La tabla es filtrable con el buscador (por nombre, grupo o ID) y ordenable por cualquier columna.

Cada fila tiene botones de accion:
- **Comprobar**: Recomprobar ese canal individual
- **MPEG-TS / HLS**: Copiar al portapapeles el enlace de reproduccion en formato MPEG-TS o HLS

---

## EULA (`/eula`)

Acuerdo de Licencia de Usuario Final. Es obligatorio aceptarlo para usar OpenAce.

### Contenido

El EULA incluye 9 clausulas:

1. **Objeto**: Descripcion del servicio
2. **Aceptacion**: Como se formaliza el consentimiento
3. **Contenido de terceros**: Exencion de responsabilidad sobre contenidos P2P
4. **Tratamiento de datos**: Datos almacenados (IP, User-Agent, hash SHA-256 de la frase)
5. **Marco legal europeo**: RGPD, Directiva 2019/790/UE, Directiva 2000/31/CE, LSSI-CE, LPI
6. **Ausencia de garantias**: Servicio proporcionado "tal cual"
7. **Limitacion de responsabilidad**
8. **Modificaciones del EULA**
9. **Resolucion**

### Aceptar

Para aceptar, escribe literalmente la frase: `He leido y acepto el acuerdo`

La aceptacion se registra con:
- Marca temporal
- IP de origen
- Hash SHA-256 de la frase
- User-Agent del navegador

Todos los datos se almacenan localmente en la base de datos SQLite. No se transmiten a servicios externos.

### Revocar

Una vez aceptado, aparece la opcion "Revocar consentimiento" al pie de la pagina. La revocacion requiere rol `admin`. Al revocar, se pierde el acceso a la aplicacion hasta una nueva aceptacion.

### Estado

Si la EULA esta aceptada, se muestra el identificador de consentimiento y la fecha de aceptacion.

---

## Plugins (`/plugins`)

Gestion de fuentes M3U que proporcionan los canales a OpenAce.

### Crear un plugin

Haz clic en "+ Nuevo Plugin" y rellena:

| Campo | Descripcion |
|---|---|
| Nombre | Nombre para mostrar |
| URL de la lista M3U | URL HTTP/HTTPS, o enlace IPFS/IPNS |
| Subir archivo | Alternativa: sube un fichero .m3u / .m3u8 directamente |
| Refresco (min) | Cada cuantos minutos se actualiza la lista (por defecto 60) |
| Habilitado | Si el plugin esta activo |

El slug se genera automaticamente a partir del nombre.

Las fuentes remotas M3U tienen protecciones operativas: solo HTTP/HTTPS, bloqueo de loopback/link-local, sin redirects, limite de 50 MB, validacion cacheada y soporte de `ETag`/`Last-Modified` para evitar descargas innecesarias.

### Tarjeta de plugin

Cada plugin se muestra como una tarjeta con:
- **Estado**: ok (verde), error (rojo), pendiente (amarillo), deshabilitado (gris)
- **Canales**: Numero de canales cargados
- **Intervalo**: Frecuencia de refresco
- **Ultimo refresco**: Hace cuanto se actualizo
- **Error**: Si hay un error, se muestra un extracto

### URLs de playlist

Cada plugin genera dos URLs de playlist M3U:
- **MPEG-TS**: `http://<host>/<slug>/mpegts.m3u`
- **HLS**: `http://<host>/<slug>/hls.m3u`

Usa el boton "copiar" para copiar la URL al portapapeles.

### Acciones

- **Canales**: Muestra la tabla de canales del plugin (nombre, infohash, grupo, logo, TVG-ID) con buscador
- **Refrescar**: Fuerza una actualizacion inmediata de la lista M3U. Si ya hay un refresco en curso, la API devuelve 409.
- **Exportar**: Descarga el plugin como JSON (definicion + canales)
- **Editar**: Modifica nombre, URL, intervalo o estado
- **Eliminar**: Borra el plugin y sus canales

### Importar / Exportar

- **Exportar todo**: Descarga todos los plugins como un unico fichero JSON
- **Importar JSON**: Importa plugins desde un fichero JSON exportado previamente. No sobreescribe plugins existentes con el mismo slug.

---

## Usuarios (`/admin/users`)

Panel de administracion de usuarios y tokens API. Solo accesible para el rol `admin`.

### Gestion de usuarios

Tabla con todos los usuarios:
- **Usuario**: Nombre de usuario
- **Rol**: admin, user o viewer
- **Estado**: Activo o deshabilitado
- **Creado**: Fecha de creacion
- **Ultimo acceso**: Ultimo login
- **Acciones**: Editar o eliminar

**Crear usuario**: Boton "+ Nuevo usuario" con campos para nombre, contrasena (minimo 8 caracteres), rol y expiracion opcional.

**Editar usuario**: Permite cambiar nombre, rol, contrasena (dejar vacia para no cambiarla), estado y expiracion. Un usuario expirado invalida sesiones, tokens y Basic Auth.

**Eliminar usuario**: No puedes eliminarte a ti mismo.

### Tokens API

Tokens de autenticacion para integrar OpenAce con reproductores (TiviMate, Kodi, VLC, etc.) sin necesidad de login con cookie.

Tabla de tokens:
- **Token**: Preview del token (solo se muestran los primeros caracteres)
- **Usuario**: A que usuario pertenece
- **Descripcion**: Texto libre (ej: "TiviMate", "Kodi salon")
- **Creado / Expira**: Fechas de creacion y expiracion
- **Acciones**: Revocar

**Generar token**: Selecciona un usuario, anade una descripcion y se genera un token. El token solo se muestra una vez al crearlo; copialo inmediatamente.

**Uso del token**: Se puede usar de tres formas:
- Cabecera HTTP: `Authorization: Bearer <token>`
- Parametro URL: `?token=<token>`
- HTTP Basic Auth: `usuario:contrasena`

### Jerarquia de roles

| Rol | Panel | Checker | Plugins | Usuarios | Streaming |
|---|---|---|---|---|---|
| admin | Si | Si | Lectura + escritura | Si | Si |
| user | Si | Si | Solo lectura | No | Si |
| viewer | No | No | No | No | Si |

---

## Siguientes pasos

- [Reproductores](06-reproductores.md) para configurar clientes de streaming
- [API HTTP](08-api-referencia.md) para referencia de endpoints
- [Solucion de problemas](10-solucion-de-problemas.md) si algo no funciona
