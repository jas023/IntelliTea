# pyright: reportMissingImports=false, reportMissingModuleSource=false
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
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
supabase = None
supabase_error = None

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
10. If 'Order not received': Say "Please call our admin at 9464364880. They will sort it out."
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

import razorpay

# Initialize Razorpay Client with your Test Keys
razorpay_client = razorpay.Client(auth=("YOUR_TEST_KEY_ID", "YOUR_TEST_KEY_SECRET"))

@app.route('/api/create-order', methods=['POST'])
def create_order():
    data = request.json
    amount_in_rupees = int(data.get("amount", 0))
    
    # Razorpay expects the amount in paise (1 Rupee = 100 Paise)
    amount_in_paise = amount_in_rupees * 100 
    
    # Create the order dictionary
    order_data = {
        "amount": amount_in_paise,
        "currency": "INR",
        "receipt": "receipt_001", # You can make this dynamic later when we add the database
        "payment_capture": 1 # Auto-capture the payment
    }
    
    try:
        # Ask Razorpay for an official Order ID
        razorpay_order = razorpay_client.order.create(data=order_data)
        return jsonify(razorpay_order)
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
            amount = 0.0

        order_payload = {
            "items": data.get("items"),
            "amount": amount,
            "address": data.get("address", "Not provided"),
            "status": "Pending" # Default status for new orders
        }

        customer_phone = data.get("customer_phone")
        if customer_phone:
            order_payload["customer_phone"] = str(customer_phone)

        if supabase is not None:
            # Try insert with customer_phone when available; fallback for older schema.
            try:
                response = supabase.table("orders").insert(order_payload).execute()
                return jsonify({"status": "success", "data": response.data})
            except Exception as insert_error:
                if "customer_phone" in order_payload and ("customer_phone" in str(insert_error) or "column" in str(insert_error).lower()):
                    order_payload.pop("customer_phone", None)
                    response = supabase.table("orders").insert(order_payload).execute()
                    return jsonify({"status": "success", "data": response.data})
                raise

        # Fallback path when supabase-py client init failed.
        try:
            rest_data = insert_via_rest("orders", order_payload)
            return jsonify({"status": "success", "data": rest_data, "mode": "rest-fallback"})
        except Exception as rest_error:
            if "customer_phone" in order_payload and "column" in str(rest_error).lower():
                order_payload.pop("customer_phone", None)
                rest_data = insert_via_rest("orders", order_payload)
                return jsonify({"status": "success", "data": rest_data, "mode": "rest-fallback"})
            raise
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- ADD THIS: Save Complaint to Supabase ---
@app.route('/api/save-complaint', methods=['POST'])
def save_complaint():
    data = request.json
    try:
        complaint_payload = {
            "issue": data.get("issue"),
            "details": data.get("details")
        }

        if supabase is not None:
            response = supabase.table("complaints").insert(complaint_payload).execute()
            return jsonify({"status": "success", "data": response.data})

        # Fallback path when supabase-py client init failed.
        rest_data = insert_via_rest("complaints", complaint_payload)
        return jsonify({"status": "success", "data": rest_data, "mode": "rest-fallback", "warning": supabase_error})
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