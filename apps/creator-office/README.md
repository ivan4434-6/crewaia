# Creator Office AI

Una oficina de agentes para crear vídeo: ocho especialistas coordinados con
[CrewAI](../../README.md) investigan, escriben el guion, eligen los clips, exportan las piezas,
componen las portadas, preparan las publicaciones, clasifican los comentarios y analizan los
resultados. Tú revisas y apruebas cada paso desde una oficina en pixel art que muestra, en
directo, lo que cada agente está haciendo de verdad.

La interfaz está en español. El código y sus comentarios, en inglés, como el resto del repositorio.

## Qué hace hoy y qué necesita credenciales

La aplicación funciona entera sin ninguna credencial, en **modo demostración**. Lo que depende
de un servicio externo aparece como **«Pendiente de conexión»** y dice qué falta; nunca se
simula un resultado.

| Función | Sin configurar | Para conectarla |
| --- | --- | --- |
| Agentes (redacción y criterio) | Motor local de demostración: los agentes usan sus herramientas reales y dejan `[huecos entre corchetes]` donde escribiría un modelo. Sin tokens ni coste. | `LLM_MODEL` y `LLM_API_KEY`, o el formulario de Integraciones (solo propietarios). |
| Vídeo: análisis, cortes, exportación 9:16 / 16:9 / 1:1, subtítulos incrustados | Funciona. Necesita FFmpeg: lo trae el extra `media`. | — |
| Portadas y miniaturas con el kit de marca | Funciona. | — |
| Transcripción automática | Pendiente de conexión. Alternativa: subir un `.srt` o `.vtt`, o dejar que Clipper trabaje solo con señales de audio e imagen. | `TRANSCRIPTION_MODEL`, `TRANSCRIPTION_API_KEY` (un servicio compatible con `/audio/transcriptions` de OpenAI que devuelva marcas de tiempo) |
| Publicar y programar en YouTube / Shorts | Pendiente de conexión. Alternativa: paquete manual (vídeo, portada y textos en un zip) y «Ya la publiqué…». | `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` |
| Publicar y programar en TikTok | Igual. | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` |
| Publicar y programar en Instagram Reels | Igual. | `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` y un `APP_BASE_URL` público con https |
| Comentarios | Importación a mano (pegar texto). | Cuenta de YouTube o Instagram conectada. TikTok no ofrece lectura de comentarios a terceros: siempre a mano. |
| Métricas | Se añaden a mano y quedan marcadas como tales. | Cuenta conectada. Lo que una plataforma no da por API se indica, no se estima. |

Las credenciales de plataforma son las de una aplicación OAuth que registras tú en la consola
de desarrolladores de cada una. La dirección de retorno que hay que dar de alta es
`{APP_BASE_URL}/api/integrations/{youtube|tiktok|instagram}/callback`.

YouTube guarda las publicaciones programadas en la propia plataforma. TikTok e Instagram no
ofrecen eso por API: es esta aplicación la que sube el vídeo a la hora indicada, así que el
servidor tiene que estar en marcha en ese momento.

> **No verificado contra los servicios reales.** En el entorno donde se escribió esto no había
> credenciales. Los conectores de YouTube, TikTok e Instagram y la transcripción están probados
> con respuestas HTTP simuladas, y el camino con proveedor de IA, con un modelo de pruebas:
> ninguno contra el servicio real. Direcciones, permisos y límites están escritos según la
> documentación pública de cada plataforma y pueden haber cambiado: compruébalos antes de
> confiar en ellos. Las plataformas suelen exigir además una revisión de tu aplicación antes de
> permitir publicaciones públicas.

## Reglas que la aplicación hace cumplir

- **Nada se publica solo.** Cada publicación necesita dos decisiones de una persona: aprobar el
  contenido y confirmar el envío. Los agentes solo preparan borradores.
- **La aprobación cubre lo que se aprobó.** Si después cambian el texto, el vídeo o la portada,
  la aprobación se retira. Una publicación programada no deja cambiar su vídeo ni su portada
  hasta que se desprograma, y el envío vuelve a comprobarlo justo antes de subir nada.
- **«Publicado» solo lo dice la plataforma o tú.** El estado indica siempre si lo confirmó la
  API o si lo declaraste a mano.
- **Solo APIs oficiales.** No hay automatización de navegadores ni lectura de páginas privadas.
- **Las fuentes se leen de verdad.** Scout solo cita lo que pudo descargar, respeta `robots.txt`
  y nunca accede a direcciones privadas; un dato sin fuente leída se descarta.
- **Las cifras no se inventan.** Analyst trabaja con estadísticas calculadas; una cifra que no
  figura en ellas se marca. Lo que falta se dice que falta.
- **Derechos.** No se toca un vídeo hasta que declaras tener derechos sobre él.
- **Todo queda registrado**: quién hizo qué y cuándo, sea una persona, un agente o una plataforma.

## Instalación

Requisitos: Python 3.11–3.13 y Node.js 20 o 22 (probado con Python 3.11 y Node.js 22).

La aplicación usa el CrewAI de este mismo repositorio, así que se instala desde el código:

```bash
cd apps/creator-office/backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../../../lib/crewai-core -e ../../../lib/cli -e ../../../lib/crewai
pip install -e ".[media,dev]"      # media: incluye un FFmpeg estático; dev: pytest y ruff

cd ../frontend
npm install
npm run build                       # genera frontend/dist, que sirve el backend

cd ../backend
creator-office                      # http://localhost:8000
```

Abre `http://localhost:8000`, crea una cuenta (cada cuenta nueva estrena su propio espacio de
trabajo y es su propietaria) y crea un proyecto. Los datos se guardan en
`apps/creator-office/data/` (base de datos SQLite, archivos y una clave de desarrollo), que Git
ignora.

Para configurarla, copia `.env.example` a `.env` en esta carpeta. Todas las variables están
explicadas allí; una variable vacía cuenta como no definida, y las del entorno mandan sobre
las del archivo.

### En producción

- `APP_ENV=production` y un `APP_SECRET_KEY` propio (el servidor no arranca sin él). Con esa
  clave se cifran los tokens y las claves guardadas: si la pierdes, habrá que volver a conectar
  las cuentas.
- `APP_BASE_URL` con la dirección pública real, detrás de https.
- `ALLOW_REGISTRATION=false` si solo deben entrar personas invitadas.
- Copia de seguridad de `DATA_DIR` (o de la base de datos que indiques en `DATABASE_URL`).

### Desarrollo

Con el entorno virtual activado, en dos terminales:

```bash
cd backend && creator-office        # API en el puerto 8000
cd frontend && npm run dev          # interfaz en el 5173; reenvía /api al backend
```

Fuera de producción, la documentación de la API está en `/api/docs`.

### Pruebas

```bash
cd backend && ruff check src tests && python -m pytest
cd frontend && npm test && npm run build
```

Las pruebas del backend recorren la aplicación por su API HTTP con el motor de demostración y
un FFmpeg real, y simulan las plataformas con respuestas HTTP guionizadas. No leen tu `.env`.

## Cómo está hecha

```
backend/src/creator_office/
  pipeline/    plantillas, pasos y agentes; motor de estados; cola de trabajos; revisión previa
  agents/      tripulación de CrewAI, herramientas de cada agente y motor de demostración
  media/       FFmpeg, subtítulos, selección de clips, composición de portadas, fuentes web
  connectors/  YouTube, TikTok, Instagram y los requisitos de cada plataforma
  services/    marca, comunidad, analítica, producción y publicación
  api/         rutas HTTP; todo lo que ve el navegador sale de serializers.py
frontend/src/  React + TypeScript (Vite); una página por sección en pages/
```

- Cada paso del proyecto lo ejecuta un agente de CrewAI con herramientas propias. El resultado
  se guarda con versiones y espera tu aprobación antes de pasar al siguiente.
- La oficina se actualiza por eventos enviados por el servidor (SSE): no hace falta recargar.
- Cada espacio de trabajo está aislado. Hay tres roles: propietario, editor y lector.
- Las contraseñas se guardan con scrypt; la sesión va en una cookie `HttpOnly` y las peticiones
  que cambian algo llevan un token CSRF; los tokens de las plataformas y las claves de IA se
  guardan cifrados; los archivos son privados y solo se sirven con sesión o con un enlace firmado.

## Créditos y licencias

- Código de esta aplicación: licencia MIT, como el repositorio. CrewAI: MIT © 2025 crewAI, Inc.
  (ver [`LICENSE`](../../LICENSE)).
- Tipografía de la interfaz: **Jersey 15**, © 2023 The Soft Type Project Authors
  (<https://github.com/scfried/soft-type-jersey>), SIL Open Font License 1.1, distribuida a
  través de `@fontsource/jersey-15`.
- Tipografías para portadas y subtítulos (Anton, Archivo Black, Bebas Neue, Inter, Poppins y
  Space Grotesk): SIL Open Font License 1.1. Cada una va con su licencia en
  `backend/src/creator_office/assets/fonts/OFL-*.txt`.
- La exportación de vídeo usa FFmpeg. El extra `media` instala `imageio-ffmpeg`, que trae un
  binario de FFmpeg con su propia licencia; si prefieres el tuyo, indica su ruta en `FFMPEG_PATH`.
