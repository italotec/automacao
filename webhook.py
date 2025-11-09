import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from threading import Lock

import requests
from flask import Flask, request, jsonify

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
LINK_FILE = Path("link.txt")
RESPONDED_FILE = Path("respondidos.txt")

# Thread-safe file access
_file_lock = Lock()

# ----------------------------------------------------------------------
# Load bms.json
def load_bms() -> Dict[str, Any]:
    if not BMS_FILE.is_file():
        logger.warning("bms.json not found.")
        return {}
    try:
        data = json.loads(BMS_FILE.read_text(encoding="utf-8"))
        logger.info("Loaded %d BM entries", len(data))
        return data
    except Exception as exc:
        logger.exception("Failed to parse bms.json: %s", exc)
        return {}

bms: Dict[str, Any] = load_bms()


# ----------------------------------------------------------------------
# Load link.txt
def load_link() -> str:
    if not LINK_FILE.is_file():
        logger.warning("link.txt not found.")
        return ""
    try:
        link = LINK_FILE.read_text(encoding="utf-8").strip()
        if link:
            logger.info("Link loaded: %s", link)
        return link
    except Exception as exc:
        logger.exception("Failed to read link.txt: %s", exc)
        return ""

PAYMENT_LINK = load_link()


# ----------------------------------------------------------------------
# Responded users (wa_id)
def load_responded() -> set:
    if not RESPONDED_FILE.is_file():
        return set()
    try:
        lines = RESPONDED_FILE.read_text(encoding="utf-8").splitlines()
        return {line.strip() for line in lines if line.strip()}
    except Exception as exc:
        logger.exception("Failed to read respondidos.txt: %s", exc)
        return set()

def save_responded(wa_id: str) -> None:
    with _file_lock:
        try:
            with open(RESPONDED_FILE, "a", encoding="utf-8") as f:
                f.write(wa_id + "\n")
            logger.info("Saved wa_id to respondidos.txt: %s", wa_id)
        except Exception as exc:
            logger.error("Failed to write to respondidos.txt: %s", exc)


# ----------------------------------------------------------------------
# Find BM by phone_number_id
def find_bm_by_phone_number_id(phone_id: str) -> Optional[Dict[str, Any]]:
    for name, cfg in bms.items():
        if cfg.get("phone_number_id") == phone_id:
            return cfg
    return None


# ----------------------------------------------------------------------
# WhatsApp API
WHATSAPP_API = "https://graph.facebook.com/v20.0"


# ----------------------------------------------------------------------
# Upload banner.jpg
def upload_media(phone_number_id: str, token: str) -> Optional[str]:
    if not BANNER_PATH.is_file():
        logger.warning("banner.jpg not found.")
        return None
    url = f"{WHATSAPP_API}/{phone_number_id}/media"
    try:
        with open(BANNER_PATH, "rb") as f:
            files = {"file": ("banner.jpg", f, "image/jpeg")}
            data = {"type": "image/jpeg", "messaging_product": "whatsapp"}
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=15)
            resp.raise_for_status()
            media_id = resp.json().get("id")
            logger.info("Image uploaded – media_id=%s", media_id)
            return media_id
    except Exception as exc:
        logger.error("Failed to upload banner.jpg: %s", exc)
        return None


# ----------------------------------------------------------------------
# Send image
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
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        logger.info("Image sent to %s", to)
    except Exception as exc:
        logger.error("Failed to send image: %s", exc)


# ----------------------------------------------------------------------
# Send button
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
                "buttons": [
                    {"type": "reply", "reply": {"id": "btn_regularizar", "title": "REGULARIZAR"}}
                ]
            },
        },
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        logger.info("Button message sent to %s", to)
    except Exception as exc:
        logger.error("Failed to send button: %s", exc)


# ----------------------------------------------------------------------
# Send link
def send_link_message(phone_number_id: str, token: str, to: str, link: str) -> None:
    if not link:
        logger.warning("No link to send.")
        return
    url = f"{WHATSAPP_API}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": f"Acesse o portal para regularizar: {link}"}
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        logger.info("Link message sent to %s", to)
    except Exception as exc:
        logger.error("Failed to send link: %s", exc)


# ----------------------------------------------------------------------
# Send image + button (only once)
def send_image_and_reply_once(phone_number_id: str, token: str, to: str, body: str, wa_id: str) -> None:
    responded = load_responded()
    if wa_id in responded:
        logger.info("User %s already responded – skipping message.", wa_id)
        return

    media_id = upload_media(phone_number_id, token)
    if media_id:
        send_media_message(phone_number_id, token, to, media_id)
    send_interactive_button(phone_number_id, token, to, body)

    # Save only after successful send
    save_responded(wa_id)


# ----------------------------------------------------------------------
# Webhook GET
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verification succeeded.")
        return challenge, 200

    logger.warning("Verification failed – mode=%s token=%s", mode, token)
    return "Verification token mismatch", 403


# ----------------------------------------------------------------------
# Webhook POST
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

                # ---- incoming messages
                if "messages" in value:
                    for msg in value["messages"]:
                        wa_from = msg.get("from")
                        wa_id = value.get("contacts", [{}])[0].get("wa_id") or wa_from
                        msg_type = msg.get("type")
                        logger.info("[WA] Mensagem de %s tipo=%s", wa_from, msg_type)

                        phone_number_id = value.get("metadata", {}).get("phone_number_id")
                        if not phone_number_id:
                            logger.warning("phone_number_id missing")
                            continue

                        global bms
                        bms = load_bms()
                        bm_cfg = find_bm_by_phone_number_id(phone_number_id)
                        if not bm_cfg:
                            logger.error("BM not found for phone_number_id=%s", phone_number_id)
                            continue
                        token = bm_cfg.get("token")
                        if not token:
                            logger.error("Token missing in BM")
                            continue

                        # CASE 1: Text message → send image + button **ONLY ONCE**
                        if msg_type == "text":
                            send_image_and_reply_once(
                                phone_number_id=phone_number_id,
                                token=token,
                                to=wa_from,
                                body=REPLY_BODY,
                                wa_id=wa_id,
                            )
                            continue

                        # CASE 2: Button click → send link (even if already responded)
                        if msg_type == "interactive":
                            interactive = msg.get("interactive", {})
                            btn_reply = interactive.get("button_reply", {})
                            if (interactive.get("type") == "button_reply" and
                                btn_reply.get("id") == "btn_regularizar"):

                                current_link = load_link() or PAYMENT_LINK
                                send_link_message(
                                    phone_number_id=phone_number_id,
                                    token=token,
                                    to=wa_from,
                                    link=current_link,
                                )
                                continue

                # ---- status
                if "statuses" in value:
                    for st in value["statuses"]:
                        logger.info("[WA] Status message_id=%s status=%s", st.get("id"), st.get("status"))

    except Exception as exc:
        logger.exception("Erro ao processar WhatsApp: %s", exc)

    # ---- Instagram / Pages
    if data.get("object") in ("page", "instagram"):
        try:
            for entry in data.get("entry", []):
                for msg_event in entry.get("messaging", []):
                    sender = msg_event.get("sender", {}).get("id")
                    if "message" in msg_event:
                        text = msg_event["message"].get("text")
                        logger.info("[Messenger] sender=%s text=%s", sender, text)
                for change in entry.get("changes", []):
                    field = change.get("field")
                    logger.info("[IG/Page] field=%s change=%s", field, change.get("value"))
        except Exception as exc:
            logger.exception("Erro processando IG/Page: %s", exc)

    return jsonify({"status": "ok"}), 200


# ----------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5007))
    logger.info("Iniciando webhook na porta %s ...", port)
    app.run(host="0.0.0.0", port=port, debug=False)
