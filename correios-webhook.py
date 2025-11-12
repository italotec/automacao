import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from threading import Lock
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string

import requests

# ----------------------------------------------------------------------
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "dojunio")

# File paths
BMS_FILE = Path("bms.json")
BANNER_PATH = Path("banner.jpg")
LINK_FILE = Path("linkcorreios.txt")
RESPONDED_FILE = Path("respondidos.txt")
MESSAGES_FILE = Path("messages.json")

_file_lock = Lock()

# ----------------------------------------------------------------------
# Load bms.json
def load_bms() -> Dict[str, Any]:
    if not BMS_FILE.is_file():
        logger.warning("bms.json not found.")
        return {}
    try:
        data = json.loads(BMS_FILE.read_text(encoding="utf-8"))
        return data
    except Exception as exc:
        logger.exception("Failed to parse bms.json: %s", exc)
        return {}

bms: Dict[str, Any] = load_bms()

# ----------------------------------------------------------------------
# Load link.txt
def load_link() -> str:
    if not LINK_FILE.is_file():
        return ""
    try:
        link = LINK_FILE.read_text(encoding="utf-8").strip()
        return link
    except Exception as exc:
        logger.exception("Failed to read link.txt: %s", exc)
        return ""

PAYMENT_LINK = load_link()

# ----------------------------------------------------------------------
# Responded users
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
        except Exception as exc:
            logger.error("Failed to write respondidos.txt: %s", exc)

# ----------------------------------------------------------------------
# Message storage
def load_messages() -> Dict:
    if not MESSAGES_FILE.is_file():
        return {}
    try:
        return json.loads(MESSAGES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_message(phone_number_id: str, wa_id: str, name: str,
                 message: str, msg_id: str, timestamp: int):
    """
    Store a user message.
    Key = f"{phone_number_id}_{wa_id}"
    """
    with _file_lock:
        data = load_messages()

        # -------------------------------------------------
        # 1. Build the *correct* key
        # -------------------------------------------------
        correct_key = f"{phone_number_id}_{wa_id}"

        # -------------------------------------------------
        # 2. If an old key exists (wrong phone_number_id) → move it
        # -------------------------------------------------
        for old_key in list(data.keys()):
            if old_key.endswith(f"_{wa_id}") and old_key != correct_key:
                # Same user, different phone_number_id → merge
                old_chat = data.pop(old_key)
                # Keep the newer name if needed
                if correct_key not in data:
                    data[correct_key] = old_chat
                    data[correct_key]["phone_number_id"] = phone_number_id
                else:
                    # Merge messages
                    data[correct_key]["messages"].extend(old_chat["messages"])
                logger.info("Merged old key %s → %s", old_key, correct_key)

        # -------------------------------------------------
        # 3. Initialise the chat entry if missing
        # -------------------------------------------------
        if correct_key not in data:
            data[correct_key] = {
                "phone_number_id": phone_number_id,
                "wa_id": wa_id,
                "name": name,
                "messages": []
            }

        # -------------------------------------------------
        # 4. Append the new message (avoid duplicates)
        # -------------------------------------------------
        if not any(m["id"] == msg_id for m in data[correct_key]["messages"]):
            data[correct_key]["messages"].append({
                "id": msg_id,
                "text": message,
                "timestamp": timestamp,
                "from_user": True
            })

        # -------------------------------------------------
        # 5. Write back
        # -------------------------------------------------
        try:
            MESSAGES_FILE.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        except Exception as exc:
            logger.error("Failed to write messages.json: %s", exc)

# ----------------------------------------------------------------------
# Find BM
def find_bm_by_phone_number_id(phone_id: str) -> Optional[Dict[str, Any]]:
    for name, cfg in bms.items():
        if cfg.get("phone_number_id") == phone_id:
            return cfg
    return None

# ----------------------------------------------------------------------
# WhatsApp API
WHATSAPP_API = "https://graph.facebook.com/v20.0"

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
    except Exception as exc:
        logger.error("Upload failed: %s", exc)
        return None

def send_media_message(phone_number_id: str, token: str, to: str, media_id: str) -> None:
    url = f"{WHATSAPP_API}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {"id": media_id},
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10).raise_for_status()
    except Exception as exc:
        logger.error("Send image failed: %s", exc)

def send_interactive_button(phone_number_id: str, token: str, to: str, body: str) -> None:
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
            },
        },
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10).raise_for_status()
    except Exception as exc:
        logger.error("Send button failed: %s", exc)

def send_link_message(phone_number_id: str, token: str, to: str, link: str) -> None:
    if not link:
        return
    url = f"{WHATSAPP_API}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": f"Aqui está o link: {link}"}
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10).raise_for_status()
    except Exception as exc:
        logger.error("Send link failed: %s", exc)

def send_image_and_reply_once(phone_number_id: str, token: str, to: str, body: str, wa_id: str) -> None:
    if wa_id in load_responded():
        return
    media_id = upload_media(phone_number_id, token)
    if media_id:
        send_media_message(phone_number_id, token, to, media_id)
    send_interactive_button(phone_number_id, token, to, body)
    save_responded(wa_id)

# ----------------------------------------------------------------------
# Webhook GET
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

# ----------------------------------------------------------------------
REPLY_BODY = """HOJE É O ÚLTIMO DIA PARA REGULARIZAR O SEU PACOTE
Recebemos sua encomenda em nosso Centro Logístico.
Para que possamos liberar o envio e garantir a entrega em até 3 dias úteis, é necessário regularizar a situação.
O valor para a liberação é de R$ 57,15.
Para dar continuidade, clique no botão abaixo:
"REGULARIZAR"
Assim que finalizar, por favor, me envie o comprovante. Estarei à disposição para ajudar."""

@app.route("/webhook", methods=["POST"])
def receive():
    data = request.get_json(silent=True) or {}
    logger.info("Evento recebido: %s", data)

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                if not phone_number_id:
                    continue

                global bms
                bms = load_bms()
                bm_cfg = find_bm_by_phone_number_id(phone_number_id)
                if not bm_cfg:
                    continue
                token = bm_cfg.get("token")
                if not token:
                    continue

                if "messages" in value:
                    for msg in value["messages"]:
                        wa_from = msg.get("from")
                        msg_type = msg.get("type")
                        msg_id = msg.get("id")
                        timestamp = int(msg.get("timestamp", 0))

                        # Get contact name
                        contact = value.get("contacts", [{}])[0]
                        name = contact.get("profile", {}).get("name", "Usuário")
                        wa_id = contact.get("wa_id") or wa_from

                        if msg_type == "text":
                            body = msg["text"]["body"]
                            # Save message
                            save_message(phone_number_id, wa_id, name, body, msg_id, timestamp)
                            # Send reply once
                            send_image_and_reply_once(phone_number_id, token, wa_from, REPLY_BODY, wa_id)

                        elif msg_type == "interactive":
                            interactive = msg.get("interactive", {})
                            if (interactive.get("type") == "button_reply" and
                                interactive.get("button_reply", {}).get("id") == "btn_regularizar"):
                                link = load_link() or PAYMENT_LINK
                                send_link_message(phone_number_id, token, wa_from, link)

                if "statuses" in value:
                    for st in value["statuses"]:
                        logger.info("Status: %s", st.get("status"))
    except Exception as exc:
        logger.exception("Erro: %s", exc)

    return jsonify({"status": "ok"}), 200

# ----------------------------------------------------------------------
# /chat UI
HTML_TEMPLATE = """
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
        .bm-item, .chat-item {
            padding: 12px; border-bottom: 1px solid #eee; cursor: pointer; display: flex; align-items: center;
            transition: background 0.2s;
        }
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

        // Optional: Refresh only when on chat list (not in conversation)
        setInterval(async () => {
            if (currentBM && !currentChat) {
                // Only refresh chat list
                showChats(currentBM.name, currentBM.phoneId);
            } else if (currentChat) {
                // Refresh current conversation
                const messages = await fetchMessages();
                const key = `${currentChat.phone_number_id}_${currentChat.wa_id}`;
                const updated = messages[key];
                if (updated && updated.messages.length > currentChat.messages.length) {
                    showConversation(updated);
                }
            }
        }, 8000); // Refresh every 8 seconds

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
    messages = load_messages()
    return render_template_string(HTML_TEMPLATE, bms=bms, messages=messages)

# ----------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    logger.info("Iniciando na porta %s ...", port)
    app.run(host="0.0.0.0", port=port, debug=False)
