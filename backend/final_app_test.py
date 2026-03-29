import requests

# The URL of your local running Flask app
url = "http://localhost:5000/api/save-order"

# Mock data to simulate a real customer order
test_order = {
    "items": "2kg Black Gold Tea, 100g Masala",
    "amount": 810,
    "address": "House 42, Tea Lane, Ludhiana"
}

try:
    response = requests.post(url, json=test_order)
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.json()}")
except Exception as e:
    print(f"❌ Connection failed: {e}")