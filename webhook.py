# app.py
import os
import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, Optional
from threading import Lock
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, redirect, url_for
import requests

# ----------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "dojunio")
WHATSAPP_API = "https://graph.facebook.com/v23.0"

# Arquivos
BMS_FILE = Path("bms.json")
BANNER_PATH = Path("bannercorreios.jpg")
LINK_FILE = Path("linkcorreios.txt")
RESPONDED_FILE = Path("respondidos.txt")
MESSAGES_FILE = Path("messages.json")
_file_lock = Lock()

# ----------------------------------------------------------------------
# Carregar bms.json
def load_bms() -> Dict[str, Any]:
    if not BMS_FILE.is_file():
        logger.warning("bms.json não encontrado.")
        return {}
    try:
        data = json.loads(BMS_FILE.read_text(encoding="utf-8"))
        for cfg in data.values():
            cfg.setdefault("waba_id", None)
            cfg.setdefault("status", "active")
            cfg.setdefault("quality_rating", "GREEN")
            cfg.setdefault("messaging_limit", 2000)
            cfg.setdefault("last_update", "Nunca")
            cfg.setdefault("ban_info", None)
            cfg.setdefault("templates", [])
        return data
    except Exception as e:
        logger.exception("Erro ao ler bms.json: %s", e)
        return {}

def save_bms(data: Dict):
    with _file_lock:
        try:
            BMS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error("Erro ao salvar bms.json: %s", e)

bms = load_bms()

# ----------------------------------------------------------------------
# Link de pagamento
def load_link() -> str:
    if not LINK_FILE.is_file():
        return ""
    try:
        return LINK_FILE.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.exception("Erro ao ler linkcorreios.txt: %s", e)
        return ""
PAYMENT_LINK = load_link()

# ----------------------------------------------------------------------
# Respondidos
def load_responded() -> set:
    if not RESPONDED_FILE.is_file():
        return set()
    try:
        lines = RESPONDED_FILE.read_text(encoding="utf-8").splitlines()
        return {line.strip() for line in lines if line.strip()}
    except Exception:
        return set()

def save_responded(wa_id: str) -> None:
    with _file_lock:
        try:
            with open(RESPONDED_FILE, "a", encoding="utf-8") as f:
                f.write(wa_id + "\n")
        except Exception as e:
            logger.error("Erro ao salvar respondidos.txt: %s", e)

# ----------------------------------------------------------------------
# Mensagens
def load_messages() -> Dict:
    if not MESSAGES_FILE.is_file():
        return {}
    try:
        return json.loads(MESSAGES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_message(phone_number_id: str, wa_id: str, name: str, message: str, msg_id: str, timestamp: int):
    with _file_lock:
        data = load_messages()
        key = f"{phone_number_id}_{wa_id}"
        for old_key in list(data.keys()):
            if old_key.endswith(f"_{wa_id}") and old_key != key:
                old_chat = data.pop(old_key)
                if key not in data:
                    data[key] = old_chat
                    data[key]["phone_number_id"] = phone_number_id
                else:
                    data[key]["messages"].extend(old_chat["messages"])
                logger.info("Mesclado: %s → %s", old_key, key)

        if key not in data:
            data[key] = {"phone_number_id": phone_number_id, "wa_id": wa_id, "name": name, "messages": []}

        if not any(m["id"] == msg_id for m in data[key]["messages"]):
            data[key]["messages"].append({
                "id": msg_id,
                "text": message,
                "timestamp": timestamp,
                "from_user": True
            })

        try:
            MESSAGES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error("Erro ao salvar messages.json: %s", e)

# ----------------------------------------------------------------------
# Encontrar BM
def find_bm_by_phone_number_id(phone_id: str) -> Optional[Dict[str, Any]]:
    for cfg in bms.values():
        if cfg.get("phone_number_id") == phone_id:
            return cfg
    return None

# ----------------------------------------------------------------------
# WhatsApp API
def upload_media(phone_number_id: str, token: str) -> Optional[str]:
    if not BANNER_PATH.is_file():
        return None
    url = f"{WHATSAPP_API}/{phone_number_id}/media"
    try:
        with open(BANNER_PATH, "rb") as f:
            files = {"file": ("banner.jpg", f, "image/jpeg")}
            data = {"type": "image/jpeg", "messaging_product": "whatsapp"}
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=15)
            resp.raise_for_status()
            return resp.json().get("id")
    except Exception as e:
        logger.error("Upload falhou: %s", e)
        return None

def send_media_message(phone_number_id: str, token: str, to: str, media_id: str):
    url = f"{WHATSAPP_API}/{phone_number_id}/messages"
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"id": media_id}}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10).raise_for_status()
    except Exception as e:
        logger.error("Envio de imagem falhou: %s", e)

def send_interactive_button(phone_number_id: str, token: str, to: str, body: str):
    url = f"{WHATSAPP_API}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [{"type": "reply", "reply": {"id": "btn_regularizar", "title": "REGULARIZAR"}}]
            }
        }
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10).raise_for_status()
    except Exception as e:
        logger.error("Botão falhou: %s", e)

def send_link_message(phone_number_id: str, token: str, to: str, link: str):
    if not link:
        return
    url = f"{WHATSAPP_API}/{phone_number_id}/messages"
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": f"Aqui está o link: {link}"}}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10).raise_for_status()
    except Exception as e:
        logger.error("Link falhou: %s", e)

def send_image_and_reply_once(phone_number_id: str, token: str, to: str, body: str, wa_id: str):
    if wa_id in load_responded():
        return
    media_id = upload_media(phone_number_id, token)
    if media_id:
        send_media_message(phone_number_id, token, to, media_id)
    send_interactive_button(phone_number_id, token, to, body)
    save_responded(wa_id)

# ----------------------------------------------------------------------
REPLY_BODY = """HOJE É O ÚLTIMO DIA PARA REGULARIZAR O SEU PACOTE
Recebemos sua encomenda em nosso Centro Logístico.
Para que possamos liberar o envio e garantir a entrega em até 3 dias úteis, é necessário regularizar a situação.
O valor para a liberação é de R$ 57,15.
Para dar continuidade, clique no botão abaixo:
"REGULARIZAR"
Assim que finalizar, por favor, me envie o comprovante. Estarei à disposição para ajudar."""

# ----------------------------------------------------------------------
# Webhook: Verificação
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

# ----------------------------------------------------------------------
# Webhook: Mensagens + Atualizações
@app.route("/webhook", methods=["POST"])
def webhook():
    global bms
    data = request.get_json(silent=True) or {}
    logger.info("Webhook: %s", json.dumps(data, ensure_ascii=False))

    try:
        for entry in data.get("entry", []):
            waba_id = entry.get("id")
            for change in entry.get("changes", []):
                field = change.get("field")
                value = change.get("value", {})

                # === MENSAGENS ===
                if "messages" in value:
                    phone_number_id = value.get("metadata", {}).get("phone_number_id")
                    if not phone_number_id:
                        continue
                    bm_cfg = find_bm_by_phone_number_id(phone_number_id)
                    if not bm_cfg:
                        continue
                    token = bm_cfg.get("token")
                    if not token:
                        continue

                    for msg in value["messages"]:
                        wa_from = msg.get("from")
                        msg_type = msg.get("type")
                        msg_id = msg.get("id")
                        timestamp = int(msg.get("timestamp", 0))
                        contact = value.get("contacts", [{}])[0]
                        name = contact.get("profile", {}).get("name", "Usuário")
                        wa_id = contact.get("wa_id") or wa_from

                        if msg_type == "text":
                            body = msg["text"]["body"]
                            save_message(phone_number_id, wa_id, name, body, msg_id, timestamp)
                            send_image_and_reply_once(phone_number_id, token, wa_from, REPLY_BODY, wa_id)
                        elif msg_type == "interactive":
                            interactive = msg.get("interactive", {})
                            if (interactive.get("type") == "button_reply" and
                                interactive.get("button_reply", {}).get("id") == "btn_regularizar"):
                                link = load_link() or PAYMENT_LINK
                                send_link_message(phone_number_id, token, wa_from, link)

                # === account_alerts ===
                if field == "account_alerts":
                    alert = value.get("alert_info", {})
                    alert_type = alert.get("alert_type", "")
                    description = alert.get("alert_description", "")

                    for bm_id, cfg in bms.items():
                        if str(cfg.get("waba_id")) == str(waba_id):
                            now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
                            cfg["last_update"] = now

                            if "DISABLED" in alert_type or "BAN" in alert_type:
                                cfg["status"] = "banned"
                                cfg["ban_info"] = {"reason": description.split(".")[0], "date": now}
                            elif "RESTRICTED" in alert_type or "LIMIT" in alert_type:
                                cfg["status"] = "restricted"
                                m = re.search(r"(\d+)", description)
                                if m:
                                    cfg["messaging_limit"] = int(m.group(1))
                            elif "QUALITY" in alert_type:
                                cfg["quality_rating"] = "YELLOW" if "YELLOW" in description else "RED" if "RED" in description else "GREEN"
                            break

                # === account_update ===
                elif field == "account_update":
                    event = value.get("event")
                    phone_number = value.get("phone_number")

                    for bm_id, cfg in bms.items():
                        if (cfg.get("phone_number_id") == phone_number or str(cfg.get("waba_id")) == str(waba_id)):
                            now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
                            cfg["last_update"] = now

                            if event == "DISABLED_UPDATE":
                                ban_state = value.get("ban_info", {}).get("waba_ban_state")
                                ban_date = value.get("ban_info", {}).get("waba_ban_date", now)
                                if ban_state == "BANNED":
                                    cfg["status"] = "banned"
                                    cfg["ban_info"] = {"reason": "Conta desativada", "date": ban_date}
                                elif ban_state == "REINSTATE":
                                    cfg["status"] = "active"
                                    cfg["ban_info"] = None

                            elif event == "RESTRICTION":
                                cfg["status"] = "restricted"
                                tier = value.get("restriction_info", {}).get("restricted_messaging_tier", "0")
                                cfg["messaging_limit"] = int(tier.replace("K", "000").replace("M", "000000"))

                            elif event == "ACCOUNT_VIOLATION":
                                cfg["status"] = "flagged"
                                cfg["ban_info"] = {"reason": value.get("violation_info", {}).get("violation_type", "Desconhecido")}
                            break

        save_bms(bms)

    except Exception as e:
        logger.exception("Erro no webhook: %s", e)

    return jsonify({"status": "ok"}), 200

# ----------------------------------------------------------------------
# Adicionar BM
@app.route("/add-bm", methods=["POST"])
def add_bm():
    global bms
    bm_name = request.form.get("bm_name")
    token = request.form.get("token")
    phone_id = request.form.get("phone_id")
    waba_id = request.form.get("waba_id")

    if not all([bm_name, token, phone_id, waba_id]):
        return "Erro: todos os campos são obrigatórios", 400

    if bm_name in bms:
        return "Erro: BM já existe", 400

    bms[bm_name] = {
        "phone_number_id": phone_id,
        "token": token,
        "waba_id": waba_id,
        "status": "active",
        "quality_rating": "GREEN",
        "messaging_limit": 2000,
        "last_update": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
        "ban_info": None,
        "templates": []
    }

    save_bms(bms)
    logger.info("BM %s adicionado!", bm_name)
    return redirect(url_for("panel"))

# ----------------------------------------------------------------------
# Painel de Status
HTML_PANEL = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel de Status - WhatsApp BMs</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Segoe UI', sans-serif; }
        body { background: #f4f6f9; color: #333; }
        .container { max-width: 1200px; margin: 16px auto; padding: 16px; }
        h1 { text-align: center; margin-bottom: 20px; color: #075e54; }
        .add-btn { display: block; margin: 0 auto 20px; padding: 12px 24px; background: #25d366; color: white; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; }
        .add-btn:hover { background: #1da851; }
        .table-container { overflow-x: auto; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); background: white; }
        table { width: 100%; min-width: 800px; border-collapse: collapse; }
        th, td { padding: 12px 10px; text-align: left; border-bottom: 1px solid #eee; font-size: 0.9rem; }
        th { background: #075e54; color: white; position: sticky; top: 0; z-index: 10; }
        .status { padding: 5px 10px; border-radius: 20px; font-weight: bold; font-size: 0.75rem; min-width: 70px; text-align: center; }
        .active { background: #d4edda; color: #155724; }
        .restricted { background: #fff3cd; color: #856404; }
        .banned { background: #f8d7da; color: #721c24; }
        .flagged { background: #f1c40f; color: #7f5a00; }
        .quality.green { color: #28a745; }
        .quality.yellow { color: #ffc107; }
        .quality.red { color: #dc3545; }
        .ban-info { font-size: 0.75rem; }
        .ban-info strong { color: #721c24; }
        .refresh { text-align: center; margin-top: 20px; }
        .refresh button { padding: 12px 24px; background: #075e54; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; }
        .refresh button:hover { background: #063f38; }

        /* Modal */
        .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); justify-content: center; align-items: center; }
        .modal-content { background: white; padding: 24px; border-radius: 12px; width: 90%; max-width: 500px; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
        .modal-header { display: flex; justify-content: space-between; margin-bottom: 16px; }
        .modal-header h2 { color: #075e54; }
        .close { font-size: 1.5rem; cursor: pointer; color: #aaa; }
        .close:hover { color: #000; }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; margin-bottom: 6px; font-weight: 600; }
        .form-group input { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 6px; }
        .modal-footer { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }
        .btn { padding: 10px 20px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; }
        .btn-primary { background: #25d366; color: white; }
        .btn-secondary { background: #ddd; color: #333; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Painel de Status dos BMs</h1>
        <button class="add-btn" onclick="openModal()">Adicionar BM</button>

        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th>BM</th>
                        <th>Número</th>
                        <th>Status</th>
                        <th>Qualidade</th>
                        <th>Limite</th>
                        <th>Atualização</th>
                        <th>Ban</th>
                    </tr>
                </thead>
                <tbody>
                    {% for bm_id, cfg in bms.items() %}
                    <tr>
                        <td><strong>{{ bm_id }}</strong></td>
                        <td>{{ cfg.phone_number_id }}</td>
                        <td><span class="status {{ cfg.status }}">{{ {'active':'Ativo','restricted':'Restrito','banned':'Desativado','flagged':'Sinalizado'}.get(cfg.status,'Ativo') }}</span></td>
                        <td class="quality {{ cfg.quality_rating.lower() }}">{{ cfg.quality_rating }}</td>
                        <td>{{ cfg.messaging_limit }}</td>
                        <td style="white-space: nowrap;">{{ cfg.last_update }}</td>
                        <td>{% if cfg.ban_info %}<div class="ban-info"><strong>{{ cfg.ban_info.reason }}</strong><br><small>{{ cfg.ban_info.date[:10] }}</small></div>{% else %}—{% endif %}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="refresh">
            <button onclick="location.reload()">Atualizar Agora</button>
        </div>
    </div>

    <!-- Modal -->
    <div id="addModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Adicionar Novo BM</h2>
                <span class="close" onclick="closeModal()">×</span>
            </div>
            <form action="/add-bm" method="POST">
                <div class="form-group">
                    <label for="bm_name">Nome do BM (ex: 178)</label>
                    <input type="text" id="bm_name" name="bm_name" required placeholder="178">
                </div>
                <div class="form-group">
                    <label for="token">Token</label>
                    <input type="text" id="token" name="token" required placeholder="EAA...">
                </div>
                <div class="form-group">
                    <label for="phone_id">Phone Number ID</label>
                    <input type="text" id="phone_id" name="phone_id" required placeholder="894871697035812">
                </div>
                <div class="form-group">
                    <label for="waba_id">WABA ID</label>
                    <input type="text" id="waba_id" name="waba_id" required placeholder="102290129340398">
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancelar</button>
                    <button type="submit" class="btn btn-primary">Adicionar</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        const modal = document.getElementById('addModal');
        function openModal() { modal.style.display = 'flex'; }
        function closeModal() { modal.style.display = 'none'; }
        window.onclick = e => { if (e.target === modal) closeModal(); }
    </script>
</body>
</html>
"""

@app.route("/")
def panel():
    return render_template_string(HTML_PANEL, bms=bms)

# ----------------------------------------------------------------------
# Chat UI
HTML_CHAT = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Chat Hub</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
        body { background: #f0f2f5; color: #1c1e21; }
        .container { display: flex; height: 100vh; }
        .sidebar { width: 320px; background: white; border-right: 1px solid #ddd; overflow-y: auto; }
        .main { flex: 1; display: flex; flex-direction: column; background: #e5ddd5; }
        .header { padding: 16px; background: #075e54; color: white; font-weight: bold; }
        .bm-list, .chat-list { padding: 8px; }
        .bm-item, .chat-item { padding: 12px; border-bottom: 1px solid #eee; cursor: pointer; display: flex; align-items: center; }
        .bm-item:hover, .chat-item:hover { background: #f5f5f5; }
        .bm-name { font-weight: 600; }
        .chat-name { font-weight: 500; flex: 1; }
        .chat-preview { font-size: 0.85em; color: #666; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 180px; }
        .chat-time { font-size: 0.75em; color: #999; }
        .messages { flex: 1; padding: 16px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
        .msg { max-width: 70%; padding: 8px 12px; border-radius: 8px; line-height: 1.4; }
        .msg.user { background: #dcf8c6; align-self: flex-end; }
        .msg.bot { background: white; align-self: flex-start; }
        .msg-time { font-size: 0.7em; color: #999; margin-top: 4px; }
        .empty { text-align: center; color: #666; margin-top: 50px; }
        .back-btn { background: none; border: none; color: white; font-size: 1.2em; cursor: pointer; margin-right: 10px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="sidebar" id="sidebar">
            <div class="header">Selecione um BM</div>
            <div class="bm-list" id="bm-list"></div>
        </div>
        <div class="main" id="main" style="display: none;">
            <div class="header">
                <button class="back-btn" onclick="goBack()">←</button>
                <span id="chat-title"></span>
            </div>
            <div class="messages" id="messages"></div>
        </div>
    </div>
    <script>
        const bms = {{ bms | tojson }};
        let currentBM = null;
        let currentChat = null;
        function renderBMs() {
            const list = document.getElementById('bm-list');
            list.innerHTML = '';
            Object.keys(bms).forEach(name => {
                const div = document.createElement('div');
                div.className = 'bm-item';
                div.innerHTML = `<div class="bm-name">${name}</div>`;
                div.onclick = () => showChats(name, bms[name].phone_number_id);
                list.appendChild(div);
            });
        }
        async function fetchMessages() {
            try {
                const resp = await fetch('/chat-data');
                const data = await resp.json();
                return data.messages;
            } catch (e) {
                console.error("Failed to fetch messages:", e);
                return {};
            }
        }
        async function showChats(bmName, phoneId) {
            currentBM = { name: bmName, phoneId };
            document.getElementById('sidebar').style.display = 'none';
            document.getElementById('main').style.display = 'flex';
            document.getElementById('chat-title').textContent = bmName;
            const messages = await fetchMessages();
            const chats = Object.values(messages).filter(m => m.phone_number_id === phoneId);
            const unique = {};
            chats.forEach(chat => {
                if (!unique[chat.wa_id]) unique[chat.wa_id] = chat;
            });
            const chatList = document.createElement('div');
            chatList.className = 'chat-list';
            if (Object.keys(unique).length === 0) {
                chatList.innerHTML = '<div class="empty">Nenhuma conversa ainda.</div>';
            } else {
                Object.values(unique).forEach(chat => {
                    const lastMsg = chat.messages[chat.messages.length - 1];
                    const time = new Date(lastMsg.timestamp * 1000).toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit'});
                    const div = document.createElement('div');
                    div.className = 'chat-item';
                    div.innerHTML = `
                        <div class="chat-name">${chat.name}</div>
                        <div class="chat-preview">${lastMsg.text}</div>
                        <div class="chat-time">${time}</div>
                    `;
                    div.onclick = () => showConversation(chat);
                    chatList.appendChild(div);
                });
            }
            const messagesDiv = document.getElementById('messages');
            messagesDiv.innerHTML = '';
            messagesDiv.appendChild(chatList);
        }
        async function showConversation(chat) {
            currentChat = chat;
            const container = document.getElementById('messages');
            container.innerHTML = '';
            document.getElementById('chat-title').textContent = chat.name;
            chat.messages.forEach(msg => {
                const div = document.createElement('div');
                div.className = `msg ${msg.from_user ? 'user' : 'bot'}`;
                div.innerHTML = `
                    <div>${msg.text}</div>
                    <div class="msg-time">${new Date(msg.timestamp * 1000).toLocaleString('pt-BR')}</div>
                `;
                container.appendChild(div);
            });
            container.scrollTop = container.scrollHeight;
        }
        function goBack() {
            currentBM = null;
            currentChat = null;
            document.getElementById('sidebar').style.display = 'block';
            document.getElementById('main').style.display = 'none';
            renderBMs();
        }
        setInterval(async () => {
            if (currentBM && !currentChat) {
                showChats(currentBM.name, currentBM.phoneId);
            } else if (currentChat) {
                const messages = await fetchMessages();
                const key = `${currentChat.phone_number_id}_${currentChat.wa_id}`;
                const updated = messages[key];
                if (updated && updated.messages.length > currentChat.messages.length) {
                    showConversation(updated);
                }
            }
        }, 8000);
        renderBMs();
    </script>
</body>
</html>
"""

@app.route("/chat-data")
def chat_data():
    return jsonify({"messages": load_messages()})

@app.route("/chat")
def chat_hub():
    return render_template_string(HTML_CHAT, bms=bms)

# ----------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info("Servidor iniciado na porta %s", port)
    app.run(host="0.0.0.0", port=port, debug=False)
