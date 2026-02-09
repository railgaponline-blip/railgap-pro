import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import time
import os
import json
import sqlite3
import datetime
import razorpay
import hashlib
import hmac
import certifi
import re
import requests # IMPORT REQUESTS FIRST
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# ==========================================
# 0. CRITICAL: FORCE DISABLE SSL (THE FIX)
# ==========================================
# This disables SSL verification globally for the entire app
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# We monkey-patch the requests library to NEVER verify SSL
# This fixes the "SSLError" you are seeing on your laptop
def new_request(*args, **kwargs):
    kwargs['verify'] = False # Force False
    return requests.api.request.__defaults__[0](*args, **kwargs) if hasattr(requests.api.request, '__defaults__') else requests.api.request(*args, **kwargs)

# Override all possible request methods
os.environ['CURL_CA_BUNDLE'] = ""
requests.session().verify = False

# The Nuclear Option: Override the internal request method of the library
original_request = requests.sessions.Session.request
def patched_request(self, method, url, *args, **kwargs):
    kwargs['verify'] = False
    return original_request(self, method, url, *args, **kwargs)
requests.sessions.Session.request = patched_request

# ==========================================
# 1. SETUP & CONFIG
# ==========================================
st.set_page_config(
    page_title="RailGap Pro",
    page_icon="🚄",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# ⚠️ REAL KEYS REQUIRED (Ensure these are TEST keys for localhost)
RZP_KEY_ID = "rzp_test_SDIZyfIgzj9y8k"
RZP_KEY_SECRET = "a8BfGHBFk81I5KVPnjB1jLT7"
try: client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))
except: pass

DB_FILE = "railgap_enterprise.db"

# ==========================================
# 2. UI STYLING
# ==========================================
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap');
    html, body, [class*="css"] { font-family: 'Inter', sans-serif; background-color: #f8fafc; color: #111827; }
    
    .unlock-card {
        background: white;
        border: 1px solid #e2e8f0;
        border-radius: 16px;
        padding: 25px;
        text-align: center;
        box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.1);
        margin-bottom: 30px;
    }
    .blur-container {
        filter: blur(6px);
        opacity: 0.5;
        pointer-events: none;
        user-select: none;
    }
    .stTextInput input { padding: 12px; border-radius: 8px; border: 1px solid #cbd5e1; text-align: center; font-weight: 600; letter-spacing: 1px; }
    
    #MainMenu {visibility: hidden;} footer {visibility: hidden;} header {visibility: hidden;}
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 3. DATABASE
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS journey_access (mobile_hash TEXT PRIMARY KEY, access_end TIMESTAMP, payment_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS train_scan_cache (train_no TEXT, scan_date DATE, scan_result JSON, chart_status TEXT, expires_at TIMESTAMP)''')
    conn.commit(); conn.close()

def hash_mobile(mobile): return hashlib.sha256(mobile.encode()).hexdigest()

def get_access_status(mobile):
    if not mobile or len(mobile) != 10: return False, None
    h = hash_mobile(mobile); conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute("SELECT access_end FROM journey_access WHERE mobile_hash = ?", (h,))
    row = c.fetchone(); conn.close()
    if row:
        expiry = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S.%f")
        if datetime.datetime.now() < expiry: return True, expiry
    return False, None

def grant_access(mobile, pay_id):
    h = hash_mobile(mobile); conn = sqlite3.connect(DB_FILE); c = conn.cursor(); now = datetime.datetime.now()
    expiry = now + datetime.timedelta(hours=24)
    c.execute("INSERT OR REPLACE INTO journey_access VALUES (?, ?, ?)", (h, expiry, pay_id))
    conn.commit(); conn.close()

def get_cached_data(train_no):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor(); today = datetime.datetime.now().date()
    c.execute("SELECT scan_result, expires_at, chart_status FROM train_scan_cache WHERE train_no = ? AND scan_date = ?", (train_no, today))
    row = c.fetchone(); conn.close()
    if row:
        expiry = datetime.datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S.%f")
        if datetime.datetime.now() < expiry: return json.loads(row[0]), row[2], "VALID"
        else: return None, None, "EXPIRED"
    return None, None, "MISSING"

def save_to_cache(train_no, data, status):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor(); now = datetime.datetime.now()
    ttl = datetime.timedelta(hours=12) if status == "PREPARED" else datetime.timedelta(minutes=15)
    expiry = now + ttl; today = now.date()
    c.execute("DELETE FROM train_scan_cache WHERE train_no = ? AND scan_date = ?", (train_no, today))
    c.execute("INSERT INTO train_scan_cache VALUES (?, ?, ?, ?, ?)", (train_no, today, json.dumps(data), status, expiry))
    conn.commit(); conn.close()

init_db()

# ==========================================
# 4. PAYMENT HANDLER (AUTO-VERIFY)
# ==========================================
qp = st.query_params
if "payment_id" in qp and "order_id" in qp and "signature" in qp:
    p_id = qp["payment_id"]; o_id = qp["order_id"]; sig = qp["signature"]; mob = qp.get("mobile", "")
    msg = f"{o_id}|{p_id}".encode('utf-8')
    gen_sig = hmac.new(RZP_KEY_SECRET.encode('utf-8'), msg, hashlib.sha256).hexdigest()
    
    if gen_sig == sig:
        grant_access(mob, p_id)
        st.session_state['mobile'] = mob
        st.session_state['just_paid'] = True
        st.query_params.clear()
    else:
        st.error("⚠️ Payment Failed."); st.query_params.clear()

# ==========================================
# 5. BOT ENGINE
# ==========================================
# --- UPDATED DRIVER LOGIC FOR CLOUD & LOCAL ---
def run_bot_live(train_no, status_box):
    options = Options()
    # Cloud (Linux) ke liye ye settings zaroori hain
    options.add_argument("--headless") 
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    
    try:
        # Code pehle Cloud wala driver dhundega
        service = Service() 
        driver = webdriver.Chrome(service=service, options=options)
    except:
        # Agar Cloud wala nahi mila, to Local 'chromedriver.exe' dhundega
        if os.path.exists("chromedriver.exe"):
             service = Service(executable_path="chromedriver.exe")
             driver = webdriver.Chrome(service=service, options=options)
        else: return "DRIVER_ERROR", [], "ERROR"
    
    actions = ActionChains(driver)
    # ... baki ka code same rahega ...

    try:
        status_box.update(label="📡 Connecting...", state="running", expanded=True)
        driver.get("https://www.irctc.co.in/online-charts/"); wait = WebDriverWait(driver, 15); time.sleep(2)
        if "downtime" in driver.page_source.lower():
             driver.delete_all_cookies(); driver.refresh(); time.sleep(3)
             if "downtime" in driver.page_source.lower(): driver.quit(); return "MAINTENANCE", [], "ERROR"

        status_box.write(f"🚂 Searching {train_no}...")
        try:
            train_input = wait.until(EC.element_to_be_clickable((By.XPATH, "(//input[@type='text'])[1]")))
            actions.move_to_element(train_input).click().perform(); train_input.clear(); train_input.send_keys(train_no)
            time.sleep(1); train_input.send_keys(Keys.ARROW_DOWN); train_input.send_keys(Keys.ENTER)
        except: driver.quit(); return "TRAIN_ERROR", [], "ERROR"

        status_box.write("🚉 Fetching Station...")
        try:
            inputs = driver.find_elements(By.TAG_NAME, "input"); station_input = [i for i in inputs if i.get_attribute("type") == "text"][-1]
            actions.move_to_element(station_input).click().perform(); time.sleep(1); station_input.send_keys(Keys.ARROW_DOWN); station_input.send_keys(Keys.ENTER)
        except: return "STATION_ERROR", [], "ERROR"

        status_box.write("📂 Checking Chart..."); time.sleep(1); driver.execute_script("let btns = document.querySelectorAll('button'); btns[btns.length-1].click();")
        chart_loaded = False
        for _ in range(40):
            time.sleep(1); btns = driver.find_elements(By.TAG_NAME, "button")
            if any(re.match(r'^[A-Z]{1,2}[0-9]{1,2}$', b.text.strip()) for b in btns if b.is_displayed()): chart_loaded = True; break
        if not chart_loaded:
            if "chart not prepared" in driver.page_source.lower(): driver.quit(); return "CHART_NOT_PREPARED", [], "NOT_PREPARED"
            driver.quit(); return "NO_DATA", [], "ERROR"
    except: driver.quit(); return "ERROR", [], "ERROR"

    status_box.write("🔍 Extracting Seats...")
    driver.execute_script("""window.CAUGHT_DATA = null; const originalXHR = window.XMLHttpRequest; window.XMLHttpRequest = function() { const realXHR = new originalXHR(); realXHR.addEventListener("load", function() { if (this.responseText && this.responseText.includes("bdd")) { try { const data = JSON.parse(this.responseText); if (data.bdd) { window.CAUGHT_DATA = data; } } catch (e) {} } }); return realXHR; };""")
    all_buttons = driver.find_elements(By.TAG_NAME, "button")
    coach_list = sorted(list(set([b.text.strip() for b in all_buttons if b.is_displayed() and re.match(r'^[A-Z]{1,2}[0-9]{1,2}$', b.text.strip()) and "ENG" not in b.text.strip()])))
    scanned_data = []; processed = set(); prog = st.progress(0)
    for idx, c in enumerate(coach_list):
        prog.progress((idx+1)/len(coach_list))
        try:
            xpath = f"//button[normalize-space()='{c}']"; elems = driver.find_elements(By.XPATH, xpath); target = next((e for e in elems if e.is_displayed()), None)
            if target:
                driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", target); time.sleep(0.3); target.click(); time.sleep(0.8); data = driver.execute_script("return window.CAUGHT_DATA;")
                if data:
                    c_name = data.get("coachName")
                    if c_name not in processed:
                        processed.add(c_name) 
                        for seat in data.get("bdd", []):
                            vacant = False; dtls = []
                            if seat.get("bsd"):
                                for seg in seat.get("bsd"):
                                    if not seg.get("occupancy"): vacant=True; dtls.append(f"{seg.get('from')}->{seg.get('to')}")
                            elif not seat.get("occupancy"): vacant=True; dtls.append(f"{seat.get('from')}->{seat.get('to')}")
                            if vacant: scanned_data.append({"Coach": c_name, "Seat": seat.get("berthNo"), "Type": seat.get("berthCode"), "Route": ", ".join(dtls)})
                    driver.execute_script("window.CAUGHT_DATA = null;")
        except: pass
    prog.empty(); driver.quit(); status_box.update(label="✅ Complete", state="complete", expanded=False)
    return "SUCCESS", scanned_data, "PREPARED"

# ==========================================
# 6. UI FLOW
# ==========================================
if 'mobile' not in st.session_state: st.session_state['mobile'] = ""

st.markdown('<div style="text-align:center; padding:20px;"><div style="font-size:26px; font-weight:800;">🚄 RailGap Pro</div></div>', unsafe_allow_html=True)

# Status/Restore
has_access, expiry = get_access_status(st.session_state['mobile'])
if has_access:
    t_left = str(expiry - datetime.datetime.now()).split('.')[0]
    st.success(f"⚡ Premium Active until {t_left}")
elif st.session_state.get('just_paid', False):
    st.balloons(); st.success("🎉 Payment Successful!"); st.session_state['just_paid'] = False
else:
    with st.expander("Already paid? Click to Restore"):
        r_mob = st.text_input("Registered Mobile", max_chars=10)
        if st.button("Restore"):
            acc, _ = get_access_status(r_mob)
            if acc: st.session_state['mobile'] = r_mob; st.rerun()
            else: st.error("No active pass found.")

# Search
col1, col2 = st.columns([3, 1])
with col1: train_no = st.text_input("Train No", "22957", label_visibility="collapsed", placeholder="Enter Train Number")
with col2: search = st.button("Find Seats", type="primary")

if search and len(train_no) > 4:
    c_data, c_status, c_validity = get_cached_data(train_no)
    if c_validity == "VALID":
        st.session_state['data'] = c_data; st.session_state['chart_status'] = c_status
    else:
        with st.status("Searching...", expanded=True) as status:
            res_type, fresh_data, chart_status = run_bot_live(train_no, status)
            if res_type == "SUCCESS": save_to_cache(train_no, fresh_data, "PREPARED"); st.session_state['data'] = fresh_data; st.session_state['chart_status'] = "PREPARED"; st.rerun()
            elif res_type == "CHART_NOT_PREPARED": st.warning("⚠️ Chart Not Prepared Yet"); save_to_cache(train_no, [], "NOT_PREPARED")
            else: st.error("❌ Search Failed")

# ==========================================
# 7. RESULTS & LOCK SCREEN
# ==========================================
if 'data' in st.session_state and st.session_state.get('chart_status') == "PREPARED":
    df = pd.DataFrame(st.session_state['data'])
    
    if not has_access:
        # === LOCKED VIEW ===
        st.markdown(f"""
        <div class="unlock-card">
            <div style="font-size: 40px; margin-bottom: 5px;">🔒</div>
            <div style="font-size: 20px; font-weight: 800; color: #1e293b;">Unlock Full Details</div>
            <div style="color: #64748b; font-size: 14px; margin-bottom: 10px;">
                Found <span style="color:#2563eb; font-weight:700;">{len(df)}</span> available seats.
            </div>
            <div style="font-size: 32px; font-weight: 800; color: #2563eb; margin: 10px 0;">₹19</div>
        </div>
        """, unsafe_allow_html=True)

        def update_mobile(): st.session_state['mobile'] = st.session_state.temp_mobile
        st.text_input("Enter Mobile Number to Unlock", key="temp_mobile", on_change=update_mobile, max_chars=10, placeholder="e.g. 9876543210")
        
        current_mob = st.session_state.get('mobile', "")
        
        if len(current_mob) == 10:
            try:
                # SSL BYPASS ENABLED HERE
                order = client.order.create({"amount": 1900, "currency": "INR", "receipt": f"rcpt_{int(time.time())}"})
                o_id = order['id']
                
                payment_html = f"""
                <!DOCTYPE html>
                <html>
                <head><script src="https://checkout.razorpay.com/v1/checkout.js"></script></head>
                <body style="margin:0; padding:0; display:flex; justify-content:center;">
                    <button id="pay-btn" style="width:100%; max-width:400px; background: linear-gradient(135deg, #2563eb, #1d4ed8); color:white; border:none; padding:16px; border-radius:12px; font-weight:700; font-size:18px; cursor:pointer; font-family:sans-serif; box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);">
                        👉 Pay ₹19 & View Seats
                    </button>
                    <script>
                        var options = {{
                            "key": "{RZP_KEY_ID}", "amount": "1900", "currency": "INR", "name": "RailGap Pro",
                            "description": "Unlock Seats", "order_id": "{o_id}",
                            "handler": function (response){{
                                window.parent.location.href = window.parent.location.href.split("?")[0] + "?payment_id=" + response.razorpay_payment_id + "&order_id=" + response.razorpay_order_id + "&signature=" + response.razorpay_signature + "&mobile={current_mob}";
                            }},
                            "prefill": {{ "contact": "{current_mob}" }}, "theme": {{ "color": "#2563eb" }}
                        }};
                        document.getElementById('pay-btn').onclick = function(e){{ var rzp1 = new Razorpay(options); rzp1.open(); e.preventDefault(); }}
                    </script>
                </body></html>
                """
                components.html(payment_html, height=650)
                
            except Exception as e:
                # If SSL still fails, we catch it here
                st.error(f"⚠️ Payment Error: {str(e)}")
        else:
            st.info("👆 Enter your 10-digit mobile number to see payment options.")

        # Blurred Data
        teaser_df = df[['Coach', 'Seat', 'Type', 'Route']].head(10)
        teaser_html = teaser_df.to_html(index=False, classes='blur-table', border=0)
        st.markdown(f'<div class="blur-container">{teaser_html}</div>', unsafe_allow_html=True)

    else:
        # === UNLOCKED VIEW ===
        st.success("✅ Unlocked")
        coaches = sorted(df['Coach'].unique())
        sel_coach = st.selectbox("Filter by Coach", ["All"] + coaches)
        view_df = df if sel_coach == "All" else df[df['Coach'] == sel_coach]
        st.dataframe(view_df[['Coach', 'Seat', 'Type', 'Route']], use_container_width=True, hide_index=True)
        csv = view_df.to_csv(index=False).encode('utf-8')
        st.download_button("📥 Download CSV", csv, "seats.csv", "text/csv")