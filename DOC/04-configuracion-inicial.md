# Configuracion inicial

Al acceder por primera vez a OpenAce, se muestra un asistente de configuracion de 4 pasos.

## Asistente de configuracion

### Paso 1: EULA

Se presenta el Acuerdo de Licencia de Usuario Final. Para aceptarlo, escribe literalmente la frase:

```
He leido y acepto el acuerdo
```

La aceptacion queda registrada con marca temporal, IP de origen y hash SHA-256 de la frase.

### Paso 2: Usuarios

Crea al menos un usuario administrador. Campos:

| Campo | Requisitos |
|---|---|
| Nombre de usuario | Obligatorio, unico |
| Contrasena | Minimo 8 caracteres |
| Rol | `admin`, `user` o `viewer` |

Puedes crear varios usuarios en este paso. El primer usuario debe ser `admin`.

### Paso 3: Plugins

Configura las fuentes M3U de las que OpenAce obtendra los canales. Cada plugin tiene:

| Campo | Descripcion |
|---|---|
| Nombre | Nombre para mostrar del plugin |
| URL | URL de la lista M3U (HTTP/HTTPS/IPFS/IPNS) |
| Intervalo de refresco | Cada cuantos minutos se actualiza la lista (por defecto 60) |
| Habilitado | Si el plugin esta activo |

Tambien puedes subir un fichero M3U directamente en lugar de proporcionar una URL.

Este paso es opcional: puedes saltar la creacion de plugins y anadirlos despues desde la seccion de Plugins.

### Paso 4: Resumen

Muestra un resumen de la configuracion realizada:
- Estado de la EULA
- Usuarios creados y sus roles
- Plugins configurados con numero de canales

Al confirmar, se finaliza el setup y se redirige al login.

## Auto-setup por variables de entorno

Para entornos automatizados (CI/CD, scripts), puedes saltar el asistente por completo:

```yaml
environment:
  OPENACE_AUTO_SETUP: "true"
  OPENACE_ADMIN_USER: "admin"
  OPENACE_ADMIN_PASSWORD: "tu_password_segura"
  OPENACE_EULA_ACCEPT: "true"
```

Requisitos:
- `OPENACE_ADMIN_PASSWORD` es obligatoria
- `OPENACE_EULA_ACCEPT` debe ser `true`

Al arrancar, OpenAce automaticamente:
1. Acepta la EULA
2. Crea el usuario admin con la contrasena especificada
3. Marca el setup como completado
4. Inicia los plugins existentes

## Re-setup

Un administrador puede reiniciar el asistente de configuracion a traves de la API:

```bash
curl -X POST https://tu-dominio.com/api/setup/reset \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json"
```

El re-setup muestra los datos existentes (usuarios y plugins actuales) y permite modificarlos o saltar cada paso con la opcion "Saltar".

## API del setup

| Metodo | Ruta | Descripcion |
|---|---|---|
| `GET` | `/api/setup/status` | Estado actual del setup |
| `POST` | `/api/setup/eula` | Aceptar EULA |
| `POST` | `/api/setup/users` | Crear usuarios |
| `POST` | `/api/setup/plugins` | Crear plugins |
| `POST` | `/api/setup/finalize` | Finalizar setup |
| `POST` | `/api/setup/complete` | Setup completo en una sola llamada |
| `POST` | `/api/setup/reset` | Reiniciar setup (requiere admin) |

## Despues del setup

Una vez completado el setup, OpenAce redirige al login. Inicia sesion con el usuario admin que acabas de crear y accederas al Dashboard.

### Jerarquia de roles

| Rol | Permisos |
|---|---|
| `admin` | Acceso completo: usuarios, plugins, configuracion |
| `user` | Panel, checker, plugins (lectura), playlists |
| `viewer` | Solo reproduccion de streams y playlists M3U |

### Autenticacion

OpenAce soporta multiples metodos de autenticacion:

- **Cookie de sesion**: Login web normal, duracion configurable con `SESSION_DURATION_HOURS` (por defecto 24h)
- **Bearer token**: Para integracion con reproductores y APIs externas, se crea desde el panel de admin
- **Token en URL**: `?token=<valor>` como parametro de consulta, util para reproductores que no soportan cabeceras HTTP
- **HTTP Basic Auth**: Usuario y contrasena en la cabecera Authorization

## Siguientes pasos

- [Modulos](05-modulos.md) para entender cada seccion de la interfaz
- [Reproductores](06-reproductores.md) para configurar clientes de streaming
