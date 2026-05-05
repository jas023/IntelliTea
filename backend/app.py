# pyright: reportMissingImports=false, reportMissingModuleSource=false
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import hashlib
import json
import time
import requests
from dotenv import load_dotenv
from groq import Groq
from supabase import create_client, Client

# Load env explicitly from backend folder first, then fallback to default search.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
load_dotenv(override=True)

# --- SUPABASE CONNECTION ---
SUPABASE_URL = os.getenv("SUPABASE_URL") 
# Prefer service role key on backend to bypass RLS for inserts.
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_SECRET_KEY")
    or os.getenv("SUPABASE_KEY")
)
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
supabase = None
supabase_error = None

# Process-local dedupe cache for quick duplicate submissions.
RECENT_ORDER_CACHE = {}
RECENT_ORDER_CACHE_TTL_SECONDS = 15 * 60
RECENT_UTR_CACHE = {}
RECENT_UTR_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

# --- Initialize the app ---
app = Flask(__name__)
CORS(app)


if not SUPABASE_URL or not SUPABASE_KEY:
    print("Error: Supabase credentials missing in .env file! Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY).")
else:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        supabase.table("orders").select("*").limit(1).execute()
        print("Supabase client initialized successfully.")
    except Exception as e:
        supabase_error = str(e)
        print(f"Supabase client init failed, REST fallback will be used: {e}")


def insert_via_rest(table_name, payload):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise Exception("Supabase URL/key missing for REST fallback")

    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table_name}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    response = requests.post(url, headers=headers, json=payload, timeout=15)
    if not response.ok:
        raise Exception(f"REST insert failed ({response.status_code}): {response.text}")
    try:
        return response.json()
    except Exception:
        return []


def fetch_via_rest(table_name, select_cols="*", limit=10, order_by=None, ascending=False):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise Exception("Supabase URL/key missing for REST fallback")

    params = {
        "select": select_cols,
        "limit": str(limit)
    }
    if order_by:
        direction = "asc" if ascending else "desc"
        params["order"] = f"{order_by}.{direction}"

    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table_name}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Prefer": "count=exact"
    }
    response = requests.get(url, headers=headers, params=params, timeout=15)
    if not response.ok:
        raise Exception(f"REST fetch failed ({response.status_code}): {response.text}")

    rows = response.json() if response.text else []
    count = None
    content_range = response.headers.get("content-range")
    if content_range and "/" in content_range:
        try:
            count = int(content_range.split("/")[-1])
        except Exception:
            count = None
    return rows, count


def normalize_payload_for_key(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_order_idempotency_key(data):
    provided_key = data.get("idempotency_key")
    if provided_key:
        return str(provided_key)

    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0.0

    normalized = {
        "items": data.get("items"),
        "amount": round(amount, 2),
        "address": str(data.get("address", "")).strip().lower(),
        "customer_phone": str(data.get("customer_phone", "")).strip(),
    }
    return hashlib.sha256(normalize_payload_for_key(normalized).encode("utf-8")).hexdigest()


def record_order_event(event_type, order_id, payload=None):
    event_payload = {
        "event_type": event_type,
        "order_id": order_id,
        "payload": payload or {}
    }

    try:
        if supabase is not None:
            try:
                supabase.table("order_events").insert(event_payload).execute()
                return
            except Exception:
                return

        insert_via_rest("order_events", event_payload)
    except Exception:
        return


def get_existing_order_by_idempotency(idempotency_key):
    if not idempotency_key or supabase is None:
        return None

    try:
        response = supabase.table("orders").select("*").eq("idempotency_key", idempotency_key).limit(1).execute()
        rows = getattr(response, "data", None) or []
        if isinstance(rows, list) and rows:
            return rows[0]
    except Exception:
        return None
    return None


def get_existing_order_from_cache(idempotency_key):
    if not idempotency_key:
        return None

    now = time.time()
    stale_keys = [k for k, v in RECENT_ORDER_CACHE.items() if (now - v.get("ts", 0)) > RECENT_ORDER_CACHE_TTL_SECONDS]
    for key in stale_keys:
        RECENT_ORDER_CACHE.pop(key, None)

    cached = RECENT_ORDER_CACHE.get(idempotency_key)
    if cached:
        return cached.get("order")
    return None


def put_order_in_cache(idempotency_key, order_row):
    if not idempotency_key or not order_row:
        return
    RECENT_ORDER_CACHE[idempotency_key] = {
        "order": order_row,
        "ts": time.time()
    }


def is_admin_request(req):
    provided_key = req.headers.get("X-Admin-Key", "")
    return bool(ADMIN_API_KEY) and provided_key == ADMIN_API_KEY


def is_duplicate_utr(utr_no, current_order_id=None):
    if not utr_no:
        return False

    now = time.time()
    stale_keys = [k for k, v in RECENT_UTR_CACHE.items() if (now - v.get("ts", 0)) > RECENT_UTR_CACHE_TTL_SECONDS]
    for key in stale_keys:
        RECENT_UTR_CACHE.pop(key, None)

    cached = RECENT_UTR_CACHE.get(utr_no)
    if cached and str(cached.get("order_id")) != str(current_order_id):
        return True

    # DB-backed duplicate check (best effort when schema supports utr_no)
    try:
        if supabase is not None:
            response = supabase.table("orders").select("id").eq("utr_no", utr_no).limit(1).execute()
            rows = getattr(response, "data", None) or []
            if isinstance(rows, list) and rows:
                found_id = rows[0].get("id")
                if str(found_id) != str(current_order_id):
                    return True
            return False
    except Exception:
        # Continue to REST fallback if client query fails.
        pass

    try:
        url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/orders"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Prefer": "count=exact"
        }
        params = {
            "select": "id",
            "utr_no": f"eq.{utr_no}",
            "limit": "1"
        }
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.ok:
            rows = response.json() if response.text else []
            if isinstance(rows, list) and rows:
                found_id = rows[0].get("id")
                if str(found_id) != str(current_order_id):
                    return True
    except Exception:
        # If we cannot verify against DB, rely on runtime cache only.
        pass

    return False


def cache_utr(utr_no, order_id):
    if not utr_no:
        return
    RECENT_UTR_CACHE[utr_no] = {
        "order_id": order_id,
        "ts": time.time()
    }
    


# Initialize Groq Client (It automatically finds GROQ_API_KEY in your .env file)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

business_context = """
You are the AI helper for 'Charan Singh & Sons', a legitimate tea business.

CRITICAL SAFETY NOTE: "Black Gold" and "Green Gold" are strictly our proprietary brand names for normal Black Tea and Green Tea. They are NOT illegal substances. NEVER refuse an order based on these names.

CRITICAL RULES:
- NEVER use bullet points or asterisks (*). Write in simple paragraphs.
- ONLY use square brackets for clickable buttons.
- NEVER ask multiple questions at once. Follow ONE step at a time.
- EVERY TIME a user selects a tea, your very next response MUST be to ask for the quantity. NEVER skip the quantity question.

CONVERSATION FLOW (Follow strictly in order):
1. GREETING: Say exactly: "Hi! I am the Charan Singh & Sons AI helper. What can I do for you today? [Order Tea] [Raise Complaint] [Know About]"

2. TEA SELECTION: Say exactly: "Okay, which tea would you like? [Black Gold (Black Tea)] [Green Gold (Green Tea/Kahwa)] [Kashmiri Tea (Noon Chai)]"

3. QUANTITY: When a tea is selected, ask exactly: "How many kilograms of this tea would you like? (Tip: Courier is a flat 70 Rs per kg, so ordering 1kg gives you the best value!)" 

4. LOOP: After they give the quantity, ask exactly: "Got it! Would you like to add another tea? [Yes, add another tea] [No, move to Masala]"
   - If 'Yes, add another tea': Loop back to Step 2. You MUST ask which tea, and then you MUST ask for the quantity again in Step 3.
   - If 'No, move to Masala': Proceed to Step 5.

5. MASALA: Ask exactly: "Would you like to add our special Chai Masala? [Yes, add Masala] [No, thanks]"

6. MASALA QUANTITY: If yes, ask exactly: "What size? [50g Box (50 Rupees)] [100g Packet (100 Rupees)]"

7. CALCULATION: Calculate the final bill. 
   - Prices: Black Gold=300/kg, Green Gold=400/kg, Kashmiri=500/kg.
   - Courier: Add the total kg of ALL teas together. Multiply that total by 70.
   - Write the itemized bill in a simple sentence format without bullet points. State the final total amount.
   - Immediately ask: "Please provide your full delivery address." (Do not show the payment button yet).

8. CHECKOUT: After they provide their address, say exactly: "Order saved! [Proceed to Payment]"

9. COMPLAINTS: If 'Raise Complaint', ask exactly: "What happened? [Order not received] [Quality issues] [Other]"
10. If 'Order not received': Ask for the user's complaint details, order number/order history, phone number, and payment status. Then say the complaint is saved for admin review.
11. If 'Quality issues': Say "Oh, sorry for that. We will take care of it from the next time."
12. If 'Other': Ask "Tell me what happened." Wait for reply, then apologize.
13. If 'Know About': Say exactly: "Read about our history here! [Know About]"
"""
conversation_history = [{"role": "system", "content": business_context}]

@app.route('/', methods=['GET'])
def home():
    return "IntelliTea AI Backend (Groq Powered) is running successfully!"

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get("message")
    
    if not user_message:
        return jsonify({"error": "Message is required"}), 400
    
    # if user_message.lower() == "order tea":
    #     global conversation_history
    #     conversation_history = [{"role": "system", "content": business_context}]
     
    global conversation_history
        
    try:
        # Add user message to history
        conversation_history.append({"role": "user", "content": user_message})
        
        # Send the whole history to Groq's super-fast Llama 3 model
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant", 
            messages=conversation_history,
            temperature=0.7
        )
        
        ai_reply = response.choices[0].message.content
        
        # Add AI reply to history so it remembers for the next turn
        conversation_history.append({"role": "assistant", "content": ai_reply})
        # ADD THIS: Check if the AI just finished the order
        if "Order saved!" in ai_reply:
            # Note: In a real app, your Frontend should now pull the 
            # cart data and send it to /api/save-order.
            print("LOG: Order detection triggered for Supabase.")
            
        
        return jsonify({"reply": ai_reply})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

  # --- ADD THIS: Save Order to Supabase ---
@app.route('/api/save-order', methods=['POST'])
def save_order():
    data = request.json
    try:
        amount = data.get("amount", 0)
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid amount"}), 400

        items = data.get("items")
        if not items:
            return jsonify({"error": "Items are required"}), 400

        address = str(data.get("address", "")).strip()
        if not address:
            return jsonify({"error": "Address is required"}), 400

        customer_phone = data.get("customer_phone")
        idempotency_key = build_order_idempotency_key(data)

        cached_order = get_existing_order_from_cache(idempotency_key)
        if cached_order:
            return jsonify({"status": "success", "data": [cached_order], "idempotency": True, "source": "cache"})

        existing_order = get_existing_order_by_idempotency(idempotency_key)
        if existing_order:
            put_order_in_cache(idempotency_key, existing_order)
            return jsonify({"status": "success", "data": [existing_order], "idempotency": True})

        order_payload = {
            "items": items,
            "amount": amount,
            "address": address,
            "status": "pending",
            "payment_status": "pending",
            "idempotency_key": idempotency_key
        }

        if customer_phone:
            order_payload["customer_phone"] = str(customer_phone)

        if supabase is not None:
            try:
                response = supabase.table("orders").insert(order_payload).execute()
                created_rows = response.data or []
                if created_rows:
                    put_order_in_cache(idempotency_key, created_rows[0])
                    record_order_event("order_created", created_rows[0].get("id"), {"idempotency_key": idempotency_key})
                return jsonify({"status": "success", "data": created_rows, "idempotency_key": idempotency_key})
            except Exception as insert_error:
                fallback_payload = {
                    "items": items,
                    "amount": amount,
                    "address": address,
                    "status": "pending",
                    "payment_status": "pending"
                }
                if customer_phone:
                    fallback_payload["customer_phone"] = str(customer_phone)
                try:
                    response = supabase.table("orders").insert(fallback_payload).execute()
                    created_rows = response.data or []
                    if created_rows:
                        put_order_in_cache(idempotency_key, created_rows[0])
                        record_order_event("order_created", created_rows[0].get("id"), {"idempotency_key": idempotency_key, "fallback": True})
                    return jsonify({"status": "success", "data": created_rows, "idempotency_key": idempotency_key, "warning": str(insert_error)})
                except Exception:
                    raise

        # Fallback path when supabase-py client init failed.
        try:
            rest_data = insert_via_rest("orders", order_payload)
            if rest_data and isinstance(rest_data, list) and rest_data[0].get("id"):
                put_order_in_cache(idempotency_key, rest_data[0])
                record_order_event("order_created", rest_data[0].get("id"), {"idempotency_key": idempotency_key, "mode": "rest-fallback"})
            return jsonify({"status": "success", "data": rest_data, "mode": "rest-fallback", "idempotency_key": idempotency_key})
        except Exception as rest_error:
            fallback_payload = {
                "items": items,
                "amount": amount,
                "address": address,
                "status": "pending",
                "payment_status": "pending"
            }
            if customer_phone:
                fallback_payload["customer_phone"] = str(customer_phone)
            rest_data = insert_via_rest("orders", fallback_payload)
            if rest_data and isinstance(rest_data, list) and rest_data[0].get("id"):
                put_order_in_cache(idempotency_key, rest_data[0])
                record_order_event("order_created", rest_data[0].get("id"), {"idempotency_key": idempotency_key, "mode": "rest-fallback", "fallback": True})
            return jsonify({"status": "success", "data": rest_data, "mode": "rest-fallback", "idempotency_key": idempotency_key, "warning": str(rest_error)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/submit-payment-proof', methods=['POST'])
def submit_payment_proof():
    data = request.json or {}
    order_id = data.get("order_id")
    utr_no = str(data.get("utr_no", "")).strip()

    if not order_id:
        return jsonify({"error": "order_id is required"}), 400
    if not utr_no or not (8 <= len(utr_no) <= 30):
        return jsonify({"error": "Valid utr_no is required"}), 400
    if not all(ch.isalnum() or ch == "-" for ch in utr_no):
        return jsonify({"error": "UTR can only contain letters, numbers, and hyphen"}), 400

    if is_duplicate_utr(utr_no, current_order_id=order_id):
        return jsonify({"error": "This UTR is already used for another order"}), 409

    payment_update = {
        "payment_status": "pending_verification",
        "utr_no": utr_no
    }

    try:
        if supabase is not None:
            try:
                response = supabase.table("orders").update(payment_update).eq("id", order_id).execute()
                updated_rows = response.data or []
                cache_utr(utr_no, order_id)
                record_order_event("payment_submitted", order_id, {"utr_no": utr_no})
                return jsonify({"status": "success", "data": updated_rows, "message": "Payment proof submitted. Awaiting admin verification."})
            except Exception:
                fallback_update = {"status": "pending_verification"}
                response = supabase.table("orders").update(fallback_update).eq("id", order_id).execute()
                updated_rows = response.data or []
                cache_utr(utr_no, order_id)
                record_order_event("payment_submitted", order_id, {"utr_no": utr_no, "fallback": True})
                return jsonify({"status": "success", "data": updated_rows, "warning": "Payment columns missing; status-only update used."})

        # REST fallback if client init failed.
        url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/orders?id=eq.{order_id}"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        response = requests.patch(url, headers=headers, json=payment_update, timeout=15)
        if not response.ok:
            response = requests.patch(url, headers=headers, json={"status": "pending_verification"}, timeout=15)
            if not response.ok:
                raise Exception(f"REST payment submit failed ({response.status_code}): {response.text}")
        cache_utr(utr_no, order_id)
        record_order_event("payment_submitted", order_id, {"utr_no": utr_no, "mode": "rest-fallback"})
        return jsonify({"status": "success", "data": response.json() if response.text else [], "mode": "rest-fallback", "message": "Payment proof submitted. Awaiting admin verification."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/confirm-payment', methods=['POST'])
def confirm_payment():
    if not ADMIN_API_KEY:
        return jsonify({"error": "Admin payment verification is not configured. Set ADMIN_API_KEY in backend .env"}), 503
    if not is_admin_request(request):
        return jsonify({"error": "Unauthorized. Admin key required."}), 401

    data = request.json or {}
    order_id = data.get("order_id")
    utr_no = str(data.get("utr_no", "")).strip()

    if not order_id:
        return jsonify({"error": "order_id is required"}), 400
    if not utr_no or not (8 <= len(utr_no) <= 30):
        return jsonify({"error": "Valid utr_no is required"}), 400
    if not all(ch.isalnum() or ch == "-" for ch in utr_no):
        return jsonify({"error": "UTR can only contain letters, numbers, and hyphen"}), 400

    if is_duplicate_utr(utr_no, current_order_id=order_id):
        return jsonify({"error": "This UTR is already used for another order"}), 409

    payment_update = {
        "payment_status": "paid",
        "utr_no": utr_no
    }

    try:
        if supabase is not None:
            try:
                response = supabase.table("orders").update(payment_update).eq("id", order_id).execute()
                updated_rows = response.data or []
                cache_utr(utr_no, order_id)
                record_order_event("payment_confirmed", order_id, {"utr_no": utr_no, "actor": "admin"})
                return jsonify({"status": "success", "data": updated_rows})
            except Exception:
                fallback_update = {"status": "Paid"}
                response = supabase.table("orders").update(fallback_update).eq("id", order_id).execute()
                updated_rows = response.data or []
                cache_utr(utr_no, order_id)
                record_order_event("payment_confirmed", order_id, {"utr_no": utr_no, "fallback": True, "actor": "admin"})
                return jsonify({"status": "success", "data": updated_rows, "warning": "Payment columns missing; status-only update used."})

        # REST fallback if client init failed.
        url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/orders?id=eq.{order_id}"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        response = requests.patch(url, headers=headers, json=payment_update, timeout=15)
        if not response.ok:
            response = requests.patch(url, headers=headers, json={"status": "Paid"}, timeout=15)
            if not response.ok:
                raise Exception(f"REST payment confirm failed ({response.status_code}): {response.text}")
        cache_utr(utr_no, order_id)
        record_order_event("payment_confirmed", order_id, {"utr_no": utr_no, "mode": "rest-fallback", "actor": "admin"})
        return jsonify({"status": "success", "data": response.json() if response.text else [], "mode": "rest-fallback"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- ADD THIS: Save Complaint to Supabase ---
@app.route('/api/save-complaint', methods=['POST'])
def save_complaint():
    data = request.json
    try:
        complaint_payload = {
            "issue": data.get("issue"),
            "details": data.get("details"),
            "complaint_type": data.get("complaint_type"),
            "customer_phone": data.get("customer_phone"),
            "order_history": data.get("order_history"),
            "payment_done": data.get("payment_done"),
            "order_id": data.get("order_id")
        }

        # Remove empty fields so older schemas have a better chance of accepting the insert.
        complaint_payload = {k: v for k, v in complaint_payload.items() if v not in (None, "", [], {})}

        if supabase is not None:
            try:
                response = supabase.table("complaints").insert(complaint_payload).execute()
                return jsonify({"status": "success", "data": response.data})
            except Exception as insert_error:
                # Fallback to the minimum legacy schema if the table has not been migrated yet.
                legacy_payload = {
                    "issue": complaint_payload.get("issue"),
                    "details": complaint_payload.get("details")
                }
                response = supabase.table("complaints").insert(legacy_payload).execute()
                return jsonify({"status": "success", "data": response.data, "warning": str(insert_error)})

        # Fallback path when supabase-py client init failed.
        try:
            rest_data = insert_via_rest("complaints", complaint_payload)
            return jsonify({"status": "success", "data": rest_data, "mode": "rest-fallback", "warning": supabase_error})
        except Exception as rest_error:
            legacy_payload = {
                "issue": complaint_payload.get("issue"),
                "details": complaint_payload.get("details")
            }
            rest_data = insert_via_rest("complaints", legacy_payload)
            return jsonify({"status": "success", "data": rest_data, "mode": "rest-fallback", "warning": str(rest_error)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/complaints', methods=['GET'])
def get_complaints():
    try:
        limit_raw = request.args.get("limit", "20")
        try:
            limit = max(1, min(int(limit_raw), 100))
        except Exception:
            limit = 20

        if supabase is not None:
            response = supabase.table("complaints") \
                .select("id,issue,details,created_at", count="exact") \
                .order("created_at", desc=True) \
                .limit(limit) \
                .execute()
            return jsonify({
                "status": "success",
                "data": response.data or [],
                "count": response.count or 0
            })

        rows, count = fetch_via_rest(
            "complaints",
            select_cols="id,issue,details,created_at",
            limit=limit,
            order_by="created_at",
            ascending=False
        )
        return jsonify({
            "status": "success",
            "data": rows or [],
            "count": count if count is not None else len(rows or []),
            "mode": "rest-fallback",
            "warning": supabase_error
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
# --- NEW: Route for Admin to change status ---
@app.route('/api/update-status', methods=['POST'])
def update_status():
    data = request.json
    order_id = data.get("id")
    new_status = data.get("status") # e.g., "Packed", "Shipped", "Delivered"
    
    try:
        response = supabase.table("orders").update({"status": new_status}).eq("id", order_id).execute()
        return jsonify({"status": "success", "data": response.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
         
if __name__ == '__main__':
    print("Backend server is running on http://localhost:5000")
    app.run(debug=True, port=5000)