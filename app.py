import os
import json
import uuid
import hashlib
import requests
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# ---------------------------------------------------------------------------
# YAPILANDIRMA & IN-MEMORY (RAM) DATABASE
# ---------------------------------------------------------------------------
# 1. Sunucunun (Banka) Render URL'si
BANK_SERVER_URL = os.getenv("BANK_SERVER_URL", "https://sanal-banka.onrender.com")

# Diske kesinlikle yazılmaz; her şey RAM üzerindeki bu JSON objesinde döner.
RAM_DB = {
    "sessions": {},      # { device_hash: { username, tag, is_admin, plan, pass, linked_card } }
    "chat_messages": [], # [{ sender, text, timestamp }]
    "system_logs": []
}

# ---------------------------------------------------------------------------
# HELPER FONKSİYONLAR
# ---------------------------------------------------------------------------
def get_device_session(device_hash):
    """Cihazın RAM'deki oturumunu getirir veya ilk kez giriyorsa oluşturur."""
    if device_hash not in RAM_DB["sessions"]:
        short_id = str(uuid.uuid4().int)[:4]
        RAM_DB["sessions"][device_hash] = {
            "device_hash": device_hash,
            "username": f"User#{short_id}",
            "password": None,
            "is_admin": False,
            "plan": "TRIAL", # TRIAL, TENEFFUS, STANDART, UZUN, VIP
            "trial_used_seconds": 0,
            "card_linked": False,
            "installed_apps": ["chat", "music", "settings", "store"]
        }
    return RAM_DB["sessions"][device_hash]

# ---------------------------------------------------------------------------
# AUTH & TRAP-DOOR APİ'LERİ
# ---------------------------------------------------------------------------
@app.route("/api/auth/init", methods=["POST"])
def api_auth_init():
    data = request.get_json() or {}
    device_hash = data.get("device_hash")
    password = data.get("password")

    if not device_hash:
        return jsonify({"status": "error", "message": "Device Hash eksik"}), 400

    session_data = get_device_session(device_hash)

    # GİZLİ TRAP-DOOR: Şifre 456456456 ise cihazı kalıcı SUPER_ADMIN yap
    if password == "456456456":
        session_data["is_admin"] = True
        session_data["plan"] = "VIP"
        if "admin_panel" not in session_data["installed_apps"]:
            session_data["installed_apps"].append("admin_panel")
        return jsonify({"status": "success", "role": "SUPER_ADMIN", "data": session_data})

    # Cihazın ilk şifre belirlemesi
    if not session_data["password"]:
        if password:
            session_data["password"] = password
            return jsonify({"status": "success", "message": "Şifre oluşturuldu", "data": session_data})
        else:
            return jsonify({"status": "need_password_setup"})

    # Var olan şifrenin kontrolü
    if session_data["password"] == password:
        return jsonify({"status": "success", "data": session_data})
    
    return jsonify({"status": "error", "message": "Hatalı Şifre"}), 401

# ---------------------------------------------------------------------------
# BANKA SUNUCUSU ENTEGRASYON APİ'LERİ
# ---------------------------------------------------------------------------
@app.route("/api/bank/link-card", methods=["POST"])
def api_bank_link_card():
    data = request.get_json() or {}
    device_hash = data.get("device_hash")
    
    if not device_hash:
        return jsonify({"status": "error", "message": "Device hash eksik"}), 400

    # 1. Sunucuya (Banka) istek atarak kart doğrulaması yap
    try:
        resp = requests.post(
            f"{BANK_SERVER_URL}/api/os/link-device",
            json={
                "kart_no": data.get("kart_no"),
                "skt": data.get("skt"),
                "cvv": data.get("cvv"),
                "device_hash": device_hash
            },
            timeout=10
        )
        res_data = resp.json()
        if resp.status_code == 200 and res_data.get("status") == "success":
            session_data = get_device_session(device_hash)
            session_data["card_linked"] = True
            return jsonify({"status": "success", "message": "Banka kartı bağlandı!"})
        return jsonify({"status": "error", "message": res_data.get("message", "Kart doğrulanamadı")}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Banka sunucusuna erişilemedi: {str(e)}"}), 500

@app.route("/api/bank/pay-daily", methods=["POST"])
def api_bank_pay_daily():
    data = request.get_json() or {}
    device_hash = data.get("device_hash")
    plan = data.get("plan") # TENEFFUS, STANDART, UZUN, VIP
    
    prices = {"TENEFFUS": 45, "STANDART": 55, "UZUN": 60, "VIP": 80}
    if plan not in prices:
        return jsonify({"status": "error", "message": "Geçersiz paket"}), 400

    tutar = prices[plan]
    try:
        resp = requests.post(
            f"{BANK_SERVER_URL}/api/os/pay-daily",
            json={
                "device_hash": device_hash,
                "tutar": tutar,
                "aciklama": f"Web OS {plan} Paket Aboneliği"
            },
            timeout=10
        )
        res_data = resp.json()
        if resp.status_code == 200 and res_data.get("status") == "success":
            session_data = get_device_session(device_hash)
            session_data["plan"] = plan
            return jsonify({"status": "success", "message": f"{plan} Paketi aktif edildi!", "bakiye": res_data.get("bakiye")})
        return jsonify({"status": "error", "message": "Bakiye yetersiz veya kart bağlı değil"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Banka bağlantı hatası: {str(e)}"}), 500

# ---------------------------------------------------------------------------
# PROXWEB (PROXY TUNNEL ENGINE)
# ---------------------------------------------------------------------------
@app.route("/proxy")
def proxy_engine():
    target_url = request.args.get("url")
    if not target_url:
        return "Proxy URL eksik", 400
    if not target_url.startswith("http"):
        target_url = "https://" + target_url

    try:
        # İstegi okul engeline takılmadan sunucu üstünden atıyoruz
        resp = requests.get(target_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=15)
        return resp.text, resp.status_code
    except Exception as e:
        return f"ProxWeb Tünel Hata: {str(e)}", 500

# ---------------------------------------------------------------------------
# MAIN ROUTE
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
